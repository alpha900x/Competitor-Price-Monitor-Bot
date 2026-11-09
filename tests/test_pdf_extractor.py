import hashlib
import os

import pytest

from src import pdf_extractor
from src.pdf_extractor import NotAPdfError, PdfDownloadError, download_pdf, extract_text
from tests.conftest import SAMPLE_PDF


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(pdf_extractor.time, "sleep", lambda s: None)


def test_download_returns_path_and_correct_sha256(pdf_server, tmp_path):
    path, digest = download_pdf(f"{pdf_server}/sample_spec_sheet.pdf", str(tmp_path))
    expected = hashlib.sha256(open(SAMPLE_PDF, "rb").read()).hexdigest()
    assert digest == expected
    assert os.path.basename(path) == f"{expected}.pdf"
    assert open(path, "rb").read() == open(SAMPLE_PDF, "rb").read()
    assert [f for f in os.listdir(tmp_path) if f.endswith(".part")] == []


def test_same_pdf_twice_gives_same_hash_and_single_file(pdf_server, tmp_path):
    url = f"{pdf_server}/sample_spec_sheet.pdf"
    a = download_pdf(url, str(tmp_path))
    b = download_pdf(url, str(tmp_path))
    assert a == b and len(os.listdir(tmp_path)) == 1


def test_creates_dest_dir(pdf_server, tmp_path):
    dest = tmp_path / "nested" / "pdfs"
    download_pdf(f"{pdf_server}/sample_spec_sheet.pdf", str(dest))
    assert dest.is_dir()


def test_html_landing_page_is_rejected_and_leaves_no_file(pdf_server, tmp_path):
    """Lenovo's pdf_url is an HTML landing page (PROJECT_STATE known issue)."""
    with pytest.raises(NotAPdfError):
        download_pdf(f"{pdf_server}/landing.html", str(tmp_path))
    assert os.listdir(tmp_path) == []


def test_404_fails_immediately(pdf_server, tmp_path):
    with pytest.raises(PdfDownloadError, match="404"):
        download_pdf(f"{pdf_server}/missing.pdf", str(tmp_path))


def test_503_is_retried_then_succeeds(pdf_server, tmp_path):
    path, _ = download_pdf(f"{pdf_server}/flaky.pdf", str(tmp_path))
    assert os.path.exists(path)


def test_connection_refused_raises_pdf_download_error(tmp_path):
    with pytest.raises(PdfDownloadError):
        download_pdf("http://127.0.0.1:9/x.pdf", str(tmp_path))


def test_extract_text_sample_contains_known_specs():
    text = extract_text(SAMPLE_PDF)
    assert "C9300-24P-A" in text
    assert "3-Year Premium" in text
    assert "715W AC" in text
    assert "\n" not in text and "  " not in text  # whitespace collapsed


def test_extract_text_unreadable_file_returns_empty(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf at all")
    assert extract_text(str(bad)) == ""
    assert extract_text(str(tmp_path / "missing.pdf")) == ""
