"""PDF download + raw text extraction (Sprint 2).

Contracts (ARCHITECTURE.md section 6):
    download_pdf(pdf_url, dest_dir) -> (local_file_path, sha256_hash)
    extract_text(pdf_path) -> str

download_pdf RAISES PdfDownloadError on failure (callers such as spec_pipeline
catch it). extract_text never raises: unreadable PDF -> "".
"""
import hashlib
import logging
import os
import re
import tempfile
import time

import httpx
from pypdf import PdfReader

from src.config import get_settings

logger = logging.getLogger(__name__)

PDF_MAGIC = b"%PDF-"
MAX_PDF_BYTES = 25 * 1024 * 1024  # safety cap for a spec sheet


class PdfDownloadError(Exception):
    """Download failed or the response is not a PDF."""


class NotAPdfError(PdfDownloadError):
    """Server answered, but the body is not a PDF (e.g. an HTML landing page)."""


def _stream_to_file(client: httpx.Client, pdf_url: str, dest_dir: str) -> tuple[str, str]:
    """One attempt: stream body to a temp file while hashing; rename to <sha256>.pdf."""
    hasher = hashlib.sha256()
    size = 0
    fd, tmp_path = tempfile.mkstemp(dir=dest_dir, suffix=".part")
    try:
        with os.fdopen(fd, "wb") as fh, client.stream("GET", pdf_url) as resp:
            if resp.status_code >= 400:
                raise PdfDownloadError(f"HTTP {resp.status_code} for {pdf_url}")
            first = True
            for chunk in resp.iter_bytes(65536):
                if first:
                    first = False
                    if not chunk.lstrip()[:5].startswith(PDF_MAGIC):
                        ctype = resp.headers.get("content-type", "unknown")
                        raise NotAPdfError(
                            f"{pdf_url} did not return a PDF (content-type: {ctype}); "
                            "likely an HTML landing page"
                        )
                size += len(chunk)
                if size > MAX_PDF_BYTES:
                    raise PdfDownloadError(f"PDF exceeds {MAX_PDF_BYTES} bytes: {pdf_url}")
                hasher.update(chunk)
                fh.write(chunk)
            if size == 0:
                raise NotAPdfError(f"Empty response body for {pdf_url}")
        digest = hasher.hexdigest()
        final_path = os.path.join(dest_dir, f"{digest}.pdf")
        os.replace(tmp_path, final_path)  # same bytes -> same file, so re-downloads are idempotent
        return final_path, digest
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def download_pdf(pdf_url: str, dest_dir: str) -> tuple[str, str]:
    """Stream-download a PDF, return (local_path, sha256 of the bytes).

    File is stored as <dest_dir>/<sha256>.pdf. Retries transient failures
    (network errors, timeouts, HTTP 429/5xx) up to SCRAPER_MAX_RETRIES extra
    attempts with 1s/2s/4s backoff. Non-PDF bodies and HTTP 4xx fail at once.
    Raises PdfDownloadError.
    """
    settings = get_settings()
    os.makedirs(dest_dir, exist_ok=True)
    attempts = settings.SCRAPER_MAX_RETRIES + 1
    last_err: Exception | None = None
    with httpx.Client(
        timeout=settings.SCRAPER_TIMEOUT_SECONDS,
        headers={"User-Agent": settings.SCRAPER_USER_AGENT},
        follow_redirects=True,
    ) as client:
        for attempt in range(attempts):
            try:
                return _stream_to_file(client, pdf_url, dest_dir)
            except NotAPdfError:
                raise
            except PdfDownloadError as exc:
                last_err = exc
                transient = any(f"HTTP {c}" in str(exc) for c in (429, 500, 502, 503, 504))
                if not transient:
                    raise
            except httpx.HTTPError as exc:
                last_err = exc
            if attempt < attempts - 1:
                delay = min(2 ** attempt, 8)
                logger.warning("PDF download retry %d/%d in %ss: %s", attempt + 1, attempts - 1, delay, last_err)
                time.sleep(delay)
    raise PdfDownloadError(f"Download failed after {attempts} attempts: {last_err}")


def extract_text(pdf_path: str) -> str:
    """Extract text from every page with pypdf, collapse whitespace. "" on failure."""
    try:
        reader = PdfReader(pdf_path)
        pages = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception as exc:  # one bad page should not lose the rest
                logger.warning("pypdf failed on a page of %s: %s", pdf_path, exc)
        return re.sub(r"\s+", " ", " ".join(pages)).strip()
    except Exception as exc:
        logger.error("Could not read PDF %s: %s", pdf_path, exc)
        return ""
