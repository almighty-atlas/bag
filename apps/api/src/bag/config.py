from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BAG_", env_file=".env", extra="ignore")

    database_url: SecretStr
    user_name: str = Field(default="My Bag", min_length=1, max_length=200)
    max_request_bytes: int = Field(default=1_048_576, ge=1024)
    max_upload_bytes: int = Field(default=52_428_800, ge=1)
    storage_path: Path = Path("./data/storage")
    worker_port: int = Field(default=8001, ge=1, le=65535)
    worker_poll_seconds: float = Field(default=1.0, ge=0.01, le=60)
    job_lease_seconds: int = Field(default=300, ge=1, le=86_400)
    job_max_attempts: int = Field(default=5, ge=1, le=100)
    job_retry_seconds: float = Field(default=5.0, ge=0, le=3600)
    trash_retention_days: int = Field(default=30, ge=0, le=36_500)
    fetch_urls: bool = True
    fetch_max_bytes: int = Field(default=5_242_880, ge=1024)
    fetch_timeout_seconds: float = Field(default=10.0, ge=0.1, le=300)
    fetch_max_redirects: int = Field(default=5, ge=0, le=20)
    cookie_secure: bool = True
    session_days: int = Field(default=30, ge=1, le=365)
    login_max_failures: int = Field(default=10, ge=1, le=1000)
    login_window_seconds: float = Field(default=900.0, ge=1, le=86_400)
    job_retention_days: int = Field(default=7, ge=0, le=3650)
