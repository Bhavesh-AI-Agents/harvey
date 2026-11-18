"""SalesQL: search is free, a reveal spends credits, and only for ticked people.

Everything runs against a stand-in MCP server. All people and companies are
invented and use reserved example domains.
"""

import json

import httpx
import pytest
import pytest_asyncio

from harvey.collectors import salesql as collector
from harvey.config import HarveyConfig
from harvey.integrations import salesql as sq
from harvey.signals import seed_signal_catalog
from harvey.state import StateManager

KEY = "sq_test_key"
US = "country-us"


def make_config(**salesql):
    return HarveyConfig(
        persona={"name": "H", "company": "A", "role": "B", "email": "h@example.com",
                 "linkedin": "", "tone": "t"},
        product={"name": "P", "description": "D", "pricing": "$1", "key_benefits": [],
                 "objection_responses": {}},
        icp={"industries": ["Financial Services"], "company_size": "2-50",
             "titles": ["Founder", "Owner"], "geography": ["US"]},
        salesql={"verify_emails": False, **salesql},
    )


def person(uuid, name, title, org_uuid, org_name, domain, *, headline="", size="1 - 10",
           emails=None):
    return {
        "uuid": uuid, "full_name": name, "title": title, "headline": headline or title,
        "linkedin_url": f"https://linkedin.com/in/{uuid}",
        "location": {"city": "Springfield", "state": "Ohio", "country": "United States"},
        "organization": ({"uuid": org_uuid, "name": org_name, "website_domain": domain,
                          "number_of_employees": size, "industry": "Financial Services"}
                         if org_uuid else {"uuid": None, "name": org_name}),
        "emails": emails if emails is not None
        else [{"email": f"...@{domain}", "type": "BE", "verified": True}],
        "n_work_emails": 1,
    }


PEOPLE = [
    person("p-ada", "Ada Example", "Founder & CEO", "o-1", "Northvale Payments", "northvale.example"),
    person("p-ben", "Ben Sample, MBA", "Owner", "o-2", "Lakeside Merchant Co", "lakeside.example"),
    person("p-cal", "Cal Tester", "Founder & CTO", "o-3", "Hillside Pay", "hillside.example"),
    person("p-dee", "Dee Placeholder", "Founder", None, "Stealth Venture", ""),
    person("p-eve", "Eve Invented", "Owner", "o-5", "Bigger Co", "bigger.example", size="51 - 200"),
]


class FakeSalesQL:
    """A stand-in MCP server: records every tool call it receives."""

    def __init__(self, *, credits=50, developers=None, enrich=None, stream=False,
                 status=200, dev_total=None):
        self.calls: list[tuple[str, dict]] = []
        self.credits = credits
        self.developers = developers or []      # people results for the developer check
        self.enrich = enrich
        self.stream = stream
        self.status = status
        self.dev_total = dev_total
        self.verify_status = "valid"            # None: the key lacks the Verify scope

    def tools(self, name):
        return [args for tool, args in self.calls if tool == name]

    def _result(self, name, args):
        params = args.get("params", {})
        if name == "get_account_status":
            return {"plan": "Free", "credits": self.credits, "monthly_included": 50,
                    "credits_refresh_date": "2026-12-17T08:00:00+01:00", "banned": False,
                    "rate_limits": {"search_people": {"daily": 25},
                                    "persons/enrich": {"daily": 25}}}
        if name == "verify_email":
            if self.verify_status is None:
                raise PermissionError
            return {"email": params["email"], "status": self.verify_status}
        if name == "lookup":
            if params["type"] == "country":
                return {"values": [{"value": US, "label": "United States"}]
                        if params.get("q") == "United States" else []}
            if params["type"] == "region":
                return {"values": [{"value": "r-bc", "label": "Baja California"},
                                   {"value": "r-ca", "label": "California"}]
                        if params.get("q") == "California" else []}
            if params["type"] == "industry":
                if getattr(self, "no_industry", False):
                    return {"values": []}
                return {"values": [{"value": "financial-services", "label": "Financial Services"}]}
            return {"values": []}
        if name == "search_organizations":
            return {"results": [{"uuid": "o-1", "name": "Northvale Payments"},
                                {"uuid": "o-2", "name": "Lakeside Merchant Co"}], "total": 2}
        if name == "search_people":
            if "past_job_titles" in params:      # the candidate search
                return {"results": PEOPLE, "total": 1234}
            total = self.dev_total if self.dev_total is not None else len(self.developers)
            return {"results": self.developers, "total": total}
        if name == "enrich_person_bulk":
            if self.enrich is not None:
                return self.enrich(params["persons"])
            out = []
            for req in params["persons"]:
                uuid = req["linkedin_url"].rsplit("/", 1)[1]
                src = next(p for p in PEOPLE if p["uuid"] == uuid)
                domain = src["organization"].get("website_domain") or "elsewhere.example"
                out.append({"uuid": uuid, "full_name": src["full_name"], "title": src["title"],
                            "emails": [
                                {"email": f"{uuid}@gmail.example", "type": "PE", "verified": True},
                                {"email": f"{uuid}@other.example", "type": "BE", "verified": True},
                                {"email": f"{uuid}@{domain}", "type": "BE", "verified": True}],
                            "phones": [{"phone": "+1 555 0100", "type": "Company"},
                                       {"phone": "+1 555 0142", "type": "Mobile", "is_valid": True}]})
            return {"results": out}
        raise AssertionError(name)

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {KEY}"
        assert str(request.url) == sq.MCP_URL
        if self.status != 200:
            return httpx.Response(self.status, json={})
        body = json.loads(request.content)
        if "id" not in body:
            return httpx.Response(202)
        if body["method"] == "initialize":
            result = {"protocolVersion": sq.PROTOCOL_VERSION, "capabilities": {}}
        else:
            name, args = body["params"]["name"], body["params"]["arguments"]
            self.calls.append((name, args))
            try:
                result = {"content": [{"type": "text",
                                       "text": json.dumps(self._result(name, args))}]}
            except PermissionError:
                result = {"isError": True, "content": [
                    {"type": "text", "text": "This key lacks the verify scope."}]}
        message = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        if self.stream:
            return httpx.Response(
                200, headers={"content-type": "text/event-stream", "mcp-session-id": "s1"},
                content=f"event: message\ndata: {json.dumps(message)}\n\n")
        return httpx.Response(200, json=message, headers={"mcp-session-id": "s1"})

    @property
    def transport(self):
        return httpx.MockTransport(self.handler)


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(db_path=str(tmp_path / "t.db"))
    await s.init_db()
    await seed_signal_catalog(s)
    return s


@pytest.fixture(autouse=True)
def _fresh_keys():
    sq._REJECTED.clear()
    yield
    sq._REJECTED.clear()


async def verified(email, env=None):
    return "verified"


# ── the client ──

@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_the_client_reads_json_and_event_stream_answers(stream):
    fake = FakeSalesQL(stream=stream)
    async with sq.SalesQL(KEY, transport=fake.transport) as client:
        account = await client.account_status()
    assert account["plan"] == "Free" and account["credits"] == 50
    assert account["credits_refresh_date"] == "2026-12-17" and account["reveals_per_day"] == 25


@pytest.mark.asyncio
async def test_a_rejected_key_is_not_sent_again():
    fake = FakeSalesQL(status=401)
    with pytest.raises(sq.SalesQLError, match="rejected"):
        async with sq.SalesQL(KEY, transport=fake.transport) as client:
            await client.account_status()
    fake.status = 200
    with pytest.raises(sq.SalesQLError, match="rejected"):
        async with sq.SalesQL(KEY, transport=fake.transport) as client:
            await client.account_status()
    assert not fake.calls


@pytest.mark.asyncio
async def test_a_rate_limit_says_so():
    fake = FakeSalesQL(status=429)
    with pytest.raises(sq.SalesQLError, match="limit"):
        async with sq.SalesQL(KEY, transport=fake.transport) as client:
            await client.account_status()


def test_a_key_that_cannot_be_a_header_is_refused():
    for bad in ("", "  ", "ke\ny", "clé"):
        with pytest.raises(sq.SalesQLError):
            sq.SalesQL(bad)


# ── grading ──

def test_company_sizes_from_the_icp():
    assert collector.sizes_for("2-50") == ["1-10", "11-50"]
    assert collector.sizes_for("under 200 employees") == ["1-10", "11-50", "51-200"]
    assert collector.sizes_for("500+") == ["201-500", "501-1000", "1001-5000", "5001-10000", "10001+"]
    assert collector.sizes_for("") == []


def test_technical_words_match_on_word_boundaries():
    words = ["engineer", "cto"]
    assert collector.technical_word("Founder & CTO", words) == "cto"
    assert collector.technical_word("Director of Engineering", words) == ""
    assert collector.technical_word("Doctor", words) == ""          # "cto" inside a word
    assert collector.technical_word("Sales Engineer", words) == "engineer"


def test_names_lose_post_nominals():
    assert collector.split_name("Ben Sample, MBA") == ("Ben", "Sample")
    assert collector.split_name("Ada  Example") == ("Ada", "Example")
    assert collector.split_name("Mononym") == ("Mononym", "")


def test_the_work_address_at_the_company_wins():
    profile = {"emails": [
        {"email": "a@gmail.example", "type": "PE", "verified": True},
        {"email": "a@other.example", "type": "BE", "verified": True},
        {"email": "a@northvale.example", "type": "BE", "verified": False},
        {"email": "...@northvale.example", "type": "BE", "verified": True},   # still masked
    ]}
    assert collector.pick_email(profile, "northvale.example") == ("a@northvale.example", False)
    assert collector.pick_email({"emails": [profile["emails"][0]]}, "x.example") == ("", False)


# ── search ──

@pytest.mark.asyncio
async def test_a_search_grades_everyone_and_spends_nothing(state):
    fake = FakeSalesQL(developers=[
        {"uuid": "d1", "organization": {"uuid": "o-2"}},
        {"uuid": "d2", "organization": {"uuid": "o-2"}}])
    summary = await collector.search(state, make_config(), KEY, transport=fake.transport)
    assert summary["found"] == 5 and summary["new"] == 5 and summary["total"] == 1234
    rows = {r["id"]: r for r in await state.get_salesql_candidates()}
    assert rows["p-ada"]["grade"] == "qualified" and rows["p-ada"]["dev_count"] == 0
    assert rows["p-ben"]["grade"] == "rejected" and "2 people" in rows["p-ben"]["reason"]
    assert rows["p-cal"]["grade"] == "rejected" and "cto" in rows["p-cal"]["reason"]
    assert rows["p-dee"]["grade"] == "unsure" and "no company record" in rows["p-dee"]["reason"]
    assert rows["p-eve"]["grade"] == "unsure" and "51-200" in rows["p-eve"]["reason"]
    assert not fake.tools("enrich_person_bulk")

    candidate_search = next(a["params"] for a in fake.tools("search_people")
                            if "past_job_titles" in a["params"])
    assert candidate_search["job_titles"] == {"include": ["Founder", "Owner"]}
    assert candidate_search["organization_employee_ranges"] == ["1-10", "11-50"]
    assert candidate_search["organization_countries"] == [US]
    assert candidate_search["organization_industries"] == ["financial-services"]
    assert candidate_search["has_work_emails"] is True
    # Only the two companies whose founder could still qualify were checked, in one search.
    dev_checks = [a["params"] for a in fake.tools("search_people")
                  if "past_job_titles" not in a["params"]]
    assert len(dev_checks) == 1 and dev_checks[0]["organization_uuids"] == ["o-1", "o-2"]


@pytest.mark.asyncio
async def test_a_cut_short_developer_check_leaves_companies_unsure(state):
    fake = FakeSalesQL(developers=[{"uuid": "d1", "organization": {"uuid": "o-2"}}],
                       dev_total=400)
    await collector.search(state, make_config(), KEY, transport=fake.transport)
    rows = {r["id"]: r for r in await state.get_salesql_candidates()}
    assert rows["p-ada"]["grade"] == "unsure" and rows["p-ada"]["dev_count"] is None
    assert rows["p-ben"]["grade"] == "rejected"


@pytest.mark.asyncio
async def test_lookups_are_cached(state):
    fake = FakeSalesQL()
    await collector.search(state, make_config(), KEY, transport=fake.transport)
    first = len(fake.tools("lookup"))
    await collector.search(state, make_config(), KEY, transport=fake.transport)
    assert first == 2 and len(fake.tools("lookup")) == first


@pytest.mark.asyncio
async def test_company_keywords_find_the_companies_first(state):
    fake = FakeSalesQL()
    config = make_config(company_keywords=["merchant services"])
    await collector.search(state, config, KEY, transport=fake.transport)
    org_search = fake.tools("search_organizations")[0]["params"]
    assert org_search["name"] == ["merchant services"] and org_search["countries"] == [US]
    people_search = next(a["params"] for a in fake.tools("search_people")
                         if "past_job_titles" in a["params"])
    assert people_search["organization_uuids"] == ["o-1", "o-2"]
    assert "organization_industries" not in people_search


@pytest.mark.asyncio
async def test_a_second_search_keeps_what_was_already_decided(state):
    fake = FakeSalesQL()
    await collector.search(state, make_config(), KEY, transport=fake.transport)
    await state.update_salesql_candidate("p-ada", status="revealed", prospect_id="x")
    summary = await collector.search(state, make_config(), KEY, transport=fake.transport)
    assert summary["new"] == 0
    row = (await state.get_salesql_candidates(ids=["p-ada"]))[0]
    assert row["status"] == "revealed" and row["prospect_id"] == "x"


# ── reveal ──

@pytest.mark.asyncio
async def test_revealing_makes_a_prospect_with_the_company_address(state):
    fake = FakeSalesQL()
    config = make_config()
    await collector.search(state, config, KEY, transport=fake.transport)
    summary = await collector.reveal(state, config, KEY, ["p-ada"], verify=verified,
                                     transport=fake.transport)
    assert summary["revealed"] == 1 and summary["credits_used"] == 1
    prospect = await state.get_prospect_by_email("p-ada@northvale.example")
    assert prospect.first_name == "Ada" and prospect.last_name == "Example"
    assert prospect.email_status == "verified" and prospect.status == "new"
    assert prospect.phone == "+1 555 0142" and prospect.phone_verified is True
    assert prospect.source == "salesql" and "No software job titles" in prospect.personalization_notes
    company = await state.get_company(prospect.company_id)
    assert company.domain == "northvale.example" and company.external_id == "salesql:o-1"
    assert await state.count_writable_prospects() == 1
    assert fake.tools("enrich_person_bulk")[0]["params"]["persons"] == [
        {"linkedin_url": "https://linkedin.com/in/p-ada"}]
    assert await collector.reveals_left_today(state, config) == 24


@pytest.mark.asyncio
async def test_the_daily_cap_and_the_credits_hold_people_back(state):
    fake = FakeSalesQL(credits=50)
    config = make_config(max_reveals_per_day=1)
    await collector.search(state, config, KEY, transport=fake.transport)
    summary = await collector.reveal(state, config, KEY, ["p-ada", "p-ben"], verify=verified,
                                     transport=fake.transport)
    assert summary["revealed"] == 1 and summary["held_back"] == 1
    assert "cap" in summary["messages"][0]
    assert len(fake.tools("enrich_person_bulk")[0]["params"]["persons"]) == 1
    # Tomorrow's allowance is untouched; today's is spent.
    again = await collector.reveal(state, config, KEY, ["p-ben"], verify=verified,
                                   transport=fake.transport)
    assert again["revealed"] == 0 and len(fake.tools("enrich_person_bulk")) == 1

    fake2 = FakeSalesQL(credits=0)
    out = await collector.reveal(state, make_config(), KEY, ["p-ben"], verify=verified,
                                 transport=fake2.transport)
    assert out["held_back"] == 1 and "credits" in out["messages"][0]
    assert not fake2.tools("enrich_person_bulk")


@pytest.mark.asyncio
async def test_an_unverifiable_address_is_never_writable_unless_salesql_verified_it(state):
    async def guess(email, env=None):
        return "guess"

    async def invalid(email, env=None):
        return "invalid"
    fake = FakeSalesQL()
    await collector.search(state, make_config(), KEY, transport=fake.transport)
    await collector.reveal(state, make_config(trust_verified=False), KEY, ["p-ada"],
                           verify=guess, transport=fake.transport)
    assert (await state.get_prospect_by_email("p-ada@northvale.example")).email_status == "guess"
    await collector.reveal(state, make_config(), KEY, ["p-ben"], verify=guess,
                           transport=fake.transport)
    assert (await state.get_prospect_by_email("p-ben@lakeside.example")).email_status == "verified"
    await collector.reveal(state, make_config(), KEY, ["p-cal"], verify=invalid,
                           transport=fake.transport)
    assert (await state.get_prospect_by_email("p-cal@hillside.example")).email_status == "invalid"
    assert await state.count_writable_prospects() == 1


@pytest.mark.asyncio
async def test_do_not_contact_is_honoured_before_any_credit_is_spent(state):
    fake = FakeSalesQL()
    config = make_config()
    config.compliance.do_not_contact = ["northvale.example"]
    await collector.search(state, config, KEY, transport=fake.transport)
    summary = await collector.reveal(state, config, KEY, ["p-ada"], verify=verified,
                                     transport=fake.transport)
    assert summary["skipped"] == 1 and summary["credits_used"] == 0
    assert not fake.tools("enrich_person_bulk")
    assert (await state.get_salesql_candidates(ids=["p-ada"]))[0]["status"] == "skipped"


@pytest.mark.asyncio
async def test_people_salesql_cannot_find_or_who_have_no_work_address(state):
    def enrich(persons):
        return {"results": [{"error": "not found"},
                            {"uuid": "p-ben", "full_name": "Ben Sample",
                             "emails": [{"email": "ben@gmail.example", "type": "PE"}]}]}
    fake = FakeSalesQL(enrich=enrich)
    config = make_config()
    await collector.search(state, config, KEY, transport=fake.transport)
    summary = await collector.reveal(state, config, KEY, ["p-ada", "p-ben"], verify=verified,
                                     transport=fake.transport)
    assert summary["failed"] == 1 and summary["no_email"] == 1 and summary["credits_used"] == 1
    assert await state.count_writable_prospects() == 0


@pytest.mark.asyncio
async def test_signals_are_recorded_only_once_confirmed(state):
    fake = FakeSalesQL()
    config = make_config()
    await collector.search(state, config, KEY, transport=fake.transport)
    await collector.reveal(state, config, KEY, ["p-ada"], verify=verified,
                           transport=fake.transport)
    assert not await state.get_observations(signal_code="INHOUSE_DEV_COUNT")
    await state.set_signal_status("INHOUSE_DEV_COUNT", "confirmed")
    await state.set_signal_status("FOUNDER_NON_TECHNICAL", "confirmed")
    await collector.search(state, config, KEY, transport=fake.transport)
    await collector.reveal(state, config, KEY, ["p-dee"], verify=verified,
                           transport=fake.transport)
    assert len(await state.get_observations(signal_code="FOUNDER_NON_TECHNICAL")) == 1
    # p-dee's company was never checked, so no developer count is claimed.
    assert not await state.get_observations(signal_code="INHOUSE_DEV_COUNT")


@pytest.mark.asyncio
async def test_a_failed_reveal_still_counts_against_today(state):
    def boom(persons):
        return "not json the client can use"
    fake = FakeSalesQL(enrich=boom)
    config = make_config()
    await collector.search(state, config, KEY, transport=fake.transport)
    with pytest.raises(sq.SalesQLError):
        await collector.reveal(state, config, KEY, ["p-ada", "p-ben"], verify=verified,
                               transport=fake.transport)
    assert await collector.reveals_left_today(state, config) == 23


@pytest.mark.asyncio
async def test_the_cap_set_on_the_dashboard_wins_over_the_file(state):
    config = make_config(max_reveals_per_day=25)
    assert await collector.daily_cap(state, config) == 25
    await collector.set_daily_cap(state, 60)
    assert await collector.daily_cap(state, config) == 60
    assert await collector.reveals_left_today(state, config) == 60
    for bad in (-1, 1001, True, "60", None, 2.5):
        with pytest.raises(ValueError):
            await collector.set_daily_cap(state, bad)
    await collector.set_daily_cap(state, 0)
    fake = FakeSalesQL()
    await collector.search(state, config, KEY, transport=fake.transport)
    out = await collector.reveal(state, config, KEY, ["p-ada"], verify=verified,
                                 transport=fake.transport)
    assert out["held_back"] == 1 and not fake.tools("enrich_person_bulk")


def test_the_phone_kept_is_a_real_valid_direct_number():
    pick = collector.pick_phone
    assert pick({}) == ("", False) and pick({"phones": "x"}) == ("", False)
    assert pick({"phones": ["+1 (555) 0100"]}) == ("+1 (555) 0100", False)
    assert pick({"phones": [{"phone": "+1 555 ***"}, {"number": "12"}]}) == ("", False)
    assert pick({"phones": [{"phone": "+1 555 0100", "type": "Mobile"},
                            {"phone": "+1 555 0199", "type": "Company", "is_valid": True}]}) \
        == ("+1 555 0199", True)
    assert pick({"phones": [{"phone": "+1 555 0100", "type": "Company"},
                            {"number": "+1 555 0142", "type": "Direct"}]}) == ("+1 555 0142", False)


async def _guess(email, env=None):
    return "guess"


@pytest.mark.asyncio
@pytest.mark.parametrize("answer,expect", [("valid", "verified"), ("invalid", "invalid"),
                                           ("catch_all", "risky"), ("unknown", "guess")])
async def test_salesql_checks_an_address_harveys_verifier_could_not(state, answer, expect):
    fake = FakeSalesQL()
    fake.verify_status = answer
    config = make_config(verify_emails=True)
    await collector.search(state, config, KEY, transport=fake.transport)
    out = await collector.reveal(state, config, KEY, ["p-ada"], verify=_guess,
                                 transport=fake.transport)
    assert out["verifications_used"] == 1
    assert fake.tools("verify_email") == [{"params": {"email": "p-ada@northvale.example"}}]
    assert (await state.get_prospect_by_email("p-ada@northvale.example")).email_status == expect


@pytest.mark.asyncio
async def test_no_salesql_check_when_harveys_verifier_already_answered(state):
    fake = FakeSalesQL()
    config = make_config(verify_emails=True)
    await collector.search(state, config, KEY, transport=fake.transport)
    out = await collector.reveal(state, config, KEY, ["p-ada"], verify=verified,
                                 transport=fake.transport)
    assert out["verifications_used"] == 0 and not fake.tools("verify_email")


@pytest.mark.asyncio
async def test_a_key_without_the_verify_scope_is_tried_once_and_says_so(state):
    fake = FakeSalesQL()
    fake.verify_status = None
    config = make_config(verify_emails=True)
    await collector.search(state, config, KEY, transport=fake.transport)
    out = await collector.reveal(state, config, KEY, ["p-ada", "p-ben"], verify=_guess,
                                 transport=fake.transport)
    assert out["revealed"] == 2 and out["verifications_used"] == 0
    assert len(fake.tools("verify_email")) == 1
    assert any("Verify" in m for m in out["messages"])
    # SalesQL's own mark still counts when its check couldn't run.
    assert (await state.get_prospect_by_email("p-ben@lakeside.example")).email_status == "verified"


@pytest.mark.asyncio
async def test_a_state_narrows_both_the_person_and_the_company(state):
    fake = FakeSalesQL()
    await collector.search(state, make_config(states=["California"]), KEY, transport=fake.transport)
    sent = fake.tools("search_people")[0]["params"]
    assert sent["organization_states_or_cities"] == ["r-ca"] and sent["states_or_cities"] == ["r-ca"]


@pytest.mark.asyncio
async def test_an_unknown_state_stops_the_search_instead_of_widening_it(state):
    fake = FakeSalesQL()
    with pytest.raises(sq.SalesQLError, match="not cities"):
        await collector.search(state, make_config(states=["Atlantis"]), KEY,
                               transport=fake.transport)
    assert not fake.tools("search_people")


@pytest.mark.asyncio
async def test_many_addresses_are_checked_in_one_reveal(state):
    fake = FakeSalesQL()
    config = make_config(verify_emails=True)
    await collector.search(state, config, KEY, transport=fake.transport)
    out = await collector.reveal(state, config, KEY, ["p-ada", "p-ben", "p-cal"], verify=_guess,
                                 transport=fake.transport)
    assert out["revealed"] == 3 and out["verifications_used"] == 3
    assert sorted(c["params"]["email"] for c in fake.tools("verify_email")) == [
        "p-ada@northvale.example", "p-ben@lakeside.example", "p-cal@hillside.example"]


@pytest.mark.asyncio
async def test_search_settings_saved_on_the_dashboard_replace_the_file(state):
    config = make_config()
    before = await collector.search_settings(state, config)
    assert before["job_titles"] == ["Founder", "Owner"] and before["states"] == []
    saved = await collector.set_search_settings(state, config, {
        "job_titles": ["Owner", " Owner ", "President"], "states": ["California"],
        "company_sizes": ["1-10"], "require_no_developers": False, "page_size": 50})
    assert saved["job_titles"] == ["Owner", "President"] and saved["page_size"] == 50
    fake = FakeSalesQL()
    await collector.search(state, config, KEY, transport=fake.transport)
    sent = fake.tools("search_people")[0]["params"]
    assert sent["job_titles"] == {"include": ["Owner", "President"]}
    assert sent["organization_employee_ranges"] == ["1-10"] and sent["page_size"] == 50
    assert sent["states_or_cities"] == ["r-ca"] and len(fake.tools("search_people")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    {"job_titles": []}, {"job_titles": "Owner"}, {"company_sizes": ["2-9"]},
    {"page_size": 500}, {"page_size": True}, {"require_no_developers": "yes"},
    {"states": ["x" * 81]}, "nope"])
async def test_search_settings_that_make_no_sense_are_refused(state, bad):
    with pytest.raises(ValueError):
        await collector.set_search_settings(state, make_config(), bad)
    assert await collector.search_settings(state, make_config()) \
        == await collector.search_settings(state, make_config())


@pytest.mark.asyncio
async def test_searching_again_reads_the_next_page(state):
    fake = FakeSalesQL()
    config = make_config(require_no_developers=False)
    await collector.search(state, config, KEY, transport=fake.transport)
    await collector.search(state, config, KEY, transport=fake.transport)
    assert [c["params"]["page"] for c in fake.tools("search_people")] == [1, 2]
    await collector.set_search_settings(state, config, {"job_titles": ["President"]})
    await collector.search(state, config, KEY, transport=fake.transport)
    assert fake.tools("search_people")[-1]["params"]["page"] == 1


@pytest.mark.asyncio
async def test_a_city_is_matched_against_each_persons_location(state):
    fake = FakeSalesQL()
    config = make_config(require_no_developers=False, cities=["springfield"])
    out = await collector.search(state, config, KEY, transport=fake.transport)
    assert out["found"] == len(PEOPLE) and not any("outside" in n for n in out["notes"])
    fake2 = FakeSalesQL()
    out = await collector.search(state, make_config(require_no_developers=False, cities=["Boston"]),
                                 KEY, transport=fake2.transport)
    assert out["found"] == 0 and any("outside Boston" in n for n in out["notes"])


@pytest.mark.asyncio
async def test_unknown_industries_stop_the_search(state):
    fake = FakeSalesQL()
    fake.no_industry = True
    with pytest.raises(sq.SalesQLError, match="LinkedIn's industry names"):
        await collector.search(state, make_config(industries=["merchant services"]), KEY,
                               transport=fake.transport)
    assert not fake.tools("search_people")


@pytest.mark.asyncio
async def test_saved_names_are_checked_with_salesql(state):
    fake = FakeSalesQL()
    config = make_config()
    await collector.set_search_settings(state, config, {
        "states": ["California", "Boston"], "countries": ["US"], "industries": ["finance"]})
    notes = await collector.check_names(state, config, KEY, transport=fake.transport)
    assert len(notes) == 2
    assert any("no state called \"Boston\"" in n and "Cities" in n for n in notes)
    assert any("\"finance\" will be read as \"Financial Services\"" in n for n in notes)


class ListServer(FakeSalesQL):
    """A SalesQL whose lookup matches the start of any word, 100 names at most."""

    def __init__(self, names, **kw):
        super().__init__(**kw)
        self.names = names
        self.per_day = 1000

    def _result(self, name, args):
        if name == "get_account_status":
            return {"plan": "Basic", "credits": 2000, "banned": False,
                    "rate_limits": {"lookup": {"daily": self.per_day, "minute": 20}}}
        if name == "lookup":
            q = args["params"]["q"].lower()
            hits = [n for n in self.names[args["params"]["type"]]
                    if any(word.startswith(q) for word in n.lower().split())]
            return {"values": [{"value": "v-" + n.lower(), "label": n} for n in hits[:100]]}
        return super()._result(name, args)


async def _no_sleep(_seconds):
    return None


@pytest.mark.asyncio
async def test_the_whole_list_is_collected_letter_by_letter(state):
    names = {"industry": ["Retail", "Real Estate", "Financial Services", "Accounting"]
             + [f"S{chr(97 + i % 26)}{chr(97 + i // 26)}x Services" for i in range(150)],
             "region": ["California", "Massachusetts", "New York (Area)"],
             "country": ["United States", "United Kingdom"]}
    server = ListServer(names)
    out = await collector.collect_catalogs(state, KEY, transport=server.transport, sleep=_no_sleep)
    assert out["complete"] and not out["stopped"]
    got = await collector.catalog(state, "industry")
    assert got["complete"] and set(got["values"]) == set(names["industry"])
    assert (await collector.catalog(state, "region"))["values"] == [
        "California", "Massachusetts", "New York (Area)"]
    # 26 letters for each list; the full "s" split into 26, then the full "se" into 26.
    assert out["lookups"] == 26 * 3 + 26 + 26
    # A name picked from the list needs no second lookup at search time.
    assert await state.get_setting("salesql:lookup:region:new york (area)", "") \
        == "v-new york (area)"
    again = await collector.collect_catalogs(state, KEY, transport=server.transport,
                                             sleep=_no_sleep)
    assert again["lookups"] == 0 and again["complete"]


@pytest.mark.asyncio
async def test_collecting_stops_short_of_the_days_lookups_and_resumes(state):
    server = ListServer({"industry": ["Retail"], "region": ["California"],
                         "country": ["United States"]})
    server.per_day = 50
    out = await collector.collect_catalogs(state, KEY, transport=server.transport, sleep=_no_sleep)
    assert out["lookups"] == 30 and "tomorrow" in out["stopped"] and not out["complete"]
    assert (await collector.catalog(state, "industry"))["values"] == ["Retail"]
    assert (await collector.catalog(state, "region"))["complete"] is False
    await state.set_setting(collector._lookups_today_key(), "0")       # the next day
    server.per_day = 1000
    out = await collector.collect_catalogs(state, KEY, transport=server.transport, sleep=_no_sleep)
    assert out["complete"] and out["lookups"] == 26 * 3 - 30
