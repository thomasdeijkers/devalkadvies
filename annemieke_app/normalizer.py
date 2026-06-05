from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import BudgetLine, NormalizationTerm, ReferenceLine


DEFAULT_TERMS = [
    ("uitgangspunten", "Uitgangspunten", ["uitgangspunt", "uitgangspunten", "onderliggende begroting"], "hard", 100),
    ("energie_prestatie", "Energie prestatie", ["energie prestatie", "energieprestatie", "energielabel"], "hard", 100),
    ("vooropname", "Vooropname", ["vooropname", "voor opname", "omliggende belendingen"], "hard", 100),
    ("flora_fauna", "Flora en fauna", ["flora en fauna", "flora fauna"], "hard", 100),
    ("maatvoering", "Maatvoering", ["maatvoering", "hoofdmaatvoering", "controle maatvoering"], "hard", 100),
    ("zav", "Zelf aangebrachte voorzieningen", ["zav", "zav's", "zelf aangebrachte voorzieningen"], "hard", 100),
    ("projectleiding", "Projectleiding", ["projectleiding", "project leider"], "fuzzy", 84),
    ("uitvoering", "Uitvoering", ["uitvoering", "uitvoerder", "uitvoering assistent"], "fuzzy", 84),
    ("voorman", "Voorman", ["voorman", "meewerkend voorman"], "fuzzy", 84),
    ("bouwplaats", "Bouwplaatskosten", ["bouwplaatskosten", "bouwplaats voorziening", "algemene bouwplaats"], "fuzzy", 84),
    ("opruimen", "Opruimen en schoonmaken", ["opruimen", "schoonmaken", "bouw schoonmaken"], "fuzzy", 84),
    ("mobiele_kraan", "Mobiele kraan", ["mobiele kraan", "mobiele kranen", "kraan fundering"], "fuzzy", 84),
]

NOISE_FRAGMENTS = {
    "printtijd",
    "printdatum",
    "pagina:",
    "bestand:",
    "copyright",
    "bladzijde",
}


def seed_default_normalization_terms(session: Session) -> None:
    if session.scalar(select(NormalizationTerm.id).limit(1)):
        return
    for canonical_key, canonical_label, aliases, match_type, min_score in DEFAULT_TERMS:
        for alias in aliases:
            session.add(
                NormalizationTerm(
                    canonical_key=canonical_key,
                    canonical_label=canonical_label,
                    alias=alias,
                    category="omschrijving",
                    match_type=match_type,
                    min_score=min_score,
                    active=1,
                )
            )
    session.commit()


def normalize_text(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(character for character in text if not unicodedata.combining(character))
    text = text.lower()
    text = text.replace("&", " en ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalization_key(value: str | None) -> str:
    text = normalize_text(value)
    text = re.sub(r"^\d+(\s+\d+)*\s+", "", text)
    text = re.sub(r"\b\d+[.,]?\d*\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:180]


def is_noise_line(value: str | None) -> bool:
    raw = (value or "").strip()
    if not raw:
        return True
    lowered = raw.lower()
    if any(fragment in lowered for fragment in NOISE_FRAGMENTS):
        return True
    normalized = normalize_text(raw)
    if normalized in {"omschrijving hvh ehd norm uren materiaal materieel oa eindprijs totaal", "hvh ehd norm uren"}:
        return True
    if len(normalized) <= 2 and not re.search(r"[a-z]{3,}", normalized):
        return True
    if re.fullmatch(r"[\d\s.,:/-]+", raw):
        return True
    return False


def apply_normalization(session: Session, lines: Iterable[BudgetLine | ReferenceLine]) -> None:
    line_list = [line for line in lines if line is not None]
    if not line_list:
        return

    terms = session.scalars(
        select(NormalizationTerm)
        .where(NormalizationTerm.active == 1)
        .order_by(NormalizationTerm.match_type, NormalizationTerm.canonical_label)
    ).all()
    references = _reference_candidates(session)

    for line in line_list:
        description = (line.omschrijving_werkzaamheden or "").strip()
        cleaned_key = normalization_key(description)
        line.normalized_key = cleaned_key or None
        line.normalized_omschrijving = description or None
        line.normalization_method = "raw"
        line.normalization_score = 0
        line.normalization_candidate = None

        if is_noise_line(description):
            line.normalization_method = "noise"
            line.normalization_score = 0
            line.confidence = min(line.confidence or 0, 25)
            continue

        hard_match = _hard_match(cleaned_key, terms)
        if hard_match:
            _apply_term(line, hard_match, 100, "hard")
            continue

        fuzzy_match, fuzzy_score = _fuzzy_term(cleaned_key, terms)
        if fuzzy_match:
            _apply_term(line, fuzzy_match, fuzzy_score, "fuzzy")
            continue

        reference_label, reference_key, reference_score = _fuzzy_reference(cleaned_key, references)
        if reference_label:
            line.normalized_key = reference_key
            line.normalized_omschrijving = reference_label
            line.normalization_method = "reference"
            line.normalization_score = reference_score
            line.normalization_candidate = reference_label if reference_score < 96 else None
            continue

        line.normalization_score = 55 if line.eenheidsprijs is not None else 35


def split_aliases(value: str) -> list[str]:
    aliases = []
    for part in re.split(r"[\n;]+", value or ""):
        cleaned = part.strip()
        if cleaned:
            aliases.append(cleaned)
    return aliases


def _hard_match(cleaned_key: str, terms: list[NormalizationTerm]) -> NormalizationTerm | None:
    if not cleaned_key:
        return None
    for term in terms:
        if term.match_type != "hard":
            continue
        alias_key = normalization_key(term.alias)
        if not alias_key:
            continue
        if cleaned_key == alias_key or alias_key in cleaned_key:
            return term
    return None


def _fuzzy_term(cleaned_key: str, terms: list[NormalizationTerm]) -> tuple[NormalizationTerm | None, int]:
    best_term: NormalizationTerm | None = None
    best_score = 0
    for term in terms:
        alias_key = normalization_key(term.alias)
        if not alias_key:
            continue
        score = _score(cleaned_key, alias_key)
        if len(alias_key) >= 6 and alias_key in cleaned_key:
            score = max(score, 92)
        if score > best_score:
            best_term = term
            best_score = score
    if best_term and best_score >= max(best_term.min_score or 82, 65):
        return best_term, best_score
    return None, best_score


def _fuzzy_reference(cleaned_key: str, references: list[tuple[str, str]]) -> tuple[str | None, str | None, int]:
    best_label: str | None = None
    best_key: str | None = None
    best_score = 0
    for reference_key, reference_label in references:
        score = _score(cleaned_key, reference_key)
        if score > best_score:
            best_label = reference_label
            best_key = reference_key
            best_score = score
    if best_label and best_key and best_score >= 78:
        return best_label, best_key, best_score
    return None, None, best_score


def _apply_term(line: BudgetLine | ReferenceLine, term: NormalizationTerm, score: int, method: str) -> None:
    line.normalized_key = term.canonical_key
    line.normalized_omschrijving = term.canonical_label
    line.normalization_method = method
    line.normalization_score = score
    line.normalization_candidate = term.canonical_label if method == "fuzzy" else None


def _reference_candidates(session: Session) -> list[tuple[str, str]]:
    rows = session.execute(
        select(ReferenceLine.normalized_key, ReferenceLine.normalized_omschrijving)
        .where(ReferenceLine.normalized_key.is_not(None))
        .where(ReferenceLine.normalized_omschrijving.is_not(None))
        .where(ReferenceLine.eenheidsprijs.is_not(None))
        .limit(1000)
    ).all()
    seen: set[str] = set()
    candidates: list[tuple[str, str]] = []
    for key, label in rows:
        if not key or not label or key in seen:
            continue
        seen.add(key)
        candidates.append((str(key), str(label)))
    return candidates


def _score(left: str, right: str) -> int:
    if not left or not right:
        return 0
    return round(SequenceMatcher(None, left, right).ratio() * 100)
