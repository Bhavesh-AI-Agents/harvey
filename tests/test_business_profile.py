"""Settings > Your business: who Harvey writes as and what it sells, without editing a file."""

import shutil

import pytest
import yaml

from harvey import config as cfg
from harvey.config import ConfigError, HarveyConfig, business_profile, set_business_profile
from harvey.paths import PROJECT_ROOT

GOOD = {"sender_name": "Ana Example", "sender_role": "Founder", "sender_email": "ana@example.com",
        "company": "Example Works", "product_name": "Example Works",
        "product_description": "We build small software changes for shops.",
        "pricing": "Quoted before work starts.", "key_benefits": ["Fixed price", " ", "No developer to hire"],
        "booking_method": "ask_preference", "postal_address": "1 Example St, Springfield",
        "auto_prospect": False}


@pytest.fixture
def files(tmp_path):
    source = tmp_path / "harvey.yaml"
    shutil.copy(PROJECT_ROOT / "harvey.yaml", source)
    return source, tmp_path / "harvey.local.yaml"


def test_the_template_shows_as_not_filled_in(files):
    shown = business_profile(files[0])
    assert shown["complete"] is False and shown["company"] == "" and shown["key_benefits"] == []
    assert shown["auto_prospect"] is True


def test_saving_writes_a_local_file_and_keeps_everything_else(files):
    source, target = files
    set_business_profile(GOOD, source=source, target=target)
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    config = HarveyConfig(**data)
    assert config.persona.company == "Example Works" and config.persona.email == "ana@example.com"
    assert config.product.key_benefits == ["Fixed price", "No developer to hire"]
    assert config.product.offer.booking_method == "ask_preference"
    assert config.compliance.postal_address == "1 Example St, Springfield"
    assert config.usage.auto_prospect is False
    assert config.product.objection_responses == {}            # the template's example answers are gone
    assert config.channels.email.provider == yaml.safe_load(source.read_text())["channels"]["email"]["provider"]
    assert source.read_text() == (PROJECT_ROOT / "harvey.yaml").read_text()     # the template is untouched
    again = business_profile(target)
    assert again["complete"] is True and again["sender_name"] == "Ana Example"
    set_business_profile({"pricing": "From $50."}, source=target, target=target)   # a later edit of one field
    assert business_profile(target)["pricing"] == "From $50." and business_profile(target)["company"] == "Example Works"


@pytest.mark.parametrize("change,match", [
    ({"company": ""}, "Still needed: company"),
    ({"sender_email": "not an address"}, "email address"),
    ({"booking_method": "calendar_link"}, "booking link"),
    ({"booking_url": "http://plain.example"}, "https://"),
    ({"goal": "world domination"}, "one of"),
    ({"key_benefits": "one string"}, "lines"),
    ({"auto_prospect": "yes"}, "on or off"),
    ({"product_description": "x" * 2001}, "too long")])
def test_a_profile_that_cannot_be_used_says_why_and_writes_nothing(files, change, match):
    source, target = files
    with pytest.raises(ConfigError, match=match):
        set_business_profile({**GOOD, **change}, source=source, target=target)
    assert not target.exists()


@pytest.mark.asyncio
async def test_with_finding_prospects_off_harvey_idles_instead_of_searching():
    from harvey.main import decide_next_action as decide_action
    data = yaml.safe_load((PROJECT_ROOT / "harvey.yaml").read_text(encoding="utf-8"))
    summary = {"prospects": {}, "writable_prospects": 0, "draft_campaigns": 0}
    assert await decide_action(None, None, HarveyConfig(**data), summary) == "prospect"
    data["usage"]["auto_prospect"] = False
    assert await decide_action(None, None, HarveyConfig(**data), summary) == "idle"
    summary["writable_prospects"] = 2
    summary["prospects"] = {"new": 2}
    assert await decide_action(None, None, HarveyConfig(**data), summary) == "write_campaign"


def test_a_server_with_keys_in_its_environment_and_no_env_file_counts_as_set_up(tmp_path, monkeypatch):
    import harvey.main as main_mod
    import harvey.config as config_mod
    cfg = tmp_path / "harvey.local.yaml"
    cfg.write_text("persona:\n  company: Example Works\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "_find_config_file", lambda *a, **k: cfg)
    assert main_mod._needs_setup() is False
    cfg.write_text("persona:\n  company: Your Company\n", encoding="utf-8")
    assert main_mod._needs_setup() is True
