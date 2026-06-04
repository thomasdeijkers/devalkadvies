from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def ensure_storage() -> None:
    if settings.database_url.startswith("sqlite:///"):
        db_path = Path(settings.database_url.replace("sqlite:///", "", 1))
        db_path.parent.mkdir(parents=True, exist_ok=True)
    settings.upload_dir.mkdir(parents=True, exist_ok=True)


def create_db() -> None:
    ensure_storage()
    Base.metadata.create_all(bind=engine)
    _apply_lightweight_migrations()


def _apply_lightweight_migrations() -> None:
    inspector = inspect(engine)
    if "incoming_documents" not in inspector.get_table_names():
        return

    document_columns = {column["name"] for column in inspector.get_columns("incoming_documents")}
    if "project_name" not in document_columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE incoming_documents ADD COLUMN project_name VARCHAR(180)"))
    if "project_id" not in document_columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE incoming_documents ADD COLUMN project_id INTEGER"))

    if "budget_lines" in inspector.get_table_names():
        budget_line_columns = {column["name"] for column in inspector.get_columns("budget_lines")}
        budget_line_migrations = {
            "price_index_series_id": "ALTER TABLE budget_lines ADD COLUMN price_index_series_id INTEGER",
            "base_price_date": "ALTER TABLE budget_lines ADD COLUMN base_price_date TIMESTAMP",
            "indexed_eenheidsprijs": "ALTER TABLE budget_lines ADD COLUMN indexed_eenheidsprijs NUMERIC(14, 2)",
            "indexed_totaal_prijs_per_regel": (
                "ALTER TABLE budget_lines ADD COLUMN indexed_totaal_prijs_per_regel NUMERIC(14, 2)"
            ),
        }
        for column_name, statement in budget_line_migrations.items():
            if column_name not in budget_line_columns:
                with engine.begin() as connection:
                    connection.execute(text(statement))

    if "price_index_series" in inspector.get_table_names():
        series_columns = {column["name"] for column in inspector.get_columns("price_index_series")}
        series_migrations = {
            "provider": "ALTER TABLE price_index_series ADD COLUMN provider VARCHAR(60) DEFAULT 'manual'",
            "api_url": "ALTER TABLE price_index_series ADD COLUMN api_url TEXT",
            "period_field": "ALTER TABLE price_index_series ADD COLUMN period_field VARCHAR(120)",
            "value_field": "ALTER TABLE price_index_series ADD COLUMN value_field VARCHAR(120)",
            "last_synced_at": "ALTER TABLE price_index_series ADD COLUMN last_synced_at TIMESTAMP",
        }
        for column_name, statement in series_migrations.items():
            if column_name not in series_columns:
                with engine.begin() as connection:
                    connection.execute(text(statement))


def get_session() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
