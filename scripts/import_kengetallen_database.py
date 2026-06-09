from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from annemieke_app.config import settings  # noqa: E402
from annemieke_app.database import SessionLocal, create_db  # noqa: E402
from annemieke_app.kengetallen import import_reference_lines  # noqa: E402
from annemieke_app.models import PriceIndexSeries, PriceIndexValue, ReferenceDataset, ReferenceLine  # noqa: E402
from annemieke_app.normalizer import apply_normalization, is_noise_line  # noqa: E402


DEFAULT_SOURCE_DIR = ROOT / "Hulp bestanden" / "Kengetallen"
SUMMARY_LABELS = {"laag", "gemiddeld", "hoog"}
META_HEADERS = {"fase", "peildatum", "periode", "bdb indexering"}
CATEGORY_HEADERS = {
    "algemeen": "Algemeen",
    "sloopwerk": "Sloopwerk",
    "fundering": "Fundering",
    "skelet": "Skelet",
    "daken": "Daken",
    "gevel": "Gevel",
    "binnenwanden": "Binnenwanden",
    "vloerafwerking": "Vloerafwerking",
    "trappen": "Trappen",
    "plafondafwerking": "Plafondafwerking",
    "vaste inrichting": "Vaste inrichting",
    "terrein": "Terrein",
    "w installatie": "W installatie",
    "e installatie": "E installatie",
    "t installatie": "T installatie",
    "bouwkundig": "Bouwkundig",
    "installatie": "Installatie",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Laad DeValk kengetallenbestanden eenmalig in de database.")
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Excelbestanden of mappen. Leeg = Hulp bestanden/Kengetallen.",
    )
    args = parser.parse_args()
    files = _collect_files(args.paths or [DEFAULT_SOURCE_DIR])
    if not files:
        raise SystemExit(f"Geen Excelbestanden gevonden in {DEFAULT_SOURCE_DIR}")

    create_db()
    with SessionLocal() as session:
        total_lines = 0
        for path in files:
            line_count = import_file(session, path)
            total_lines += line_count
            print(f"{path.name}: {line_count} kengetalregels geladen")
        print(f"Klaar: {len(files)} bestand(en), {total_lines} regels")


def import_file(session, path: Path) -> int:
    _delete_existing_dataset(session, path.name)
    dataset = ReferenceDataset(
        name=path.stem,
        original_filename=path.name,
        stored_filename=_store_source_file(path),
        source="kengetallen importscript",
        status="active",
        notes="Eenmalig ingeladen bronbestand voor historische kengetallen en normalisatie.",
    )
    session.add(dataset)
    session.flush()

    lines = _kengetallen_index_lines(path, dataset)
    if not lines:
        lines = _generic_reference_lines(path, dataset)
    for line in lines:
        session.add(line)
    if lines:
        apply_normalization(session, lines)
    index_count = _import_price_index_values(session, path)
    if index_count:
        dataset.notes = (
            "Eenmalig ingeladen bronbestand voor historische kengetallen en normalisatie. "
            f"Indexblad verwerkt: {index_count} periodes."
        )
    session.commit()
    return len(lines)


def _collect_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        resolved = path if path.is_absolute() else ROOT / path
        if resolved.is_dir():
            files.extend(sorted(resolved.glob("*.xlsx")))
            files.extend(sorted(resolved.glob("*.xlsm")))
        elif resolved.suffix.lower() in {".xlsx", ".xlsm"} and resolved.exists():
            files.append(resolved)
    return sorted({file.resolve() for file in files})


def _delete_existing_dataset(session, original_filename: str) -> None:
    existing = session.scalar(
        select(ReferenceDataset).where(ReferenceDataset.original_filename == original_filename).limit(1)
    )
    if existing is not None:
        session.delete(existing)
        session.flush()


def _store_source_file(path: Path) -> str:
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    stored_filename = f"kengetallen-{uuid4().hex}{path.suffix.lower()}"
    target = settings.upload_dir / stored_filename
    target.write_bytes(path.read_bytes())
    return stored_filename


def _kengetallen_index_lines(path: Path, dataset: ReferenceDataset) -> list[ReferenceLine]:
    workbook = load_workbook(path, data_only=True, keep_vba=path.suffix.lower() == ".xlsm")
    sheet = _find_index_sheet(workbook.worksheets)
    if sheet is None:
        return []
    blocks = _find_header_blocks(sheet)
    sheet_cache: dict[tuple[str, str], str | None] = {}
    lines: list[ReferenceLine] = []
    for block_index, block in enumerate(blocks):
        next_row = blocks[block_index + 1]["row"] if block_index + 1 < len(blocks) else sheet.max_row + 1
        project_column = block["project_column"]
        variant_column = project_column + 1
        for row in range(block["row"] + 1, next_row):
            project_name = _text(sheet.cell(row=row, column=project_column).value)
            if not project_name or project_name.lower() in SUMMARY_LABELS:
                continue
            variant = _text(sheet.cell(row=row, column=variant_column).value) or None
            phase = _text(_cell(sheet, row, block["columns"].get("fase")))
            period = _text(_cell(sheet, row, block["columns"].get("periode")))
            price_date = _date(_cell(sheet, row, block["columns"].get("peildatum")))
            index_factor = _decimal(_cell(sheet, row, block["columns"].get("bdb indexering")))
            lookup_key = (project_name, variant or "")
            if lookup_key not in sheet_cache:
                sheet_cache[lookup_key] = _match_project_sheet(workbook.worksheets, project_name, variant)
            project_sheet_name = sheet_cache[lookup_key]
            for category_key, category_label in CATEGORY_HEADERS.items():
                column = block["columns"].get(category_key)
                value = _decimal(_cell(sheet, row, column))
                if value is None:
                    continue
                description = f"{category_label} - {block['section']}"
                lines.append(
                    ReferenceLine(
                        dataset_id=dataset.id,
                        line_number=len(lines) + 1,
                        regel_type="kengetal_index",
                        niveau=0,
                        hoofdstuk_code=category_key,
                        hoofdstuk_omschrijving=block["section"],
                        post_code=category_key,
                        project_name=project_name,
                        relation_name=variant,
                        document_date=price_date,
                        phase=phase,
                        period=period,
                        bdb_indexering=index_factor,
                        project_sheet_name=project_sheet_name,
                        source_row=row,
                        omschrijving_werkzaamheden=description,
                        hoeveelheid=Decimal("1"),
                        eenheid="m2 bvo",
                        norm_arbeid=index_factor,
                        totaal_prijs_per_regel=value,
                        eenheidsprijs=value,
                        confidence=100,
                        raw_text=(
                            f"{path.name}|{sheet.title}|rij:{row}|categorie:{category_key}|"
                            f"fase:{phase}|periode:{period}"
                        ),
                    )
                )
    return lines


def _import_price_index_values(session, path: Path) -> int:
    workbook = load_workbook(path, data_only=True, keep_vba=path.suffix.lower() == ".xlsm")
    sheet = next((ws for ws in workbook.worksheets if _key(ws.title) == "indexen"), None)
    if sheet is None:
        return 0

    period_column = None
    index_column = None
    header_row = None
    for row in range(1, min(sheet.max_row, 30) + 1):
        for column in range(1, min(sheet.max_column, 12) + 1):
            label = _key(sheet.cell(row=row, column=column).value)
            if label == "periode":
                period_column = column
            elif label == "index":
                index_column = column
        if period_column and index_column:
            header_row = row
            break
    if not header_row:
        return 0

    series = session.scalar(
        select(PriceIndexSeries).where(PriceIndexSeries.name == "Nieuwbouwwoningen outputprijsindex bouwkosten")
    )
    if series is None:
        series = PriceIndexSeries(name="Nieuwbouwwoningen outputprijsindex bouwkosten")
        session.add(series)
        session.flush()
    series.description = "CBS Prijsindex bouwkosten excl. BTW, ingeladen uit het DeValk kengetallenbestand."
    series.source = path.name
    series.provider = "excel"
    series.period_field = "Periode"
    series.value_field = "Index"
    series.last_synced_at = datetime.now()
    series.values.clear()
    session.flush()

    count = 0
    for row in range(header_row + 1, sheet.max_row + 1):
        period = _text(sheet.cell(row=row, column=period_column).value)
        index_value = _decimal(sheet.cell(row=row, column=index_column).value)
        effective_date = _period_start(period)
        if not period or index_value is None or effective_date is None:
            continue
        session.add(
            PriceIndexValue(
                series_id=series.id,
                effective_date=effective_date,
                index_value=index_value,
                notes=period,
            )
        )
        count += 1
    return count


def _generic_reference_lines(path: Path, dataset: ReferenceDataset) -> list[ReferenceLine]:
    imported_lines = import_reference_lines(path)
    lines: list[ReferenceLine] = []
    for imported in imported_lines:
        if is_noise_line(imported.omschrijving_werkzaamheden):
            continue
        lines.append(
            ReferenceLine(
                dataset_id=dataset.id,
                line_number=len(lines) + 1,
                regel_type=imported.regel_type,
                niveau=imported.niveau,
                hoofdstuk_code=imported.hoofdstuk_code,
                hoofdstuk_omschrijving=imported.hoofdstuk_omschrijving,
                post_code=imported.post_code,
                project_name=imported.project_name,
                relation_name=imported.relation_name,
                document_date=imported.document_date,
                omschrijving_werkzaamheden=imported.omschrijving_werkzaamheden,
                hoeveelheid=imported.hoeveelheid,
                eenheid=imported.eenheid,
                norm_arbeid=imported.norm_arbeid,
                uren=imported.uren,
                materiaal=imported.materiaal,
                materieel=imported.materieel,
                onderaannemer=imported.onderaannemer,
                totaal_prijs_per_regel=imported.totaal_prijs_per_regel,
                eenheidsprijs=imported.eenheidsprijs,
                bron_pagina=imported.bron_pagina,
                confidence=imported.confidence,
                raw_text=imported.raw_text,
            )
        )
    return lines


def _find_index_sheet(sheets: list[Worksheet]) -> Worksheet | None:
    scored: list[tuple[int, Worksheet]] = []
    for sheet in sheets:
        score = 10 if "kengetallen" in sheet.title.lower() else 0
        for row in range(1, min(sheet.max_row, 80) + 1):
            labels = {_key(sheet.cell(row=row, column=column).value) for column in range(1, min(sheet.max_column, 45) + 1)}
            score += len(labels & META_HEADERS) * 3
            score += len(labels & set(CATEGORY_HEADERS)) * 2
        scored.append((score, sheet))
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1] if scored and scored[0][0] >= 20 else None


def _match_project_sheet(sheets: list[Worksheet], project_name: str, variant: str | None) -> str | None:
    candidates = [
        sheet
        for sheet in sheets
        if _key(sheet.title) not in {"indexen", "vormfactoren", "kengetallen geindexeerd", "kengetallen geïndexeerd"}
    ]
    project_key = _compact_key(project_name)
    variant_key = _compact_key(variant or "")
    best_score = 0
    best_title = None
    for sheet in candidates:
        title_key = _compact_key(sheet.title)
        score = 0
        if title_key and title_key in project_key:
            score += 50
        if project_key and project_key in title_key:
            score += 50
        if variant_key and variant_key in title_key:
            score += 15
        if score == 0:
            for row in range(1, min(sheet.max_row, 12) + 1):
                row_text = " ".join(_text(sheet.cell(row=row, column=column).value) for column in range(1, min(sheet.max_column, 12) + 1))
                row_key = _compact_key(row_text)
                if project_key and project_key in row_key:
                    score += 40
                    break
                if variant_key and variant_key in row_key:
                    score += 15
        if score > best_score:
            best_score = score
            best_title = sheet.title
    return best_title if best_score >= 20 else None


def _find_header_blocks(sheet: Worksheet) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    current_section = ""
    for row in range(1, min(sheet.max_row, 1000) + 1):
        columns: dict[str, int] = {}
        for column in range(1, sheet.max_column + 1):
            label = _key(sheet.cell(row=row, column=column).value)
            if label in META_HEADERS or label in CATEGORY_HEADERS:
                columns[label] = column
        category_count = len(set(columns) & set(CATEGORY_HEADERS))
        if "fase" in columns and "periode" in columns and category_count >= 4:
            project_column = max(1, columns["fase"] - 2)
            section = _text(sheet.cell(row=row, column=project_column).value) or current_section or f"Blok rij {row}"
            current_section = section
            blocks.append(
                {
                    "row": row,
                    "section": section,
                    "columns": columns,
                    "project_column": project_column,
                }
            )
        else:
            first_values = [_text(sheet.cell(row=row, column=column).value) for column in range(1, min(sheet.max_column, 5) + 1)]
            first_text = next((value for value in first_values if value), "")
            if first_text and first_text.lower() not in SUMMARY_LABELS:
                current_section = first_text
    return blocks


def _key(value: Any) -> str:
    return re.sub(r"\s+", " ", _text(value).lower().replace("bdb-", "bdb")).strip(" :")


def _compact_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", _text(value).lower())


def _cell(sheet: Worksheet, row: int, column: int | None) -> Any:
    if not column:
        return None
    return sheet.cell(row=row, column=column).value


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | float):
        return Decimal(str(value))
    text = str(value).strip().lower()
    if text in {"x", "-", "n.v.t.", "nvt"}:
        return None
    cleaned = text.replace("€", "").replace(" ", "")
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _date(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = _text(value)
    for pattern in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%y"):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    return None


def _period_start(period: str) -> datetime | None:
    match = re.search(r"(\d{4})\s*[-_ ]?\s*q([1-4])", period.lower())
    if match:
        year = int(match.group(1))
        quarter = int(match.group(2))
        return datetime(year, 1 + (quarter - 1) * 3, 1)
    return _date(period)


if __name__ == "__main__":
    main()
