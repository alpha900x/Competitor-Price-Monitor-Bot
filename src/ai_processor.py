"""Claude API -> SpecSheetData (Sprint 2). Fail soft: never raises.

Contract (ARCHITECTURE.md section 6):
    extract_spec_json(raw_pdf_text, settings) -> SpecSheetData
Addition (needed so Sprint 4 can store raw_ai_response):
    extract_spec_with_raw(raw_pdf_text, settings) -> tuple[SpecSheetData, str | None]
    The str is the raw model reply that validated; None means total failure.
"""
import json
import logging
import re

import anthropic
from pydantic import ValidationError

from src.config import Settings
from src.models import SpecSheetData

logger = logging.getLogger(__name__)

# ROADMAP suggests ~8000. Raised to 12000 because the router-switch sample puts
# its warranty line ~6k chars in; a longer sheet would push it past 8000.
MAX_PDF_CHARS = 12000
MAX_OUTPUT_TOKENS = 600

SYSTEM_PROMPT = (
    "You extract structured data from product datasheet text. "
    "Reply with ONLY a single JSON object, no prose, no markdown fences, with exactly these keys:\n"
    '{"warranty_years": integer or null, '
    '"power_consumption_watts": integer or null, '
    '"key_features": array of at most 8 short strings}\n'
    "Rules:\n"
    "- warranty_years: the manufacturer/seller warranty length in whole years, e.g. "
    '"3-Year Premium Warranty" -> 3. null if not stated.\n'
    "- power_consumption_watts: the product's own stated power consumption/draw in watts. "
    "Do NOT use power-supply ratings, PoE budgets or other capacities as consumption. "
    "null if the text does not state consumption.\n"
    "- key_features: the most decision-relevant technical features (ports, capacity, stacking, "
    "PoE, uplinks, licensing). Short phrases, each under 80 characters.\n"
    "- Never guess. If a value is not in the text, use null (or [] for key_features)."
)

STRICT_FOLLOWUP = (
    "Your previous reply was not valid. Return valid JSON only: one object with keys "
    '"warranty_years" (integer or null), "power_consumption_watts" (integer or null), '
    '"key_features" (array of strings). No prose, no markdown fences.'
)


def _build_client(settings: Settings) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)


def _parse_spec(reply: str) -> SpecSheetData:
    """Parse a model reply into SpecSheetData. Tolerates ```json fences and surrounding prose.

    Raises ValueError / ValidationError on bad input (caught by the retry loop).
    """
    text = reply.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object in reply")
    obj, _ = json.JSONDecoder().raw_decode(text[start:])  # ignores trailing prose
    if not isinstance(obj, dict):
        raise ValueError("JSON reply is not an object")
    spec = SpecSheetData.model_validate(obj)
    spec.key_features = [str(f).strip() for f in spec.key_features if str(f).strip()][:8]
    return spec


def _call_claude(client: anthropic.Anthropic, settings: Settings, messages: list[dict]) -> str:
    resp = client.messages.create(
        model=settings.AI_MODEL,
        max_tokens=MAX_OUTPUT_TOKENS,
        system=SYSTEM_PROMPT,
        messages=messages,
    )
    return "".join(getattr(b, "text", "") for b in resp.content)


def extract_spec_with_raw(raw_pdf_text: str, settings: Settings) -> tuple[SpecSheetData, str | None]:
    empty = SpecSheetData()
    if not raw_pdf_text or not raw_pdf_text.strip():
        logger.warning("AI extraction skipped: empty PDF text (scanned/image-only PDF?)")
        return empty, None
    if not settings.ANTHROPIC_API_KEY:
        logger.warning("AI extraction skipped: ANTHROPIC_API_KEY is not set")
        return empty, None

    try:
        client = _build_client(settings)
    except Exception as exc:
        logger.error("Could not create Anthropic client: %s", exc)
        return empty, None

    text = raw_pdf_text[:MAX_PDF_CHARS]
    messages: list[dict] = [
        {"role": "user", "content": f"Datasheet text:\n<datasheet>\n{text}\n</datasheet>"}
    ]
    last_reply = ""
    attempts = settings.AI_MAX_RETRIES + 1  # AI_MAX_RETRIES = extra attempts
    for attempt in range(attempts):
        try:
            last_reply = _call_claude(client, settings, messages)
            return _parse_spec(last_reply), last_reply
        except (ValueError, ValidationError) as exc:  # json.JSONDecodeError is a ValueError
            logger.warning("AI reply failed validation (attempt %d/%d): %s", attempt + 1, attempts, exc)
            # Stricter follow-up: show the model its bad reply, ask for JSON only.
            messages = messages[:1] + [
                {"role": "assistant", "content": last_reply or "(empty)"},
                {"role": "user", "content": STRICT_FOLLOWUP},
            ]
        except Exception as exc:  # API/network errors; SDK already retried transient ones
            logger.warning("Claude API call failed (attempt %d/%d): %s", attempt + 1, attempts, exc)
    logger.error("AI extraction failed after %d attempts. Last raw reply: %r", attempts, last_reply[:2000])
    return empty, None


def extract_spec_json(raw_pdf_text: str, settings: Settings) -> SpecSheetData:
    """Contract function: typed spec data, never raises. Total failure -> nulls/empty list."""
    try:
        return extract_spec_with_raw(raw_pdf_text, settings)[0]
    except Exception as exc:  # belt and braces
        logger.error("Unexpected error in extract_spec_json: %s", exc)
        return SpecSheetData()
