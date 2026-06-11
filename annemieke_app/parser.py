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
AMOUNT_TEXT = r"-?(?:\d{1,3}(?:[.\s]\d{3})+(?:,\d{2})?|\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d+(?:[.,]\d{2}))"
AMOUNT_PATTERN = re.compile(rf"\b(?:EUR|euro|bedrag)?\s*(?P<amount>{AMOUNT_TEXT})\b", re.IGNORECASE)
QUANTITY_PATTERN = re.compile(r"(?P<quantity>\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,3})?)\s*(?P<unit>m1|m2|m3|st|stk|wkn|wk|uur|uren|kg|ton)\b", re.IGNORECASE)
MONEY_PATTERN = re.compile(AMOUNT_TEXT)


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
    regel_type: str = "regel"
    niveau: int = 0
    hoofdstuk_code: str | None = None
    hoofdstuk_omschrijving: str | None = None
    post_code: str | None = None
    hoeveelheid: Decimal | None = None
    eenheid: str | None = None
    norm_arbeid: Decimal | None = None
    uren: Decimal | None = None
    materiaal: Decimal | None = None
    materieel: Decimal | None = None
    onderaannemer: Decimal | None = None
    totaal_prijs_per_regel: Decimal | None = None
    eenheidsprijs: Decimal | None = None
    bron_pagina: int | None = None
    confidence: int = 45
    raw_text: str = ""


def parse_pdf(path: Path, force_openai: bool = False, openai_model: str | None = None) -> ParsedPdf:
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
        openai_result = _parse_budget_with_openai(path, text, openai_model=openai_model)
        if openai_result and openai_result.budget_lines and (
            force_openai or len(openai_result.budget_lines) >= max(1, int(len(budget_lines) * 0.8))
        ):
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
    current_chapter_code: str | None = None
    current_chapter_description: str | None = None

    for raw_line in _candidate_budget_rows(text):
        normalized = " ".join(raw_line.split())
        chapter = _chapter_from_line(normalized)
        if chapter:
            current_chapter_code, current_chapter_description = chapter
            lines.append(
                ParsedBudgetLine(
                    line_number=len(lines) + 1,
                    omschrijving_werkzaamheden=current_chapter_description,
                    regel_type="hoofdstuk",
                    niveau=0,
                    hoofdstuk_code=current_chapter_code,
                    hoofdstuk_omschrijving=current_chapter_description,
                    confidence=75,
                    raw_text=normalized,
                )
            )
            continue
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
                regel_type="regel",
                niveau=1 if current_chapter_code else 0,
                hoofdstuk_code=current_chapter_code,
                hoofdstuk_omschrijving=current_chapter_description,
                post_code=_post_code_from_line(normalized),
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


def _chapter_from_line(line: str) -> tuple[str | None, str] | None:
    if len(line) > 120 or len(MONEY_PATTERN.findall(line)) > 0:
        return None
    match = re.match(r"^(?P<code>(?:\\d+[.)]?|[A-Z]\\d{0,3}|\\d{2,}(?:\\.\\d+)*))\\s+(?P<title>[A-Za-zÀ-ÿ].+)$", line)
    if not match:
        return None
    title = match.group("title").strip(" :-")
    if len(title) < 3:
        return None
    return match.group("code").strip(".)"), title


def _post_code_from_line(line: str) -> str | None:
    match = re.match(r"^(?P<code>\\d+(?:\\.\\d+)*|[A-Z]{1,3}\\d+(?:\\.\\d+)*)\\s+", line)
    return match.group("code") if match else None


def _candidate_budget_rows(text: str) -> list[str]:
    rows: list[str] = []
    pending = ""

    for raw in text.splitlines():
        line = " ".join(raw.split())
        if not line:
            pending = ""
            continue
        if _is_table_noise(line):
            continue

        if _chapter_from_line(line):
            if pending and _looks_like_budget_line(pending):
                rows.append(pending)
            rows.append(line)
            pending = ""
            continue

        candidate = f"{pending} {line}".strip() if pending else line
        if _looks_like_budget_line(candidate):
            rows.append(candidate)
            pending = ""
            continue

        money_count = len(MONEY_PATTERN.findall(line))
        has_letters = bool(re.search(r"[A-Za-zÀ-ÿ]", line))
        if has_letters and money_count < 2 and len(line) < 140:
            pending = candidate
        else:
            pending = ""

    if pending and _looks_like_budget_line(pending):
        rows.append(pending)

    return rows


def _is_table_noise(line: str) -> bool:
    lowered = line.lower().strip(" :;|-")
    if not lowered:
        return True
    header_words = {"omschrijving", "hvh", "ehd", "norm", "uren", "materiaal", "materieel", "o.a.", "ehdprijs", "totaal"}
    tokens = {token.strip(" :;|-") for token in lowered.split()}
    return len(tokens & header_words) >= 2


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
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "." in cleaned:
        if cleaned.count(".") > 1 or re.fullmatch(r"-?\d{1,3}(?:\.\d{3})+", cleaned):
            cleaned = cleaned.replace(".", "")
    elif "," in cleaned:
        if cleaned.count(",") > 1 or re.fullmatch(r"-?\d{1,3}(?:,\d{3})+", cleaned):
            cleaned = cleaned.replace(",", "")
        else:
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


def _parse_budget_with_openai(path: Path, text: str, openai_model: str | None = None) -> ParsedPdf | None:
    import requests

    model = (openai_model or os.getenv("OPENAI_MODEL", "gpt-5")).strip() or "gpt-5"
    content = [
        {
            "type": "input_text",
            "text": (
                "Parseer deze Nederlandse bouwbegroting naar een controlemodel voor bouwkostenadvies. "
                "Behoud de begrotingsstructuur: hoofdstukken, posten, subposten en regels moeten in dezelfde volgorde blijven staan. "
                "Neem hoofdstukken/tussenkoppen op als regel_type='hoofdstuk' of 'post', ook als er geen bedragen in staan. "
                "Voeg omschrijvingen die over meerdere regels lopen samen tot één duidelijke omschrijving. "
                "Sla regels niet over omdat een kolom leeg is; zet onbekende kolommen op null. "
                "Negeer alleen paginanummers, kop-/voetteksten en pure tabelheaders. "
                "Kolommen: regel_type, niveau, hoofdstuk_code, hoofdstuk_omschrijving, post_code, "
                "omschrijving_werkzaamheden, hoeveelheid, eenheid, norm_arbeid, uren, materiaal, materieel, "
                "onderaannemer, eenheidsprijs, totaal_prijs_per_regel, bron_pagina. "
                "Zet arbeidsnormen in norm_arbeid, arbeidsuren in uren, materiaalbedragen in materiaal, "
                "materieelbedragen in materieel, onderaannemersbedragen in onderaannemer en eindprijzen in totaal_prijs_per_regel. "
                "Gebruik decimalen als getal zonder duizendtallen. Laat onbekende waarden null. "
                "Brontekst:\n\n" + text[:50000]
            ),
        }
    ]
    for image_url in _page_image_data_urls(path):
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
            openai_usage={**_normalize_usage(response_payload.get("usage") or {}), "model": model},
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
            "regel_type": {"type": "string", "enum": ["hoofdstuk", "post", "regel", "subtotaal", "totaal"]},
            "niveau": {"type": "number", "minimum": 0, "maximum": 8},
            "hoofdstuk_code": nullable_string,
            "hoofdstuk_omschrijving": nullable_string,
            "post_code": nullable_string,
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
            "bron_pagina": nullable_number,
            "confidence": {"type": "number", "minimum": 0, "maximum": 100},
        },
        "required": [
            "regel_type",
            "niveau",
            "hoofdstuk_code",
            "hoofdstuk_omschrijving",
            "post_code",
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
            "bron_pagina",
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
                regel_type=str(raw.get("regel_type") or "regel").strip() or "regel",
                niveau=_to_int(raw.get("niveau")) or 0,
                hoofdstuk_code=str(raw.get("hoofdstuk_code") or "").strip() or None,
                hoofdstuk_omschrijving=str(raw.get("hoofdstuk_omschrijving") or "").strip() or None,
                post_code=str(raw.get("post_code") or "").strip() or None,
                hoeveelheid=_to_decimal(str(raw.get("hoeveelheid"))) if raw.get("hoeveelheid") is not None else None,
                eenheid=str(raw.get("eenheid") or "").strip() or None,
                norm_arbeid=_to_decimal(str(raw.get("norm_arbeid"))) if raw.get("norm_arbeid") is not None else None,
                uren=_to_decimal(str(raw.get("uren"))) if raw.get("uren") is not None else None,
                materiaal=_to_decimal(str(raw.get("materiaal"))) if raw.get("materiaal") is not None else None,
                materieel=_to_decimal(str(raw.get("materieel"))) if raw.get("materieel") is not None else None,
                onderaannemer=_to_decimal(str(raw.get("onderaannemer"))) if raw.get("onderaannemer") is not None else None,
                eenheidsprijs=_to_decimal(str(raw.get("eenheidsprijs"))) if raw.get("eenheidsprijs") is not None else None,
                totaal_prijs_per_regel=_to_decimal(str(raw.get("totaal_prijs_per_regel"))) if raw.get("totaal_prijs_per_regel") is not None else None,
                bron_pagina=_to_int(raw.get("bron_pagina")),
                confidence=max(0, min(100, int(raw.get("confidence") or 75))),
                raw_text="openai",
            )
        )
    return lines


def _to_int(value) -> int | None:
    if value is None:
        return None
    decimal_value = _to_decimal(str(value))
    if decimal_value is None:
        return None
    try:
        return int(decimal_value)
    except (InvalidOperation, ValueError):
        return None


def _page_image_data_urls(path: Path) -> list[str]:
    try:
        import fitz

        document = fitz.open(path)
        if not document:
            return []
        try:
            page_limit = int(os.getenv("OPENAI_IMAGE_PAGES", "6"))
        except ValueError:
            page_limit = 6
        images = []
        for page_index in range(min(len(document), max(1, page_limit))):
            page = document[page_index]
            pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            encoded = base64.b64encode(pixmap.tobytes("png")).decode("ascii")
            images.append(f"data:image/png;base64,{encoded}")
        return images
    except Exception:
        return []


def _normalize_usage(usage: dict) -> dict:
    return {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "cached_input_tokens": int((usage.get("input_tokens_details") or {}).get("cached_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }
