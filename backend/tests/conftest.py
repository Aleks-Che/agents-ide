from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agents_ide.api.app import create_app
from agents_ide.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        port=18765,
        frontend_dir=tmp_path / "frontend",
        dev_origin="http://127.0.0.1:5173",
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url=settings.origin) as client:
        yield client


@pytest.fixture
def authenticated(client: TestClient, settings: Settings) -> tuple[TestClient, dict[str, str]]:
    code = (settings.data_dir / "runtime/pair-code").read_text()
    response = client.post(
        "/api/auth/pair", json={"code": code}, headers={"Origin": settings.origin}
    )
    assert response.status_code == 200
    return client, {"Origin": settings.origin, "X-CSRF-Token": response.json()["csrf_token"]}
