import base64
import json
import mimetypes
import os
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pypdf import PdfReader


KEY_VALUE_PATTERN = re.compile(
    r"^(?P<key>[A-Za-z][A-Za-z0-9 /_.-]{2,50})\s*[:\-]\s*(?P<value>.+)$",
    re.MULTILINE,
)
DATE_PATTERN = re.compile(r"\b(?P<date>\d{1,2}[-/]\d{1,2}[-/]\d{2,4})\b")
AMOUNT_PATTERN = re.compile(r"\b(?:EUR|euro|bedrag)?\s*(?P<amount>\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2}))\b", re.IGNORECASE)
QUANTITY_PATTERN = re.compile(r"(?P<quantity>\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,3})?)\s*(?P<unit>m1|m2|m3|st|stk|wkn|wk|uur|uren|kg|ton)\b", re.IGNORECASE)
MONEY_PATTERN = re.compile(r"\d{1,3}(?:[.]\d{3})*(?:,\d{2})|\d+(?:[.,]\d{2})")


@dataclass(frozen=True)
class ParsedField:
    name: str
    value: str
    confidence: int = 60


@dataclass(frozen=True)
class ParsedPdf:
    text: str
    fields: list[ParsedField]
    budget_lines: list["ParsedBudgetLine"]
    notes: str | None = None
    parse_method: str = "pdf_text"
    confidence: int = 0
    openai_usage: dict | None = None


@dataclass(frozen=True)
class ParsedBudgetLine:
    line_number: int
    omschrijving_werkzaamheden: str
    hoeveelheid: Decimal | None = None
    eenheid: str | None = None
    norm_arbeid: Decimal | None = None
    uren: Decimal | None = None
    materiaal: Decimal | None = None
    materieel: Decimal | None = None
    onderaannemer: Decimal | None = None
    totaal_prijs_per_regel: Decimal | None = None
    eenheidsprijs: Decimal | None = None
    confidence: int = 45
    raw_text: str = ""


def parse_pdf(path: Path, force_openai: bool = False) -> ParsedPdf:
    text = _extract_pdf_text(path)
    parse_method = "pdf_text"
    notes = None

    if _ocr_enabled() and _text_needs_ocr(text):
        ocr_text = _extract_ocr_text(path)
        if len(ocr_text) > len(text):
            text = ocr_text
            parse_method = "ocr"

    fields = _extract_fields(text)
    budget_lines = extract_budget_lines(text)
    confidence = _line_confidence(budget_lines)

    if _openai_enabled() and (force_openai or confidence < _openai_threshold()):
        openai_result = _parse_budget_with_openai(path, text)
        if openai_result and openai_result.budget_lines:
            return openai_result

    if not text:
        notes = "Er is geen tekst gevonden. OCR en OpenAI fallback konden geen bruikbare tekst ophalen."
    elif confidence < 70:
        notes = "Parserzekerheid is laag. Controleer de regels of parse opnieuw met OpenAI."

    return ParsedPdf(
        text=text,
        fields=fields,
        budget_lines=budget_lines,
        notes=notes,
        parse_method=parse_method,
        confidence=confidence,
    )


def _extract_fields(text: str) -> list[ParsedField]:
    found: dict[str, ParsedField] = {}

    for match in KEY_VALUE_PATTERN.finditer(text):
        key = _normalize_key(match.group("key"))
        value = match.group("value").strip()
        if key and value and key not in found:
            found[key] = ParsedField(name=key, value=value, confidence=70)

    first_date = DATE_PATTERN.search(text)
    if first_date and "datum" not in found:
        found["datum"] = ParsedField(name="datum", value=first_date.group("date"), confidence=55)

    first_amount = AMOUNT_PATTERN.search(text)
    if first_amount and "bedrag" not in found:
        found["bedrag"] = ParsedField(name="bedrag", value=first_amount.group("amount"), confidence=50)

    return list(found.values())


def _normalize_key(raw_key: str) -> str:
    key = raw_key.strip().lower()
    key = re.sub(r"\s+", "_", key)
    key = re.sub(r"[^a-z0-9_]", "", key)
    return key[:80]


def extract_budget_lines(text: str) -> list[ParsedBudgetLine]:
    lines: list[ParsedBudgetLine] = []

    for raw_line in text.splitlines():
        normalized = " ".join(raw_line.split())
        if not _looks_like_budget_line(normalized):
            continue

        quantity_match = QUANTITY_PATTERN.search(normalized)
        money_values = [_to_decimal(value) for value in MONEY_PATTERN.findall(normalized)]
        money_values = [value for value in money_values if value is not None]

        description = normalized
        quantity = None
        unit = None
        if quantity_match:
            quantity = _to_decimal(quantity_match.group("quantity"))
            unit = quantity_match.group("unit")
            before_quantity = normalized[: quantity_match.start()].strip()
            after_quantity = normalized[quantity_match.end() :].strip()
            description = before_quantity or after_quantity

        money_matches = list(MONEY_PATTERN.finditer(description))
        if money_matches:
            description = description[: money_matches[0].start()].strip() or normalized

        norm_arbeid = money_values[0] if len(money_values) >= 3 else None
        eenheidsprijs = money_values[-2] if len(money_values) >= 2 else None
        totaal = money_values[-1] if money_values else None

        lines.append(
            ParsedBudgetLine(
                line_number=len(lines) + 1,
                omschrijving_werkzaamheden=description,
                hoeveelheid=quantity,
                eenheid=unit,
                norm_arbeid=norm_arbeid,
                totaal_prijs_per_regel=totaal,
                eenheidsprijs=eenheidsprijs,
                confidence=65 if quantity_match and money_values else 45,
                raw_text=normalized,
            )
        )

    return lines


def _looks_like_budget_line(line: str) -> bool:
    if len(line) < 4:
        return False
    lowered = line.lower()
    if lowered.startswith(("omschrijving", "hvh", "ehd", "norm", "uren", "materiaal", "materieel")):
        return False
    return bool(QUANTITY_PATTERN.search(line) or len(MONEY_PATTERN.findall(line)) >= 2)


def _to_decimal(value: str) -> Decimal | None:
    cleaned = value.strip().replace(" ", "")
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _extract_pdf_text(path: Path) -> str:
    reader = PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages).strip()


def _text_needs_ocr(text: str) -> bool:
    if len(text.strip()) < 120:
        return True
    useful_lines = [line for line in text.splitlines() if _looks_like_budget_line(" ".join(line.split()))]
    return len(useful_lines) < 2


def _extract_ocr_text(path: Path) -> str:
    try:
        import fitz
        import pytesseract
        from PIL import Image

        document = fitz.open(path)
        pages = []
        for page in document:
            pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            image = Image.frombytes("RGB", [pixmap.width, pixmap.height], pixmap.samples)
            pages.append(pytesseract.image_to_string(image, lang=os.getenv("OCR_LANG", "nld+eng")))
        return "\n".join(pages).strip()
    except Exception as exc:
        return f""


def _ocr_enabled() -> bool:
    enabled = os.getenv("OCR_ENABLED", "true").strip().lower()
    return enabled not in {"0", "false", "nee", "no"}


def _openai_enabled() -> bool:
    enabled = os.getenv("OPENAI_BUDGET_FALLBACK_ENABLED", "true").strip().lower()
    return bool(os.getenv("OPENAI_API_KEY")) and enabled not in {"0", "false", "nee", "no"}


def _openai_threshold() -> int:
    try:
        return int(os.getenv("OPENAI_BUDGET_CONFIDENCE_THRESHOLD", "70"))
    except ValueError:
        return 70


def _line_confidence(lines: list[ParsedBudgetLine]) -> int:
    if not lines:
        return 0
    scores = [line.confidence for line in lines]
    complete = sum(1 for line in lines if line.omschrijving_werkzaamheden and line.hoeveelheid and line.totaal_prijs_per_regel)
    return min(95, int((sum(scores) / len(scores)) + (complete / len(lines)) * 20))


def _parse_budget_with_openai(path: Path, text: str) -> ParsedPdf | None:
    import requests

    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini"
    content = [
        {
            "type": "input_text",
            "text": (
                "Parseer deze Nederlandse bouwbegroting naar begrotingsregels. "
                "Geef alleen regels terug die op begrotingsregels lijken. "
                "Kolommen: omschrijving_werkzaamheden, hoeveelheid, eenheid, norm_arbeid, uren, "
                "materiaal, materieel, onderaannemer, eenheidsprijs, totaal_prijs_per_regel. "
                "Gebruik decimalen als getal zonder duizendtallen. Laat onbekende waarden null. "
                "Brontekst:\n\n" + text[:50000]
            ),
        }
    ]
    image_url = _first_page_image_data_url(path)
    if image_url:
        content.append({"type": "input_image", "image_url": image_url, "detail": "high"})

    payload = {
        "model": model,
        "input": [{"role": "user", "content": content}],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "budget_parse",
                "schema": _openai_schema(),
                "strict": True,
            }
        },
    }
    try:
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=90,
        )
        response.raise_for_status()
        response_payload = response.json()
        data = _extract_response_json(response_payload)
        lines = _normalize_openai_budget_lines(data)
        if not lines:
            return None
        return ParsedPdf(
            text=text,
            fields=[],
            budget_lines=lines,
            notes="Geparsed met OpenAI fallback. Controleer bedragen en kolommen.",
            parse_method="openai",
            confidence=85,
            openai_usage=_normalize_usage(response_payload.get("usage") or {}),
        )
    except Exception:
        return None


def _openai_schema() -> dict:
    nullable_number = {"anyOf": [{"type": "number"}, {"type": "null"}]}
    nullable_string = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    line = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "omschrijving_werkzaamheden": {"type": "string"},
            "hoeveelheid": nullable_number,
            "eenheid": nullable_string,
            "norm_arbeid": nullable_number,
            "uren": nullable_number,
            "materiaal": nullable_number,
            "materieel": nullable_number,
            "onderaannemer": nullable_number,
            "eenheidsprijs": nullable_number,
            "totaal_prijs_per_regel": nullable_number,
            "confidence": {"type": "number", "minimum": 0, "maximum": 100},
        },
        "required": [
            "omschrijving_werkzaamheden",
            "hoeveelheid",
            "eenheid",
            "norm_arbeid",
            "uren",
            "materiaal",
            "materieel",
            "onderaannemer",
            "eenheidsprijs",
            "totaal_prijs_per_regel",
            "confidence",
        ],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {"lines": {"type": "array", "items": line}},
        "required": ["lines"],
    }


def _extract_response_json(payload: dict) -> dict:
    for item in payload.get("output", []):
        for content in item.get("content", []):
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                return json.loads(text)
    return {}


def _normalize_openai_budget_lines(data: dict) -> list[ParsedBudgetLine]:
    lines = []
    for raw in data.get("lines") or []:
        description = str(raw.get("omschrijving_werkzaamheden") or "").strip()
        if not description:
            continue
        lines.append(
            ParsedBudgetLine(
                line_number=len(lines) + 1,
                omschrijving_werkzaamheden=description,
                hoeveelheid=_to_decimal(str(raw.get("hoeveelheid"))) if raw.get("hoeveelheid") is not None else None,
                eenheid=str(raw.get("eenheid") or "").strip() or None,
                norm_arbeid=_to_decimal(str(raw.get("norm_arbeid"))) if raw.get("norm_arbeid") is not None else None,
                uren=_to_decimal(str(raw.get("uren"))) if raw.get("uren") is not None else None,
                materiaal=_to_decimal(str(raw.get("materiaal"))) if raw.get("materiaal") is not None else None,
                materieel=_to_decimal(str(raw.get("materieel"))) if raw.get("materieel") is not None else None,
                onderaannemer=_to_decimal(str(raw.get("onderaannemer"))) if raw.get("onderaannemer") is not None else None,
                eenheidsprijs=_to_decimal(str(raw.get("eenheidsprijs"))) if raw.get("eenheidsprijs") is not None else None,
                totaal_prijs_per_regel=_to_decimal(str(raw.get("totaal_prijs_per_regel"))) if raw.get("totaal_prijs_per_regel") is not None else None,
                confidence=max(0, min(100, int(raw.get("confidence") or 75))),
                raw_text="openai",
            )
        )
    return lines


def _first_page_image_data_url(path: Path) -> str | None:
    try:
        import fitz

        document = fitz.open(path)
        if not document:
            return None
        pixmap = document[0].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        encoded = base64.b64encode(pixmap.tobytes("png")).decode("ascii")
        return f"data:image/png;base64,{encoded}"
    except Exception:
        return None


def _normalize_usage(usage: dict) -> dict:
    return {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "cached_input_tokens": int((usage.get("input_tokens_details") or {}).get("cached_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }
