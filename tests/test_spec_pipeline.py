"""Pipeline test: download -> extract -> (mock) AI -> db, and the same-hash skip.

Uses only documented db.py signatures. AI replies are mocked.
"""
import pytest

from src import ai_processor, db, pdf_extractor
from src.config import Settings
from src.spec_pipeline import get_or_extract_spec

GOOD = '{"warranty_years": 3, "power_consumption_watts": null, "key_features": ["24 PoE+ ports", "StackWise-480"]}'


class FakeClient:
    def __init__(self, replies):
        self.replies, self.calls, self.messages = list(replies), [], self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)

        class B:
            text = reply

        class R:
            content = [B()]
        return R()


@pytest.fixture
def env(tmp_path, monkeypatch, pdf_server):
    monkeypatch.setattr(pdf_extractor.time, "sleep", lambda s: None)
    db.init_db(str(tmp_path / "t.db"))
    pid = db.upsert_product("router_switch", "https://www.router-switch.com/c9300-24p-a.html", "C9300-24P-A")
    settings = Settings(ANTHROPIC_API_KEY="k", PDF_STORAGE_DIR=str(tmp_path / "pdfs"))
    return pid, settings, f"{pdf_server}/sample_spec_sheet.pdf"


def _mock_ai(monkeypatch, replies):
    client = FakeClient(replies)
    monkeypatch.setattr(ai_processor, "_build_client", lambda s: client)
    return client


def test_first_run_calls_ai_and_stores_second_run_reuses(env, monkeypatch):
    pid, settings, url = env
    client = _mock_ai(monkeypatch, [GOOD])

    first = get_or_extract_spec(pid, url, settings)
    assert first.error is None and first.from_cache is False
    assert first.spec.warranty_years == 3
    assert len(client.calls) == 1
    # prompt really contained the PDF text
    assert "3-Year Premium" in client.calls[0]["messages"][0]["content"]
    stored = db.get_spec_sheet_by_hash(first.pdf_hash)
    assert stored["key_features"] == ["24 PoE+ ports", "StackWise-480"]
    assert stored["raw_ai_response"] == GOOD

    second = get_or_extract_spec(pid, url, settings)  # same bytes -> same hash
    assert second.from_cache is True and second.pdf_hash == first.pdf_hash
    assert second.spec == first.spec
    assert len(client.calls) == 1  # NO second AI call


def test_failed_ai_is_not_cached_so_next_run_retries(env, monkeypatch):
    pid, settings, url = env
    _mock_ai(monkeypatch, ["bad", "bad", "bad"])
    res = get_or_extract_spec(pid, url, settings)
    assert res.spec is None and res.error and res.pdf_hash
    assert db.get_spec_sheet_by_hash(res.pdf_hash) is None

    client = _mock_ai(monkeypatch, [GOOD])
    again = get_or_extract_spec(pid, url, settings)
    assert again.spec.warranty_years == 3 and len(client.calls) == 1


def test_download_failure_is_soft(env, pdf_server):
    pid, settings, _ = env
    res = get_or_extract_spec(pid, f"{pdf_server}/landing.html", settings)
    assert res.spec is None and res.pdf_hash is None and "download failed" in res.error


def test_no_api_key_is_soft(env):
    pid, settings, url = env
    res = get_or_extract_spec(pid, url, settings.model_copy(update={"ANTHROPIC_API_KEY": ""}))
    assert res.spec is None and res.error
