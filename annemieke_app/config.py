import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent


class Settings:
    app_name: str = os.getenv("APP_NAME", "DeValk advies Begrotingsparser")
    database_url: str = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'database' / 'devalkadvies.db'}")
    upload_dir: Path = Path(os.getenv("UPLOAD_DIR", BASE_DIR / "uploads"))


settings = Settings()
