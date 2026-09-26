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
