from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import requests
from sqlalchemy import delete
from sqlalchemy.orm import Session

from .models import PriceIndexSeries, PriceIndexValue


PERIOD_CANDIDATES = ("Perioden", "Periods", "periode", "period", "maand", "month", "date", "datum")
VALUE_CANDIDATES = ("Value", "value", "Waarde", "waarde", "Index", "index", "Indexcijfer", "index_value")


def sync_price_index_series(session: Session, series: PriceIndexSeries) -> int:
    if not series.api_url:
        return 0

    payload = _fetch_json(series.api_url)
    rows = _extract_rows(payload)
    values: list[PriceIndexValue] = []
    for row in rows:
        period = _first_value(row, series.period_field, PERIOD_CANDIDATES)
        value = _first_value(row, series.value_field, VALUE_CANDIDATES)
        date = _parse_period(period)
        index_value = _to_decimal(value)
        if not date or index_value is None:
            continue
        values.append(
            PriceIndexValue(
                series_id=series.id,
                effective_date=date,
                index_value=index_value,
                notes="api-sync",
            )
        )

    if not values:
        return 0

    session.execute(delete(PriceIndexValue).where(PriceIndexValue.series_id == series.id))
    session.add_all(values)
    series.last_synced_at = datetime.now(timezone.utc)
    session.commit()
    return len(values)


def _fetch_json(api_url: str) -> dict[str, Any]:
    url = api_url.strip()
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    return response.json()


def _extract_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(payload.get("value"), list):
        return [row for row in payload["value"] if isinstance(row, dict)]
    if isinstance(payload.get("results"), list):
        return [row for row in payload["results"] if isinstance(row, dict)]
    if isinstance(payload.get("data"), list):
        return [row for row in payload["data"] if isinstance(row, dict)]
    return []


def _first_value(row: dict[str, Any], configured: str | None, candidates: tuple[str, ...]) -> Any:
    if configured and configured in row:
        return row[configured]
    lowered = {key.lower(): key for key in row}
    for candidate in candidates:
        key = lowered.get(candidate.lower())
        if key:
            return row[key]
    return None


def _parse_period(value: Any) -> datetime | None:
    if value is None:
        return None
    raw = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m", "%YMM", "%Y %B", "%d-%m-%Y"):
        try:
            parsed = datetime.strptime(raw, fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    if len(raw) >= 6 and raw[:4].isdigit() and raw[4:6].isdigit():
        try:
            return datetime(int(raw[:4]), int(raw[4:6]), 1, tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    cleaned = str(value).strip().replace(" ", "")
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None
