"""Validated, shared configuration for API, worker and launcher."""

import os
from pathlib import Path
from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_data_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ["LOCALAPPDATA"]) / "AgentsIDE"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "agents-ide"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTS_IDE_", extra="ignore")

    data_dir: Path = Field(default_factory=default_data_dir)
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1024, le=65535)
    dev_origin: str | None = None
    frontend_dir: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parents[3] / "frontend/dist"
    )
    heartbeat_seconds: float = Field(default=5, ge=0.1, le=5)
    worker_stale_seconds: float = Field(default=15, ge=1, le=60)
    session_seconds: int = Field(default=43200, ge=1, le=43200)
    pairing_seconds: int = Field(default=300, ge=1, le=300)
    log_level: str = "INFO"

    @model_validator(mode="after")
    def validate_local(self) -> Self:
        if self.host != "127.0.0.1":
            raise ValueError("Only 127.0.0.1 is supported in v1")
        if self.dev_origin is not None:
            from urllib.parse import urlsplit

            origin = urlsplit(self.dev_origin)
            if (
                origin.scheme != "http"
                or origin.hostname not in {"127.0.0.1", "localhost"}
                or origin.username
                or origin.password
                or origin.path
                or origin.query
                or origin.fragment
                or not origin.port
            ):
                raise ValueError("dev_origin must be an exact loopback HTTP origin with port")
        self.data_dir = self.data_dir.expanduser().absolute()
        return self

    @property
    def origin(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def allowed_origins(self) -> set[str]:
        return {self.origin} | ({self.dev_origin} if self.dev_origin else set())

    @property
    def database_path(self) -> Path:
        return self.data_dir / "db/agents-ide.db"
