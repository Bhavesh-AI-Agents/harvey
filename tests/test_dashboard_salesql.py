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


def test_the_daily_cap_can_be_changed_here(client):
    assert client.post("/api/salesql/cap", json={"cap": 60}).json() == {"success": True, "cap": 60}
    assert client.get("/api/salesql").json()["cap"] == 60
    for bad in ({"cap": -1}, {"cap": 5000}, {"cap": "60"}, {}, []):
        assert client.post("/api/salesql/cap", json=bad).status_code == 400
    assert client.get("/api/salesql").json()["cap"] == 60


def test_search_settings_are_shown_and_saved_from_the_card(client):
    shown = client.get("/api/salesql").json()
    assert shown["search"]["job_titles"] and "1-10" in shown["sizes"]
    r = client.post("/api/salesql/settings", json={
        "job_titles": ["Owner"], "states": ["California"], "company_sizes": ["1-10"],
        "page_size": 100, "require_no_developers": True})
    assert r.status_code == 200 and r.json()["search"]["states"] == ["California"]
    again = client.get("/api/salesql").json()["search"]
    assert again["job_titles"] == ["Owner"] and again["page_size"] == 100


def test_bad_search_settings_are_refused_with_a_reason(client):
    r = client.post("/api/salesql/settings", json={"company_sizes": ["tiny"]})
    assert r.status_code == 400 and "Company sizes" in r.json()["message"]
    assert client.post("/api/salesql/settings", json={"job_titles": []}).status_code == 400


def test_typing_offers_salesqls_own_names(client, monkeypatch):
    monkeypatch.setenv("SALESQL_API_KEY", "sq_abc")

    async def fake_lookup(self, kind, q="", size=20):
        assert (kind, q) == ("region", "calif")
        return [{"value": "r-ca", "label": "California"}, {"value": "r-bc", "label": "Baja California"},
                {"value": "r-bc2", "label": "Baja California"}]
    monkeypatch.setattr(sq.SalesQL, "lookup", fake_lookup)
    r = client.post("/api/salesql/lookup", json={"kind": "region", "q": "calif"}).json()
    assert r["values"] == ["California", "Baja California"]


@pytest.mark.parametrize("body", [{"kind": "city", "q": "bo"}, {"kind": "region", "q": "b"},
                                  {"kind": "region"}, "x"])
def test_a_lookup_needs_a_known_list_and_two_letters(client, monkeypatch, body):
    monkeypatch.setenv("SALESQL_API_KEY", "sq_abc")
    assert client.post("/api/salesql/lookup", json=body).status_code == 400


def test_a_lookup_needs_salesql_connected(client):
    r = client.post("/api/salesql/lookup", json={"kind": "region", "q": "calif"})
    assert r.status_code == 400 and "Connect" in r.json()["message"]


def test_the_lists_are_served_and_collecting_starts_once_connected(client, monkeypatch):
    from harvey.collectors import salesql as collector
    started = []

    async def fake_collect(state, key, **kw):
        started.append(key)
        return {"lookups": 0, "complete": False, "stopped": "The rest is collected tomorrow."}
    monkeypatch.setattr(collector, "collect_catalogs", fake_collect)
    monkeypatch.setattr(dash, "_catalog_task", None)
    monkeypatch.setattr(dash, "_catalog_retry_at", 0.0)
    first = client.get("/api/salesql/catalog").json()
    assert first["collecting"] is False and not started            # no key yet
    assert first["lists"]["industry"] == {"values": [], "complete": False}
    monkeypatch.setenv("SALESQL_API_KEY", "sq_abc")
    assert client.get("/api/salesql/catalog").json()["collecting"] is True
    import time
    for _ in range(50):
        if dash._catalog_task.done():
            break
        time.sleep(0.02)
    after = client.get("/api/salesql/catalog").json()
    assert started == ["sq_abc"]                                   # not started again at once
    assert after["collecting"] is False and "tomorrow" in after["note"]
