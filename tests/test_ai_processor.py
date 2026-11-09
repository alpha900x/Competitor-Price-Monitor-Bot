"""AI layer tests with mocked Claude responses (no network, no API key needed)."""
import pytest

from src import ai_processor
from src.ai_processor import MAX_PDF_CHARS, extract_spec_json, extract_spec_with_raw
from src.config import Settings
from src.models import SpecSheetData

GOOD = '{"warranty_years": 3, "power_consumption_watts": 715, "key_features": ["24 PoE+ ports", "StackWise-480"]}'


class FakeClient:
    """Stands in for anthropic.Anthropic; replies come from a scripted list."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []  # list of kwargs passed to messages.create
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply

        class Block:
            text = reply

        class Resp:
            content = [Block()]

        return Resp()


@pytest.fixture
def settings():
    return Settings(ANTHROPIC_API_KEY="sk-ant-test", AI_MAX_RETRIES=2)


@pytest.fixture
def use_client(monkeypatch):
    def _use(replies):
        client = FakeClient(replies)
        monkeypatch.setattr(ai_processor, "_build_client", lambda s: client)
        return client
    return _use


def test_good_json(settings, use_client):
    use_client([GOOD])
    spec = extract_spec_json("some text", settings)
    assert isinstance(spec, SpecSheetData)
    assert spec.warranty_years == 3
    assert spec.power_consumption_watts == 715
    assert spec.key_features == ["24 PoE+ ports", "StackWise-480"]


def test_markdown_fenced_json(settings, use_client):
    use_client([f"```json\n{GOOD}\n```"])
    assert extract_spec_json("t", settings).warranty_years == 3


def test_json_with_surrounding_prose(settings, use_client):
    use_client([f"Here you go: {GOOD}\nHope that helps."])
    assert extract_spec_json("t", settings).power_consumption_watts == 715


def test_nulls_are_valid_and_count_as_success(settings, use_client):
    use_client(['{"warranty_years": null, "power_consumption_watts": null, "key_features": []}'])
    spec, raw = extract_spec_with_raw("t", settings)
    assert spec == SpecSheetData()
    assert raw is not None  # valid reply, just nothing found -> storable


def test_malformed_then_valid_retries_with_strict_followup(settings, use_client):
    client = use_client(["{warranty_years: 3,", GOOD])
    spec, raw = extract_spec_with_raw("t", settings)
    assert spec.warranty_years == 3 and raw == GOOD
    assert len(client.calls) == 2
    followup = client.calls[1]["messages"]
    assert followup[1] == {"role": "assistant", "content": "{warranty_years: 3,"}
    assert "valid JSON only" in followup[2]["content"]


def test_schema_violation_triggers_retry(settings, use_client):
    bad = '{"warranty_years": "three years", "power_consumption_watts": null, "key_features": []}'
    client = use_client([bad, GOOD])
    assert extract_spec_json("t", settings).warranty_years == 3
    assert len(client.calls) == 2


def test_total_failure_returns_empty_and_does_not_raise(settings, use_client):
    client = use_client(["nope", "still nope", "no again"])
    spec, raw = extract_spec_with_raw("t", settings)
    assert spec == SpecSheetData() and raw is None
    assert len(client.calls) == settings.AI_MAX_RETRIES + 1


def test_api_error_is_retried_then_succeeds(settings, use_client):
    use_client([RuntimeError("boom"), GOOD])
    assert extract_spec_json("t", settings).warranty_years == 3


def test_api_errors_every_time_return_empty(settings, use_client):
    use_client([RuntimeError("a"), RuntimeError("b"), RuntimeError("c")])
    assert extract_spec_json("t", settings) == SpecSheetData()


def test_missing_api_key_makes_no_call(use_client):
    client = use_client([GOOD])
    spec, raw = extract_spec_with_raw("text", Settings(ANTHROPIC_API_KEY=""))
    assert spec == SpecSheetData() and raw is None and client.calls == []


def test_empty_text_makes_no_call(settings, use_client):
    client = use_client([GOOD])
    assert extract_spec_with_raw("   ", settings)[1] is None
    assert client.calls == []


def test_text_is_truncated_and_model_from_settings(use_client):
    s = Settings(ANTHROPIC_API_KEY="k", AI_MODEL="some-model-id")
    client = use_client([GOOD])
    extract_spec_json("Q" * (MAX_PDF_CHARS + 5000), s)
    call = client.calls[0]
    assert call["model"] == "some-model-id"
    assert call["messages"][0]["content"].count("Q") == MAX_PDF_CHARS


def test_key_features_trimmed_to_eight(settings, use_client):
    feats = ", ".join(f'"f{i}"' for i in range(12))
    use_client([f'{{"warranty_years": null, "power_consumption_watts": null, "key_features": [{feats}]}}'])
    assert len(extract_spec_json("t", settings).key_features) == 8


def test_client_construction_failure_is_soft(settings, monkeypatch):
    def boom(s):
        raise RuntimeError("no sdk")
    monkeypatch.setattr(ai_processor, "_build_client", boom)
    assert extract_spec_json("t", settings) == SpecSheetData()
