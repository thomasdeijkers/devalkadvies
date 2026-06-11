import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent


def _app_name() -> str:
    value = os.getenv("APP_NAME", "De Valk Advies Controle Centre").strip()
    if value in {"DeValk advies Begrotingsparser", "DeValk advies begrotingsparser"}:
        return "De Valk Advies Controle Centre"
    return value or "De Valk Advies Controle Centre"


class Settings:
    app_name: str = _app_name()
    database_url: str = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'database' / 'devalkadvies.db'}")
    upload_dir: Path = Path(os.getenv("UPLOAD_DIR", BASE_DIR / "uploads"))


settings = Settings()
