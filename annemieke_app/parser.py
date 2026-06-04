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
QUANTITY_PATTERN = re.compile(r"(?P<quantity>\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,3})?)\s+(?P<unit>m1|m2|m3|st|stk|wkn|uur|uren|kg|ton)\b", re.IGNORECASE)
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


def parse_pdf(path: Path) -> ParsedPdf:
    reader = PdfReader(str(path))
    pages = []

    for page in reader.pages:
        pages.append(page.extract_text() or "")

    text = "\n".join(pages).strip()
    fields = _extract_fields(text)
    budget_lines = extract_budget_lines(text)
    notes = None if text else "Er is geen tekst gevonden. Mogelijk is dit een gescande PDF waarvoor OCR nodig is."

    return ParsedPdf(text=text, fields=fields, budget_lines=budget_lines, notes=notes)


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
            description = normalized[: quantity_match.start()].strip()

        if not description:
            description = normalized

        eenheidsprijs = money_values[-2] if len(money_values) >= 2 else None
        totaal = money_values[-1] if money_values else None

        lines.append(
            ParsedBudgetLine(
                line_number=len(lines) + 1,
                omschrijving_werkzaamheden=description,
                hoeveelheid=quantity,
                eenheid=unit,
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
