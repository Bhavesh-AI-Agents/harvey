"""Settings > Sending mailboxes: add, sign in with Google, limit, switch off, remove."""

import asyncio

import pytest

import harvey.dashboard as dash
from harvey import mailboxes as mb
from harvey.integrations import gmail
from harvey.state import StateManager
from tests.test_dashboard_live_status import client, env  # noqa: F401  (fixtures)

A = "ana@example.com"


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "MAILBOXES_FILE", tmp_path / "data" / "mailboxes.json")
    monkeypatch.setattr(mb, "TOKEN_DIR", tmp_path / "data" / "gmail_tokens")
    monkeypatch.setenv("GMAIL_CLIENT_ID", "cid")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "secret")
    monkeypatch.delenv("TRULYINBOX_API_KEY", raising=False)
    dash._oauth_pending.clear()
    dash._warmup_cache.clear()


def add(client, **body):
    return client.post("/api/mailboxes", json={"address": A, "name": "Ana Example", **body})


def state_of(url):
    return url.split("state=")[1].split("&")[0]


def test_an_empty_list_says_where_google_must_send_people_back(client):
    data = client.get("/api/mailboxes").json()
    assert data["mailboxes"] == [] and data["gmail_ready"] is True
    assert data["redirect_uri"].startswith("http://") and data["redirect_uri"].endswith("/")


def test_the_public_address_is_used_behind_a_proxy(client, monkeypatch):
    monkeypatch.setenv("HARVEY_PUBLIC_ORIGIN", "https://harvey.example")
    monkeypatch.setattr(dash, "BASE_PATH", "/harvey")
    assert dash._oauth_redirect(None) == "https://harvey.example/harvey/"


def test_adding_a_mailbox_starts_its_google_sign_in(client):
    r = add(client, daily_limit=20).json()
    assert r["success"] and r["auth_url"].startswith("https://accounts.google.com/")
    assert "login_hint=ana%40example.com" in r["auth_url"]
    box = client.get("/api/mailboxes").json()["mailboxes"][0]
    assert box["address"] == A and box["daily_limit"] == 20
    assert box["signed_in"] is False and box["enabled"] is True and box["warmup"] is None


def test_adding_needs_the_google_client_first(client, monkeypatch):
    monkeypatch.delenv("GMAIL_CLIENT_SECRET")
    r = add(client)
    assert r.status_code == 400 and "OAuth client" in r.json()["message"] and mb.load() == []


@pytest.mark.parametrize("body", [{"address": "nope"}, {"address": A, "daily_limit": 9999},
                                  {"address": A, "name": 5}, {"address": A, "enabled": "yes"}, []])
def test_a_bad_mailbox_is_refused(client, body):
    assert client.post("/api/mailboxes", json=body).status_code == 400 and mb.load() == []


def test_finishing_checks_the_state_once_and_saves_the_sign_in(client, monkeypatch):
    url = add(client).json()["auth_url"]
    seen = {}

    async def fake_finish(client_id, client_secret, code, redirect, address, token_file, **kw):
        seen.update(code=code, address=address, redirect=redirect)
        gmail._save_token({"refresh_token": "r"}, token_file)
        return True, f"{address} is signed in."
    monkeypatch.setattr(gmail, "finish_web_auth", fake_finish)
    body = {"code": "c0de", "state": state_of(url)}
    r = client.post("/api/mailboxes/finish", json=body).json()
    assert r["success"] and seen["address"] == A and seen["code"] == "c0de"
    assert seen["redirect"] in url.replace("%3A", ":").replace("%2F", "/")
    assert client.get("/api/mailboxes").json()["mailboxes"][0]["signed_in"] is True
    # The same state can't be used twice.
    assert client.post("/api/mailboxes/finish", json=body).status_code == 400


@pytest.mark.parametrize("body", [{}, {"code": "c", "state": "unknown"}, {"code": "", "state": "s"},
                                  {"code": "c" * 3000, "state": "s"}, {"code": 1, "state": 2}])
def test_an_unknown_or_malformed_return_is_refused(client, monkeypatch, body):
    async def never(*a, **k):
        raise AssertionError("Google must not be called")
    monkeypatch.setattr(gmail, "finish_web_auth", never)
    assert client.post("/api/mailboxes/finish", json=body).status_code == 400


def test_an_expired_sign_in_is_refused(client, monkeypatch):
    url = add(client).json()["auth_url"]
    token = state_of(url)
    address, redirect, _ = dash._oauth_pending[token]
    dash._oauth_pending[token] = (address, redirect, 0.0)
    r = client.post("/api/mailboxes/finish", json={"code": "c", "state": token})
    assert r.status_code == 400 and "expired" in r.json()["message"]


def test_a_failed_google_exchange_reports_why(client, monkeypatch):
    url = add(client).json()["auth_url"]

    async def wrong(*a, **k):
        return False, "You signed in as bo@example.com, not ana@example.com."
    monkeypatch.setattr(gmail, "finish_web_auth", wrong)
    r = client.post("/api/mailboxes/finish", json={"code": "c", "state": state_of(url)})
    assert r.status_code == 400 and "bo@example.com" in r.json()["message"]


def test_limit_name_date_and_the_switch_can_change(client):
    add(client)
    r = client.post("/api/mailboxes/update", json={
        "address": A, "name": "Ana E", "daily_limit": 12, "warmup_start": "", "enabled": False})
    assert r.json()["success"]
    box = client.get("/api/mailboxes").json()["mailboxes"][0]
    assert (box["name"], box["daily_limit"], box["warmup_start"], box["enabled"]) == (
        "Ana E", 12, "", False)
    assert box["cold_cap_today"] == 12
    bad = client.post("/api/mailboxes/update", json={"address": A, "daily_limit": -3})
    assert bad.status_code == 400
    assert client.post("/api/mailboxes/update",
                       json={"address": "nobody@example.com", "daily_limit": 3}).status_code == 400


def test_a_mailbox_with_queued_email_cannot_be_removed(client, env):
    add(client)

    async def queue():
        state = StateManager(db_path=str(env["db"]))
        await state.add_outbox_item(prospect_id="p1", to_email="p@co.example", subject="s",
                                    body="b", send_at="2026-01-01T00:00:00", from_address=A)
    asyncio.run(queue())
    r = client.post("/api/mailboxes/remove", json={"address": A})
    assert r.status_code == 409 and "Switch it off" in r.json()["message"]
    assert client.get("/api/mailboxes").json()["mailboxes"][0]["queued"] == 1


def test_removing_forgets_the_sign_in(client):
    add(client)
    gmail._save_token({"refresh_token": "r"}, mb.token_path(A))
    assert client.post("/api/mailboxes/remove", json={"address": A}).json()["success"]
    assert mb.load() == [] and not mb.token_path(A).exists()
    assert client.post("/api/mailboxes/remove", json={"address": A}).status_code == 400


def test_sign_in_again_for_a_listed_mailbox_only(client):
    add(client)
    r = client.post("/api/mailboxes/sign-in", json={"address": A}).json()
    assert r["success"] and "accounts.google.com" in r["auth_url"]
    assert client.post("/api/mailboxes/sign-in",
                       json={"address": "nobody@example.com"}).status_code == 400
