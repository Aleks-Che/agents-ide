import time
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from sqlalchemy import text

from agents_ide.api.app import create_app


def test_pairing_is_single_use_and_cookie_is_local(client, settings):
    code_file = settings.data_dir / "runtime/pair-code"
    code = code_file.read_text()
    started_at = time.time()
    response = client.post(
        "/api/auth/pair", json={"code": code}, headers={"Origin": settings.origin}
    )
    paired_at = time.time()
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/api" in cookie
    assert "Domain=" not in cookie and "Secure" not in cookie
    assert "Max-Age=5184000" in cookie
    assert not code_file.exists()
    assert client.get("/api/auth/session").status_code == 200
    assert (
        client.post(
            "/api/auth/pair", json={"code": code}, headers={"Origin": settings.origin}
        ).status_code
        == 401
    )
    with client.app.state.engine.connect() as connection:
        row = connection.execute(text("SELECT * FROM auth_sessions")).mappings().one()
    assert row["token_hash"] != client.cookies.get("agents_ide_session")
    assert started_at + 60 * 24 * 60 * 60 <= row["expires_at"] <= paired_at + 60 * 24 * 60 * 60


def test_mutation_requires_csrf_and_revoked_cookie_fails(authenticated):
    client, headers = authenticated
    cookie = client.cookies.get("agents_ide_session")
    assert client.post("/api/auth/logout", headers={"Origin": headers["Origin"]}).status_code == 403
    assert client.post("/api/auth/logout", headers=headers).status_code == 204
    client.cookies.set("agents_ide_session", cookie)
    assert client.get("/api/auth/session").status_code == 401


def test_exact_host_origin_and_error_envelope(client, settings):
    for headers, status in [
        ({"Host": "evil.test"}, 400),
        ({"Host": "127.0.0.1:9999"}, 400),
        ({"Origin": "http://evil.test"}, 403),
        ({"Origin": settings.origin + ".evil.test"}, 403),
        ({"Sec-Fetch-Site": "cross-site"}, 403),
    ]:
        result = client.get("/api/health", headers=headers)
        assert result.status_code == status
        assert set(result.json()) == {"code", "message", "details", "request_id", "retryable"}
        assert result.headers["x-request-id"] == result.json()["request_id"]
    assert client.post("/api/auth/pair", json={"code": "x"}).status_code == 403
    assert client.get("/api/system/status").status_code == 401
    assert client.get("/api/openapi.json").status_code == 401
    assert client.get("/api/health").json() == {"status": "ok"}


def test_expiry_attempt_limit_and_cleanup(client, settings):
    engine = client.app.state.engine
    for _attempt in range(5):
        with engine.begin() as connection:
            connection.execute(text("UPDATE pairing SET next_attempt_at=0"))
        assert (
            client.post(
                "/api/auth/pair", json={"code": "incorrect"}, headers={"Origin": settings.origin}
            ).status_code
            == 401
        )
    assert not (settings.data_dir / "runtime/pair-code").exists()
    code = client.app.state.auth.issue_code(rotate=True)
    with engine.begin() as connection:
        connection.execute(text("UPDATE pairing SET expires_at=0"))
    assert (
        client.post(
            "/api/auth/pair", json={"code": code}, headers={"Origin": settings.origin}
        ).status_code
        == 401
    )
    assert not (settings.data_dir / "runtime/pair-code").exists()


def test_rate_limit_and_validation_do_not_echo_input(client, settings):
    headers = {"Origin": settings.origin}
    client.post("/api/auth/pair", json={"code": "wrong"}, headers=headers)
    assert client.post("/api/auth/pair", json={"code": "wrong"}, headers=headers).status_code == 429
    result = client.post("/api/auth/pair", json={"code": {"secret": "DO-NOT-LOG"}}, headers=headers)
    assert result.status_code == 422
    assert "DO-NOT-LOG" not in result.text


def test_session_survives_api_restart_and_expires(client, settings):
    code = (settings.data_dir / "runtime/pair-code").read_text()
    client.post("/api/auth/pair", json={"code": code}, headers={"Origin": settings.origin})
    cookie = client.cookies.get("agents_ide_session")
    with TestClient(create_app(settings), base_url=settings.origin) as second:
        second.cookies.set("agents_ide_session", cookie)
        assert second.get("/api/auth/session").status_code == 200
        with second.app.state.engine.begin() as connection:
            connection.execute(
                text("UPDATE auth_sessions SET expires_at=:now"), {"now": time.time() - 1}
            )
        assert second.get("/api/auth/session").status_code == 401


def test_pairing_race_issues_only_one_session(client, settings):
    code = (settings.data_dir / "runtime/pair-code").read_text()

    def pair():
        return client.post(
            "/api/auth/pair", json={"code": code}, headers={"Origin": settings.origin}
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: pair(), range(2))) == [200, 401]
