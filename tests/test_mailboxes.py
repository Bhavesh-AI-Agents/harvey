"""Sending from several mailboxes: one mailbox per person, each with its own
limit, warmup ramp and sign-in. All addresses are placeholders."""

import json
from datetime import date, timedelta

import httpx
import pytest

from harvey import mailboxes as mb
from harvey.config import WarmupConfig
from harvey.integrations import gmail
from harvey.integrations.mail_provider import InboundMessage, MailProvider, SendResult
from harvey.models.campaign import Campaign, EmailStep
from harvey.models.prospect import Prospect
from tests.test_outbox_native import Cfg, StubBrain, make_sender, state  # noqa: F401

A, B = "ana@example.com", "bo@example.com"


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "MAILBOXES_FILE", tmp_path / "data" / "mailboxes.json")
    monkeypatch.setattr(mb, "TOKEN_DIR", tmp_path / "data" / "gmail_tokens")


class FakePool(MailProvider):
    """A pool of fake mailboxes: sends record which mailbox they left from."""

    name = "gmail"
    is_pool = True

    def __init__(self, signed_out=()):
        self.sent = []
        self.inbound = []
        self.signed_out = set(signed_out)

    def boxes(self):
        return mb.load()

    def signed_in(self, box):
        return box.address not in self.signed_out

    def usable(self):
        return [b for b in self.boxes() if b.enabled and self.signed_in(b)]

    def is_configured(self):
        return bool(self.usable())

    async def send_email(self, to_email, subject, body, thread_ref="", in_reply_to="",
                         from_address=""):
        assert from_address in {b.address for b in self.usable()}
        self.sent.append({"to": to_email, "from": from_address, "body": body,
                          "thread_ref": thread_ref})
        return SendResult(ok=True, message_id=f"<m{len(self.sent)}@example.com>",
                          thread_ref=f"t-{from_address}")

    async def get_replies(self, limit=50):
        return self.inbound

    async def test_connection(self):
        return True, "fake"


async def seed(state, n, steps=1):
    ids = []
    for i in range(n):
        ids.append(await state.add_prospect(Prospect(
            first_name=f"P{i}", last_name="Example", title="Owner", company=f"Co{i}",
            email=f"p{i}@co{i}.example", email_status="verified", email_verified=True)))
    sequence = [EmailStep(step=1, subject="hi {{first_name}}",
                          body="A question for {{company}}?\n\nHarvey", delay_days=0)]
    if steps > 1:
        sequence.append(EmailStep(step=2, subject="again", body="One more thought.\n\nHarvey",
                                  delay_days=0))
    campaign = Campaign(id="", name="c", channel="email", sequence=sequence,
                        prospect_ids=ids, status="draft")
    campaign.id = await state.add_campaign(campaign)
    return ids


def pool_sender(state, pool, approval=False):
    sender = make_sender(state, pool, require_approval=approval)
    sender.config.channels.email.provider = "gmail"
    sender.config.channels.email.warmup = WarmupConfig()
    return sender


# ── the list ──

def test_the_list_is_saved_owner_only_and_read_back():
    mb.add(A, "Ana Example", 30)
    mb.add(B, "Bo Sample", 10, warmup_start="")
    boxes = mb.load()
    assert [(b.address, b.name, b.daily_limit) for b in boxes] == [
        (A, "Ana Example", 30), (B, "Bo Sample", 10)]
    assert boxes[0].warmup_start == date.today().isoformat() and boxes[1].warmup_start == ""
    assert mb.MAILBOXES_FILE.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("address", ["", "nope", "a@b", "a b@example.com", "a..b@example.com",
                                     "../../x@example.com", 5, None])
def test_a_bad_address_is_refused(address):
    with pytest.raises(mb.MailboxError):
        mb.add(address)


def test_bad_values_are_refused_and_duplicates_too():
    mb.add(A, "Ana Example")
    for kwargs in ({"daily_limit": -1}, {"daily_limit": 9999}, {"daily_limit": True},
                   {"name": "A <b>"}, {"warmup_start": "tomorrow"}):
        with pytest.raises(mb.MailboxError):
            mb.update(A, **kwargs)
    with pytest.raises(mb.MailboxError):
        mb.add(A.upper())
    with pytest.raises(mb.MailboxError):
        mb.update("nobody@example.com", daily_limit=5)


def test_a_damaged_file_is_no_mailboxes_not_a_crash():
    mb.MAILBOXES_FILE.parent.mkdir(parents=True)
    mb.MAILBOXES_FILE.write_text("{not json")
    assert mb.load() == []
    mb.MAILBOXES_FILE.write_text(json.dumps([{"address": A}, {"address": "bad"}, "x", {"address": A}]))
    assert [b.address for b in mb.load()] == [A]


def test_the_sign_in_file_cannot_leave_its_folder_and_goes_with_the_mailbox():
    path = mb.token_path(A)
    assert path.parent == mb.TOKEN_DIR and path.name == "ana@example.com.json"
    mb.add(A)
    gmail._save_token({"refresh_token": "r"}, path)
    assert path.stat().st_mode & 0o777 == 0o600
    mb.remove(A)
    assert not path.exists() and mb.load() == []


def test_each_mailbox_ramps_from_its_own_start_date():
    class Email:
        warmup = WarmupConfig(min_days=14, start_sends=5, daily_increase=3)
    today = date(2026, 12, 1)
    fresh = mb.Mailbox(A, daily_limit=50, warmup_start=(today - timedelta(days=3)).isoformat())
    ramping = mb.Mailbox(A, daily_limit=50, warmup_start=(today - timedelta(days=16)).isoformat())
    old = mb.Mailbox(A, daily_limit=20, warmup_start=(today - timedelta(days=90)).isoformat())
    none = mb.Mailbox(A, daily_limit=50)
    assert mb.cold_cap(fresh, Email, today) == 0
    assert mb.cold_cap(ramping, Email, today) == 11
    assert mb.cold_cap(old, Email, today) == 20
    assert mb.cold_cap(none, Email, today) == 50


def test_new_people_go_to_the_least_loaded_mailbox_for_its_size():
    big, small = mb.Mailbox(A, daily_limit=40), mb.Mailbox(B, daily_limit=10)
    off = mb.Mailbox("off@example.com", daily_limit=100, enabled=False)
    assert mb.pick([big, small, off], {}).address == A
    assert mb.pick([big, small], {A: 8, B: 1}).address == B      # 8/40 is more than 1/10
    assert mb.pick([big, small], {A: 4, B: 2}).address == A
    assert mb.pick([off], {}) is None


def test_only_the_closing_name_is_swapped():
    bo = mb.Mailbox(B, name="Bo Sample")
    assert mb.resign("Hi there.\n\nHarvey", "Harvey Specter", bo) == "Hi there.\n\nBo"
    assert mb.resign("Hi.\n\nHarvey Specter\n", "Harvey Specter", bo) == "Hi.\n\nBo Sample\n"
    assert mb.resign("Hi.\n\n- Harvey", "Harvey Specter", bo) == "Hi.\n\n- Bo"
    unchanged = "Harvey here. Worth a look?\n\nThanks"
    assert mb.resign(unchanged, "Harvey Specter", bo) == unchanged
    assert mb.resign("Hi.\n\nHarvey", "Harvey", mb.Mailbox(B, name="Harvey")) == "Hi.\n\nHarvey"
    assert mb.resign("Hi.\n\nHarvey", "Harvey", mb.Mailbox(B)) == "Hi.\n\nHarvey"


# ── queuing and sending ──

@pytest.mark.asyncio
async def test_without_a_list_nothing_changes(state):
    from tests.test_outbox_native import FakeProvider
    await seed(state, 1)
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    await sender._run_native()
    sent = await state.get_outbox(status="sent")
    assert len(sent) == 1 and sent[0]["from_address"] == ""


@pytest.mark.asyncio
async def test_people_are_spread_and_each_keeps_one_mailbox(state):
    mb.add(A, "Ana Example", 30, warmup_start="")
    mb.add(B, "Bo Sample", 10, warmup_start="")
    ids = await seed(state, 8, steps=2)
    pool = FakePool()
    sender = pool_sender(state, pool, approval=True)
    await sender._run_native()

    queued = await state.get_outbox(status="pending_review", limit=100)
    by_person = {}
    for item in queued:
        by_person.setdefault(item["prospect_id"], set()).add(item["from_address"])
    assert len(by_person) == 8 and all(len(v) == 1 for v in by_person.values())
    counts = {a: sum(1 for v in by_person.values() if v == {a}) for a in (A, B)}
    assert counts == {A: 6, B: 2}                 # in proportion to 30 and 10
    # Signed with the sender's own name, so the reviewer sees what goes out.
    for item in queued:
        closing = item["body"].rstrip().split("\n")[-1]
        assert closing == ("Ana" if item["from_address"] == A else "Bo")
    assert pool.sent == [] and len(ids) == 8


@pytest.mark.asyncio
async def test_each_mailbox_stops_at_its_own_limit(state):
    mb.add(A, "Ana Example", 2, warmup_start="")
    mb.add(B, "Bo Sample", 1, warmup_start="")
    await seed(state, 9)
    pool = FakePool()
    sender = pool_sender(state, pool)
    await sender._run_native()
    await sender._drain_due()
    sent_from = [m["from"] for m in pool.sent]
    assert sent_from.count(A) == 2 and sent_from.count(B) == 1 and len(sent_from) == 3
    assert all(i["from_address"] for i in await state.get_outbox(status="sent"))
    # The rest wait, still approved, for tomorrow.
    assert len(await state.get_outbox(status="approved", limit=100)) == 6


@pytest.mark.asyncio
async def test_a_mailbox_still_warming_sends_no_cold_email(state):
    mb.add(A, "Ana Example", 50, warmup_start=date.today().isoformat())
    await seed(state, 2)
    pool = FakePool()
    sender = pool_sender(state, pool)
    sender.config.channels.email.warmup = WarmupConfig(min_days=14)
    await sender._run_native()
    assert pool.sent == [] and len(await state.get_outbox(status="approved")) == 2


@pytest.mark.asyncio
async def test_a_switched_off_or_signed_out_mailbox_holds_its_email(state):
    mb.add(A, "Ana Example", 50, warmup_start="")
    await seed(state, 2)
    pool = FakePool()
    sender = pool_sender(state, pool, approval=True)
    await sender._run_native()                       # queued for A
    await state.approve_outbox()
    mb.add(B, "Bo Sample", 50, warmup_start="")
    mb.update(A, enabled=False)
    await sender._drain_due()
    assert pool.sent == []                           # never moved to B
    mb.update(A, enabled=True)
    pool.signed_out = {A}
    await sender._drain_due()
    assert pool.sent == []
    pool.signed_out = set()
    await sender._drain_due()
    assert {m["from"] for m in pool.sent} == {A} and len(pool.sent) == 2


@pytest.mark.asyncio
async def test_email_queued_before_the_list_keeps_the_personas_mailbox(state):
    from tests.test_outbox_native import FakeProvider
    await seed(state, 1, steps=2)
    sender = make_sender(state, FakeProvider(), require_approval=True)
    await sender._run_native()                        # queued with no mailbox
    await state.approve_outbox()
    mb.add(B, "Bo Sample", 50, warmup_start="")
    mb.add("harvey@x.co", "Harvey", 50, warmup_start="")
    pool = FakePool()
    sender = pool_sender(state, pool)
    await sender._drain_due()
    assert [m["from"] for m in pool.sent] == ["harvey@x.co"]
    assert pool.sent[0]["body"].rstrip().split("\n")[-2:] != ["Bo"]
    left = await state.get_outbox(status="approved")
    assert [i["from_address"] for i in left] == ["harvey@x.co"]   # the follow-up follows


@pytest.mark.asyncio
async def test_a_reply_is_answered_from_the_mailbox_it_arrived_in(state):
    from harvey.agents.handler import Handler
    mb.add(A, "Ana Example", 50, warmup_start="")
    mb.add(B, "Bo Sample", 50, warmup_start="")
    await seed(state, 1)
    pool = FakePool()
    sender = pool_sender(state, pool)
    await sender._run_native()
    first = pool.sent[0]
    from tests.test_outbox_native import Env as NativeEnv
    handler = Handler(brain=StubBrain(reply="Happy to.\n\nHarvey"), state=state,
                      config=sender.config, env=NativeEnv())
    handler.provider = pool
    pool.inbound = [InboundMessage(provider_id="r1", from_email=first["to"], subject="hi P0",
                                   body="Tell me more?", message_id="<r1@example.com>",
                                   thread_ref="t-" + first["from"], mailbox=first["from"])]
    await handler.run()
    reply = [i for i in await state.get_outbox(limit=20) if i["kind"] == "reply"]
    assert len(reply) == 1 and reply[0]["from_address"] == first["from"]
    assert reply[0]["thread_ref"] == "t-" + first["from"]
    expected = "Ana" if first["from"] == A else "Bo"
    assert reply[0]["body"].rstrip().split("\n")[-1] == expected


# ── the Gmail pool ──

class Env:
    gmail_client_id = "cid"
    gmail_client_secret = "secret"


@pytest.mark.asyncio
async def test_the_pool_sends_from_the_named_mailbox_only(monkeypatch):
    mb.add(A, "Ana Example", warmup_start="")
    mb.add(B, "Bo Sample", warmup_start="")
    gmail._save_token({"refresh_token": "ra"}, mb.token_path(A))
    pool = gmail.GmailPool(Cfg(), Env())
    assert [b.address for b in pool.usable()] == [A] and pool.is_configured()
    seen = []

    async def fake_send(self, to_email, subject, body, thread_ref="", in_reply_to=""):
        seen.append((self.address, self.sender_name, to_email))
        return SendResult(ok=True, message_id="<x@example.com>")
    monkeypatch.setattr(gmail.GmailProvider, "send_email", fake_send)
    assert (await pool.send_email("p@co.example", "s", "b", from_address=A)).ok
    assert seen == [(A, "Ana Example", "p@co.example")]
    refused = await pool.send_email("p@co.example", "s", "b", from_address=B)   # not signed in
    assert not refused.ok and "not available" in refused.error
    assert not (await pool.send_email("p@co.example", "s", "b")).ok


@pytest.mark.asyncio
async def test_one_unreadable_mailbox_does_not_block_the_others(monkeypatch):
    mb.add(A, warmup_start="")
    mb.add(B, warmup_start="")
    for address in (A, B):
        gmail._save_token({"refresh_token": "r"}, mb.token_path(address))
    pool = gmail.GmailPool(Cfg(), Env())
    confirmed = []

    async def fake_replies(self, limit=500):
        if self.address == A:
            raise gmail.GmailError("sign-in expired")
        return [InboundMessage(provider_id="m1", from_email="p@co.example")]
    monkeypatch.setattr(gmail.GmailProvider, "get_replies", fake_replies)
    monkeypatch.setattr(gmail.GmailProvider, "confirm_read",
                        lambda self: confirmed.append(self.address))
    messages = await pool.get_replies()
    assert [(m.provider_id, m.mailbox) for m in messages] == [("m1", B)]
    assert "expired" in pool.problems[A]
    pool.confirm_read()
    assert confirmed == [B]


def test_one_mailbox_from_header_uses_its_own_name(monkeypatch):
    provider = gmail.GmailProvider(Cfg(), Env(), address=B, name="Bo Sample")
    assert provider.address == B and provider.sender_name == "Bo Sample"
    # The single-mailbox provider is untouched.
    legacy = gmail.GmailProvider(Cfg(), Env())
    assert legacy.address == "" and legacy._token_file is None


# ── signing in from the dashboard ──

def google(token_status=200, refresh="r1", signed_in_as=A):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == gmail.TOKEN_URL:
            body = {"access_token": "at", "scope": gmail.SCOPE}
            if refresh:
                body["refresh_token"] = refresh
            return httpx.Response(token_status, json=body)
        assert request.headers["authorization"] == "Bearer at"
        return httpx.Response(200, json={"emailAddress": signed_in_as})
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_a_finished_sign_in_is_saved_for_that_mailbox():
    ok, message = await gmail.finish_web_auth(
        "cid", "secret", "code", "https://harvey.example/", A, mb.token_path(A),
        transport=google())
    assert ok and A in message
    assert gmail._load_token(mb.token_path(A))["refresh_token"] == "r1"


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs, expect", [
    ({"signed_in_as": B}, "You signed in as bo@example.com"),
    ({"refresh": ""}, "no lasting sign-in"),
    ({"token_status": 400}, "redirect address"),
    ({"signed_in_as": ""}, "Gmail API"),
])
async def test_a_wrong_or_incomplete_sign_in_saves_nothing(kwargs, expect):
    ok, message = await gmail.finish_web_auth(
        "cid", "secret", "code", "https://harvey.example/", A, mb.token_path(A),
        transport=google(**kwargs))
    assert not ok and expect in message
    assert gmail._load_token(mb.token_path(A)) is None


def test_the_sign_in_link_asks_for_the_right_mailbox():
    url = gmail.web_auth_url("cid", "https://harvey.example/", "st", A)
    assert url.startswith("https://accounts.google.com/")
    for part in ("client_id=cid", "state=st", "login_hint=ana%40example.com",
                 "access_type=offline", "redirect_uri=https%3A%2F%2Fharvey.example%2F"):
        assert part in url
