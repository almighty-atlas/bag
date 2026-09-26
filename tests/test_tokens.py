import json
from uuid import UUID, uuid4

import pytest
from bag.auth import token_hash
from bag.cli import initialize, main
from bag.config import Settings
from bag.db import connection
from bag.ids import uuid7
from bag.tokens import TokenAdminError, create_token, list_tokens, revoke_token
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


def test_create_list_revoke_recover_preserves_originals(
    settings: Settings,
    client: TestClient,
    headers: dict[str, str],
) -> None:
    saved = client.post(
        "/api/v1/capture/text", headers=headers, json={"content": "precious"}
    ).json()
    initial = list_tokens(settings)[0]
    info, secret = create_token(settings, "Laptop 💼")
    assert info.id.version == 7 and info.owner_id == initial.owner_id
    assert secret.get_secret_value() not in repr(secret)
    with connection(settings) as conn:
        row = conn.execute(
            "SELECT token_hash FROM api_token WHERE owner_id = %s AND id = %s",
            (info.owner_id, info.id),
        ).fetchone()
        assert row == {"token_hash": token_hash(secret.get_secret_value())}
    new_headers = {"Authorization": "Bearer " + secret.get_secret_value()}
    assert client.get(f"/api/v1/items/{saved['id']}", headers=new_headers).status_code == 200
    assert next(row for row in list_tokens(settings) if row.id == info.id).last_used_at is not None
    revoke_token(settings, info.id)
    revoked_at = next(row for row in list_tokens(settings) if row.id == info.id).revoked_at
    revoke_token(settings, info.id)
    assert next(row for row in list_tokens(settings) if row.id == info.id).revoked_at == revoked_at
    assert client.get(f"/api/v1/items/{saved['id']}", headers=new_headers).status_code == 401
    assert client.get(f"/api/v1/items/{saved['id']}", headers=headers).status_code == 200
    revoke_token(settings, initial.id)
    recovered, recovery_secret = create_token(settings, "recovery")
    assert recovered.owner_id == initial.owner_id
    recovery_headers = {"Authorization": "Bearer " + recovery_secret.get_secret_value()}
    assert (
        client.get(f"/api/v1/items/{saved['id']}", headers=recovery_headers).json()["content"]
        == "precious"
    )
    assert initialize(settings) is None


def test_admin_owner_scoping_and_validation(settings: Settings) -> None:
    with pytest.raises(TokenAdminError, match="Not initialized"):
        create_token(settings, "client")
    assert initialize(settings)
    first = list_tokens(settings)[0]
    second_owner = uuid7()
    with connection(settings) as conn:
        conn.execute(
            'INSERT INTO "user" (id, display_name) VALUES (%s, %s)', (second_owner, "Other")
        )
    with pytest.raises(TokenAdminError, match="Multiple owners"):
        create_token(settings, "ambiguous")
    with pytest.raises(TokenAdminError, match="Multiple owners"):
        list_tokens(settings)
    with pytest.raises(TokenAdminError, match="not found"):
        revoke_token(settings, first.id, second_owner)
    assert list_tokens(settings, first.owner_id)[0].revoked_at is None
    assert list_tokens(settings, second_owner) == []
    with pytest.raises(TokenAdminError, match="Owner not found"):
        create_token(settings, "wrong", uuid4())
    for name in ("", "  ", "a" * 201, "line\nbreak", "\ud800"):
        with pytest.raises(TokenAdminError):
            create_token(settings, name, first.owner_id)


def test_cli_and_commit_failure_no_secret_output(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert initialize(settings)

    def invoke(*args: str) -> str:
        monkeypatch.setattr("sys.argv", ["bag", "token", *args])
        main()
        return capsys.readouterr().out

    created = json.loads(invoke("create", "--name", "CLI"))
    token_id = UUID(created["id"])
    listing = invoke("list")
    assert created["token"] not in listing and "token_hash" not in listing
    assert "Token revoked" in invoke("revoke", str(token_id))
    recovery = json.loads(invoke("recover"))
    assert recovery["owner_id"] == created["owner_id"]
    assert recovery["token"] != created["token"]
    with connection(settings) as conn:
        conn.execute("""
            CREATE FUNCTION test_reject_token() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'private SQL detail'; END $$;
            CREATE CONSTRAINT TRIGGER test_reject_token AFTER INSERT ON api_token
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION test_reject_token();
        """)
    before = len(list_tokens(settings))
    try:
        monkeypatch.setattr("sys.argv", ["bag", "token", "recover"])
        with pytest.raises(SystemExit) as error:
            main()
        assert error.value.code == 1
        output = capsys.readouterr()
        assert output.out == "" and "private SQL" not in output.err
        assert len(list_tokens(settings)) == before
    finally:
        with connection(settings) as conn:
            conn.execute(
                "DROP TRIGGER test_reject_token ON api_token; DROP FUNCTION test_reject_token()"
            )
