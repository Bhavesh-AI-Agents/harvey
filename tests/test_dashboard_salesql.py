"""The Discover tab's SalesQL card: connect a key, search for free, reveal on a tick."""

import pytest

import harvey.dashboard as dash
from harvey.integrations import salesql as sq
from tests.test_dashboard_live_status import client, env  # noqa: F401  (fixtures)

ACCOUNT = {"plan": "Free", "credits": 50, "monthly_included": 50,
           "credits_refresh_date": "2026-12-17", "searches_per_day": 25, "reveals_per_day": 25}


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    monkeypatch.delenv("SALESQL_API_KEY", raising=False)
    sq._REJECTED.clear()


def test_without_a_key_the_card_offers_to_connect(client):
    data = client.get("/api/salesql").json()
    assert data["connected"] is False and data["candidates"] == []
    assert data["key_url"] == sq.KEY_URL


def test_connecting_checks_the_key_then_saves_it(client, monkeypatch):
    seen = {}

    async def fake_status(self):
        seen["key"] = self._key
        return ACCOUNT
    monkeypatch.setattr(sq.SalesQL, "account_status", fake_status)
    written = {}
    monkeypatch.setattr(dash, "_write_env_file", lambda u: written.update(u))
    r = client.post("/api/salesql/connect", json={"key": "  sq_abc  "}).json()
    assert r["success"] and "50 credits" in r["message"]
    assert seen["key"] == "sq_abc" and written == {"SALESQL_API_KEY": "sq_abc"}


def test_a_rejected_key_is_not_saved(client, monkeypatch):
    async def reject(self):
        raise sq.SalesQLError(sq.KEY_REJECTED)
    monkeypatch.setattr(sq.SalesQL, "account_status", reject)
    written = {}
    monkeypatch.setattr(dash, "_write_env_file", lambda u: written.update(u))
    r = client.post("/api/salesql/connect", json={"key": "sq_bad"})
    assert r.status_code == 400 and "rejected" in r.json()["message"] and not written


@pytest.mark.parametrize("body", [{}, {"key": ""}, {"key": 5}, {"key": "x" * 600}, []])
def test_a_missing_key_is_refused(client, body):
    assert client.post("/api/salesql/connect", json=body).status_code == 400


def test_search_and_reveal_need_a_connection(client):
    assert client.post("/api/salesql/search").status_code == 400
    assert client.post("/api/salesql/reveal", json={"ids": ["a"]}).status_code == 400


@pytest.mark.parametrize("body", [{}, {"ids": []}, {"ids": "a"}, {"ids": [5]},
                                  {"ids": ["x" * 65]}, {"ids": ["a"] * 101}])
def test_reveal_refuses_a_bad_selection_before_touching_salesql(client, monkeypatch, body):
    monkeypatch.setenv("SALESQL_API_KEY", "sq_abc")
    assert client.post("/api/salesql/reveal", json=body).status_code == 400


def test_the_card_lists_who_was_found(client, env, monkeypatch):
    import asyncio

    from harvey.state import StateManager

    async def seed():
        state = StateManager(db_path=str(env["db"]))
        await state.upsert_salesql_candidate({
            "id": "p-1", "full_name": "Ada Example", "title": "Founder",
            "org_name": "Northvale Payments", "grade": "qualified",
            "reason": "No software job titles found at the company."})
        await state.upsert_salesql_candidate({"id": "p-2", "full_name": "Ben Sample",
                                              "grade": "rejected", "reason": "Looks technical."})
        await state.update_salesql_candidate("p-2", status="revealed")
    asyncio.run(seed())

    async def fake_status(self):
        return ACCOUNT
    monkeypatch.setattr(sq.SalesQL, "account_status", fake_status)
    monkeypatch.setenv("SALESQL_API_KEY", "sq_abc")
    data = client.get("/api/salesql").json()
    assert data["connected"] and data["account"]["credits"] == 50
    assert [c["id"] for c in data["candidates"]] == ["p-1"]
    assert [c["id"] for c in data["handled"]] == ["p-2"]
