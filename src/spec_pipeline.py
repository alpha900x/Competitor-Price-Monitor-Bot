"""Glue for one product's spec sheet: download -> hash -> cache check -> extract -> AI -> store.

New in Sprint 2 (not in ARCHITECTURE.md tree). Keeps pdf_extractor / ai_processor
independent of db.py. Sprint 4's main.py calls get_or_extract_spec() per product.
Never raises.
"""
import logging

from pydantic import BaseModel

from src.ai_processor import extract_spec_with_raw
from src.config import Settings
from src.db import get_spec_sheet_by_hash, save_spec_sheet
from src.models import SpecSheetData
from src.pdf_extractor import download_pdf, extract_text

logger = logging.getLogger(__name__)


class SpecResult(BaseModel):
    spec: SpecSheetData | None = None   # None if nothing usable (download/AI failure)
    pdf_hash: str | None = None         # pass to db.save_snapshot (also when cached)
    from_cache: bool = False            # True = AI call skipped, stored result reused
    error: str | None = None


def get_or_extract_spec(product_id: int, pdf_url: str, settings: Settings) -> SpecResult:
    """Return spec data for pdf_url, calling the AI only for a PDF hash not seen before.

    The PDF is always downloaded (the hash is only known after download); the saving
    is the AI call. Failed extractions are NOT stored, so the next run retries them.
    """
    try:
        path, pdf_hash = download_pdf(pdf_url, settings.PDF_STORAGE_DIR)
    except Exception as exc:
        logger.error("Spec PDF download failed for %s: %s", pdf_url, exc)
        return SpecResult(error=f"download failed: {exc}")

    try:
        cached = get_spec_sheet_by_hash(pdf_hash)
        if cached:
            logger.info("Spec sheet %s already processed; reusing stored result", pdf_hash[:12])
            spec = SpecSheetData(
                warranty_years=cached.get("warranty_years"),
                power_consumption_watts=cached.get("power_consumption_watts"),
                key_features=cached.get("key_features") or [],
            )
            return SpecResult(spec=spec, pdf_hash=pdf_hash, from_cache=True)

        text = extract_text(path)
        spec, raw = extract_spec_with_raw(text, settings)
        if raw is None:
            return SpecResult(pdf_hash=pdf_hash, error="AI extraction failed or PDF had no text")
        save_spec_sheet(product_id, pdf_url, pdf_hash, spec, raw)
        return SpecResult(spec=spec, pdf_hash=pdf_hash)
    except Exception as exc:
        logger.error("Spec pipeline error for %s: %s", pdf_url, exc)
        return SpecResult(pdf_hash=pdf_hash, error=f"pipeline error: {exc}")
