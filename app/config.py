from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CERTGEN_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./certgen.db"
    storage_dir: Path = Path("./storage")
    max_recipients_per_job: int = 5000
    worker_threads: int = 4
    api_key: str = ""  # empty => auth disabled
    public_base_url: str = "http://localhost:8000"
    # When True the job runs inside the request (used by tests for determinism).
    eager: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
