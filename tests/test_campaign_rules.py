"""Campaign rules: sequence length and spacing, A/B testing, learning from
results, link tags, and the Settings forms that hold them.

Before this, a sequence was always 3 emails over 7 days, nothing compared
two angles, results were reported but never reached the Writer, and product
knowledge could only be written by the trainer."""

import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
import yaml

import harvey.config as config_module
from harvey.agents.writer import (
    AB_ANGLE_A, AB_ANGLE_B, CAMPAIGN_VARIANT_KEY, SEQUENCE_SHAPE, Writer,
    sequence_plan, sequence_shape, tag_links,
)
from harvey.config import (
    ConfigError, WriterConfig, campaign_rules, product_knowledge,
    set_campaign_rules, set_product_knowledge,
)
from harvey.models.campaign import Campaign, EmailStep
from harvey.models.prospect import Prospect
from harvey.state import StateManager


class RecordingBrain:
    def __init__(self, reply):
        self.reply, self.prompts = reply, []

    def load_skills_for_agent(self, name):
        return ""

    def load_prompt(self, name, **kw):
        return "prompt"

    async def think_json(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.reply


def config(provider="instantly", **writer):
    return SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(
            provider=provider, require_approval=True, tag_links=True,
            link_domains=["example.com"], from_email="ana@example.com")),
        product=SimpleNamespace(name="P", description="D", key_benefits=[], pricing=""),
        persona=SimpleNamespace(name="S", company="C", role="R", tone="t", email="ana@example.com"),
        writer=SimpleNamespace(humanizer=False, email_rules="", **writer),
        compliance=config_module.ComplianceConfig(),
    )


def make_writer(brain, state, **writer):
    w = Writer(brain, state, config(**writer))
    w.skills, w.email_rules = "", ""
    return w


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "w.db"))
    await s.init_db()
    return s


async def people(state, count):
    out = []
    for n in range(count):
        pid = await state.add_prospect(Prospect(
            first_name=f"P{n}", last_name="Doe", title="Owner", company=f"Co {n}",
            industry="Roofing", email=f"p{n}@x.test", email_status="verified"))
        out.append(await state.get_prospect(pid))
    return out


def reply(count):
    return [{"step": n, "subject": f"s{n}", "body": f"b{n} {{{{company}}}}", "delay_days": 99}
            for n in range(1, count + 1)]


# ── Sequence length and spacing ──

def test_three_emails_stay_exactly_as_they_were():
    assert sequence_plan(SimpleNamespace(writer=WriterConfig())) == [0, 3, 4]
    assert sequence_shape([0, 3, 4]) == SEQUENCE_SHAPE


def test_a_longer_sequence_has_bumps_in_the_middle_and_a_goodbye_last():
    cfg = SimpleNamespace(writer=WriterConfig(sequence_steps=5))
    assert sequence_plan(cfg) == [0, 3, 4, 7, 7]
    shape = sequence_shape(sequence_plan(cfg))
    assert shape.count("Bump") == 2 and "Email 5: Break-up" in shape
    assert "applies to the last email here (email 5)" in shape


def test_the_users_own_spacing_wins_and_a_wrong_length_falls_back():
    assert sequence_plan(SimpleNamespace(
        writer=WriterConfig(sequence_steps=4, sequence_delays=[2, 5, 9]))) == [0, 2, 5, 9]
    assert sequence_plan(SimpleNamespace(
        writer=WriterConfig(sequence_steps=4, sequence_delays=[2, 5]))) == [0, 3, 4, 7]


@pytest.mark.parametrize("bad", [{"sequence_steps": 1}, {"sequence_steps": 7},
                                 {"sequence_delays": [0, 3]}, {"sequence_delays": [3, 31]},
                                 {"ab_angle_a": "x" * 401}, {"ab_min_prospects": 2}])
def test_out_of_range_rules_are_refused(bad):
    with pytest.raises(Exception):
        WriterConfig(**bad)


@pytest.mark.asyncio
async def test_the_writer_asks_for_that_many_emails_and_keeps_the_users_spacing(state):
    brain = RecordingBrain(reply(5))
    steps = await make_writer(brain, state, sequence_steps=5, sequence_delays=[])._write_sequence(
        await people(state, 1))
    assert "Write a 5-email cold outreach sequence" in brain.prompts[0]
    assert '"step": 5' in brain.prompts[0]
    assert [s.delay_days for s in steps] == [0, 3, 4, 7, 7]     # never the model's 99


# ── A/B testing ──

def test_a_big_enough_group_is_split_in_two_alike_halves():
    w = Writer(None, None, config(ab_test=True, ab_min_prospects=4, ab_angle_a="", ab_angle_b="Mine."))
    out = w._ab_split({"roofing-outreach": list(range(5)), "small-outreach": [1, 2]})
    assert out["roofing-outreach (A)"] == [0, 2, 4] and out["roofing-outreach (B)"] == [1, 3]
    assert out["small-outreach"] == [1, 2]
    assert w._batch_angles["roofing-outreach (A)"] == ("A", AB_ANGLE_A)
    assert w._batch_angles["roofing-outreach (B)"] == ("B", "Mine.")


def test_with_testing_off_groups_are_left_alone():
    w = Writer(None, None, config(ab_test=False))
    assert w._ab_split({"g": [1, 2, 3, 4, 5, 6]}) == {"g": [1, 2, 3, 4, 5, 6]}
    assert w._batch_angles == {}


@pytest.mark.asyncio
async def test_each_half_is_written_with_its_angle_and_remembered(state):
    brain = RecordingBrain(reply(3))
    await people(state, 4)
    await make_writer(brain, state, ab_test=True, ab_min_prospects=4, ab_angle_a="", ab_angle_b="").run()
    assert any(AB_ANGLE_A in p for p in brain.prompts) and any(AB_ANGLE_B in p for p in brain.prompts)
    campaigns = await state.get_campaigns_by_status("draft")
    assert sorted(c.name for c in campaigns) == ["Roofing-outreach (A)", "Roofing-outreach (B)"]
    saved = [json.loads(await state.get_setting(CAMPAIGN_VARIANT_KEY + c.id)) for c in campaigns]
    assert sorted(v["variant"] for v in saved) == ["A", "B"]


# ── Learning from results ──

async def sent_campaign(state, name, sent, replies, interested=0, angle=""):
    prospects = await people(state, sent)
    cid = await state.add_campaign(Campaign(
        id="", name=name, status="active", prospect_ids=[p.id for p in prospects],
        sequence=[EmailStep(step=1, subject="one", body="b"), EmailStep(step=2, subject="the follow-up", body="b")]))
    if angle:
        await state.set_setting(CAMPAIGN_VARIANT_KEY + cid, json.dumps({"variant": "A", "angle": angle}))
    async with state._connect() as db:
        for i, p in enumerate(prospects):
            await db.execute(
                "INSERT INTO outbox (id, campaign_id, prospect_id, step, kind, status) "
                "VALUES (?, ?, ?, 1, 'sequence', 'sent')", (f"{cid}-{i}", cid, p.id))
            if i < replies:
                await db.execute(
                    "INSERT INTO conversations (id, prospect_id, campaign_id, intent) VALUES (?, ?, ?, ?)",
                    (f"v-{cid}-{i}", p.id, cid, "interested" if i < interested else "not_interested"))
        await db.commit()
    return cid


@pytest.mark.asyncio
async def test_results_count_first_emails_sent_and_people_who_replied(state):
    await sent_campaign(state, "good", 20, 4, interested=2, angle="Offer the estimate.")
    await sent_campaign(state, "poor", 25, 1)
    await sent_campaign(state, "too small", 3, 3)
    rows = await state.campaign_results(20)
    assert [(r["name"], r["sent"], r["replies"], r["interested"]) for r in rows] == [
        ("good", 20, 4, 2), ("poor", 25, 1, 0)]
    assert rows[0]["angle"] == "Offer the estimate." and rows[0]["subject"] == "the follow-up"


@pytest.mark.asyncio
async def test_the_writer_sees_results_only_when_learning_is_on(state):
    await sent_campaign(state, "good", 20, 4, interested=2, angle="Offer the estimate.")
    on = await make_writer(None, state, learn_from_results=True, learn_min_sent=20)._results_block()
    assert "20 sent, 4 replies (20.0%), 2 interested" in on and "Offer the estimate." in on
    assert "never overrides the STRICT" in on
    assert await make_writer(None, state, learn_from_results=False)._results_block() == ""
    assert await make_writer(None, state, learn_from_results=True, learn_min_sent=50)._results_block() == ""


# ── Link tags ──

def test_only_links_to_the_users_own_site_are_tagged():
    cfg = config()
    text = "See https://example.com/book. Or https://www.example.com/a?x=1#top and https://other.test/p"
    out = tag_links(text, cfg, "abc12345-6789")
    assert "https://example.com/book?ref=h-abc12345." in out
    assert "https://www.example.com/a?x=1&ref=h-abc12345#top" in out
    assert "https://other.test/p" in out and "other.test/p?" not in out
    assert tag_links(out, cfg, "abc12345-6789") == out          # never tagged twice


def test_with_the_setting_off_nothing_changes():
    cfg = config()
    cfg.channels.email.tag_links = False
    assert tag_links("https://example.com/book", cfg, "abc") == "https://example.com/book"


# ── The Settings forms ──

@pytest.fixture
def settings_file(tmp_path):
    path = tmp_path / "harvey.local.yaml"
    data = yaml.safe_load((config_module.PROJECT_ROOT / "harvey.yaml").read_text(encoding="utf-8"))
    data["persona"]["company"] = "Example Works"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_until_saved_the_recommended_rules_are_offered_but_not_in_force(settings_file):
    rules = campaign_rules(settings_file)
    assert rules["saved"] is False
    assert rules["values"]["sequence_steps"] == 3 and rules["values"]["send_window_on"] is False
    assert rules["recommended"]["sequence_steps"] == 5 and rules["recommended"]["send_window_on"] is True


def test_saving_the_rules_keeps_everything_else_in_the_file(settings_file):
    set_campaign_rules(dict(config_module.RECOMMENDED_CAMPAIGN_RULES), source=settings_file)
    data = yaml.safe_load(settings_file.read_text(encoding="utf-8"))
    assert data["persona"]["company"] == "Example Works" and data["channels"]["email"]["provider"] == "gmail"
    assert data["channels"]["email"]["send_window"] == {
        "start": "08:00", "end": "17:00", "weekdays_only": True, "recipient_timezone": True}
    assert data["writer"]["sequence_delays"] == [3, 4, 7, 7] and data["writer"]["ab_test"] is True
    assert campaign_rules(settings_file)["saved"] is True
    set_campaign_rules({"send_window_on": False}, source=settings_file)
    assert "send_window" not in yaml.safe_load(settings_file.read_text(encoding="utf-8"))["channels"]["email"]


@pytest.mark.parametrize("bad, says", [
    ({"sequence_steps": 9}, "2 to 6"),
    ({"sequence_steps": 4, "sequence_delays": [3, 4]}, "Give 3 waits"),
    ({"ab_test": "yes"}, "on or off"),
    ({"send_window_on": True, "send_start": "18:00", "send_end": "09:00"}, "can't be saved"),
])
def test_rules_that_make_no_sense_are_refused_and_nothing_is_written(settings_file, bad, says):
    before = settings_file.read_text(encoding="utf-8")
    with pytest.raises(ConfigError, match=says):
        set_campaign_rules(bad, source=settings_file)
    assert settings_file.read_text(encoding="utf-8") == before


def test_product_knowledge_is_saved_read_back_and_cleared(tmp_path):
    assert product_knowledge(tmp_path) == ""
    set_product_knowledge("# What we sell\r\nFixed prices.  ", tmp_path)
    assert product_knowledge(tmp_path) == "# What we sell\nFixed prices.\n"
    set_product_knowledge("", tmp_path)
    assert not (tmp_path / "skills" / "product_knowledge.md").exists()
    with pytest.raises(ConfigError, match="at most"):
        set_product_knowledge("x" * 20_001, tmp_path)
