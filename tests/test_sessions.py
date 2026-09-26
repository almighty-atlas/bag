import io

import pytest
from bag.api import create_app
from bag.cli import main
from bag.config import Settings
from bag.db import connection
from bag.ids import uuid7
from bag.sessions import LoginError, login, set_password
from bag.tokens import TokenAdminError
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration
CSRF = {"X-Bag-Csrf": "1"}
PASSWORD = "korrekt-pferd-batterie-heftklammer"


def test_login_cookie_csrf_and_logout(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
) -> None:
    saved = client.post("/api/v1/capture/text", json={"content": "mine"}, headers=headers).json()
    # No password yet: login is impossible, tokens still work.
    assert (
        client.post(
            "/api/v1/session", json={"username": "atlas", "password": PASSWORD}, headers=CSRF
        ).status_code
        == 401
    )
    set_password(settings, " atlas ", PASSWORD, None)
    with connection(settings) as conn:
        row = conn.execute('SELECT username, password_hash FROM "user"').fetchone()
        assert row is not None and row["username"] == "atlas"
        assert (
            row["password_hash"].startswith("$argon2id$") and PASSWORD not in row["password_hash"]
        )

    # Login needs the CSRF header too, and wrong credentials are indistinguishable.
    body = {"username": "atlas", "password": PASSWORD}
    assert client.post("/api/v1/session", json=body).status_code == 403
    for wrong in (
        {"username": "atlas", "password": "x" * 10},
        {"username": "nobody", "password": PASSWORD},
    ):
        response = client.post("/api/v1/session", json=wrong, headers=CSRF)
        assert response.status_code == 401 and response.json() == {
            "detail": "Invalid username or password"
        }
    response = client.post("/api/v1/session", json=body, headers=CSRF)
    assert response.status_code == 200, response.text
    assert response.json()["username"] == "atlas" and response.json()["via"] == "session"
    assert response.json()["expires_at"] is not None
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/api" in cookie
    assert "Secure" not in cookie  # test settings speak plain http
    assert PASSWORD not in cookie

    # The cookie authenticates reads; writes additionally need the CSRF header.
    assert client.get("/api/v1/session").json()["via"] == "session"
    assert client.get(f"/api/v1/items/{saved['id']}").status_code == 200
    assert client.post("/api/v1/capture/text", json={"content": "web"}).status_code == 403
    web = client.post("/api/v1/capture/text", json={"content": "web"}, headers=CSRF)
    assert web.status_code == 201
    assert client.delete(f"/api/v1/items/{web.json()['id']}").status_code == 403
    assert client.delete(f"/api/v1/items/{web.json()['id']}", headers=CSRF).status_code == 204
    # Bearer wins when both are present, and reports its own kind.
    assert client.get("/api/v1/session", headers=headers).json()["via"] == "token"

    assert client.delete("/api/v1/session", headers=CSRF).status_code == 204
    assert client.get("/api/v1/session").status_code == 401
    assert client.get(f"/api/v1/items/{saved['id']}").status_code == 401
    # A revoked or unknown cookie is rejected; changing the password signs everyone out.
    client.post("/api/v1/session", json=body, headers=CSRF)
    assert client.get("/api/v1/session").status_code == 200
    set_password(settings, "atlas", PASSWORD + "!", None)
    assert client.get("/api/v1/session").status_code == 401
    client.cookies.set("bag_session", "forged")
    assert client.get("/api/v1/session").status_code == 401


def test_session_expiry_and_secure_flag(settings: Settings, client: TestClient, token: str) -> None:
    set_password(settings, "atlas", PASSWORD, None)
    token, expires = login(settings, "atlas", PASSWORD)
    with connection(settings) as conn:
        conn.execute("UPDATE session SET expires_at = now() - interval '1 second'")
    client.cookies.set("bag_session", token)
    assert client.get("/api/v1/session").status_code == 401
    with pytest.raises(LoginError):
        login(settings, "atlas", "wrong password!")
    with TestClient(create_app(settings.model_copy(update={"cookie_secure": True}))) as secure:
        response = secure.post(
            "/api/v1/session", json={"username": "atlas", "password": PASSWORD}, headers=CSRF
        )
        assert "Secure" in response.headers["set-cookie"]


def test_token_management_requires_session(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
) -> None:
    assert client.get("/api/v1/tokens", headers=headers).status_code == 403
    assert client.post("/api/v1/tokens", json={"name": "x"}, headers=headers).status_code == 403
    set_password(settings, "atlas", PASSWORD, None)
    client.post("/api/v1/session", json={"username": "atlas", "password": PASSWORD}, headers=CSRF)
    listed = client.get("/api/v1/tokens").json()
    assert [row["name"] for row in listed] == ["initial"] and "token" not in listed[0]
    assert "token_hash" not in listed[0]
    created = client.post("/api/v1/tokens", json={"name": "laptop"}, headers=CSRF)
    assert created.status_code == 201 and created.json()["token"]
    fresh = {"Authorization": "Bearer " + created.json()["token"]}
    assert client.get("/api/v1/items", headers=fresh).status_code == 200
    assert client.post("/api/v1/tokens", json={"name": ""}, headers=CSRF).status_code == 422
    assert client.delete(f"/api/v1/tokens/{created.json()['id']}").status_code == 403
    assert client.delete(f"/api/v1/tokens/{created.json()['id']}", headers=CSRF).status_code == 204
    assert client.get("/api/v1/items", headers=fresh).status_code == 401
    assert client.delete(f"/api/v1/tokens/{uuid7()}", headers=CSRF).status_code == 404
    # Another owner's token is invisible.
    other = uuid7()
    with connection(settings) as conn:
        conn.execute('INSERT INTO "user" (id, display_name) VALUES (%s, %s)', (other, "other"))
        conn.execute(
            "INSERT INTO api_token (id, owner_id, name, token_hash) VALUES (%s, %s, 'o', 'h')",
            (uuid7(), other),
        )
        foreign = conn.execute("SELECT id FROM api_token WHERE owner_id = %s", (other,)).fetchone()
        assert foreign is not None
    assert client.delete(f"/api/v1/tokens/{foreign['id']}", headers=CSRF).status_code == 404
    assert len(client.get("/api/v1/tokens").json()) == 2


def test_password_cli_and_validation(
    settings: Settings,
    token: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for username, password in (
        ("", PASSWORD),
        ("a b", PASSWORD),
        ("atlas", "short"),
        ("atlas", "nul\x00" * 5),
    ):
        with pytest.raises(TokenAdminError):
            set_password(settings, username, password, None)
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    monkeypatch.setattr("sys.argv", ["bag", "password", "set", "--username", "atlas", "--stdin"])
    main()
    out = capsys.readouterr()
    assert "Password set" in out.out and PASSWORD not in out.out + out.err
    assert login(settings, "atlas", PASSWORD)[0]
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1 and "short" not in capsys.readouterr().err
    other = uuid7()
    with connection(settings) as conn:
        conn.execute('INSERT INTO "user" (id, display_name) VALUES (%s, %s)', (other, "other"))
    with pytest.raises(TokenAdminError, match="already belongs"):
        set_password(settings, "atlas", PASSWORD, other)
