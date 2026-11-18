"""brain.provider: command — Harvey thinks through the operator's own program."""

import json
import sys

import pytest
import pytest_asyncio

from harvey import brain as brain_module
from harvey.brain import Brain
from harvey.config import BrainConfig
from harvey.state import StateManager


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(db_path=str(tmp_path / "t.db"))
    await s.init_db()
    return s


def program(tmp_path, body: str) -> list[str]:
    script = tmp_path / "model.py"
    script.write_text("import os, sys, json\nprompt = sys.stdin.read()\n" + body, encoding="utf-8")
    return [sys.executable, str(script)]


def test_the_command_provider_needs_a_program():
    with pytest.raises(ValueError, match="brain.command is empty"):
        BrainConfig(provider="command")
    assert BrainConfig(provider="command", command="/bin/echo hi").command == ["/bin/echo", "hi"]
    with pytest.raises(ValueError):
        BrainConfig(provider="command", command=["ok", 5])
    assert BrainConfig().command == []                      # Claude stays the default


@pytest.mark.asyncio
async def test_the_prompt_goes_in_and_the_printed_text_is_the_reply(state, tmp_path):
    cmd = program(tmp_path, "print('you said: ' + prompt.upper() + ' for ' + os.environ['HARVEY_AGENT']"
                            " + '/' + os.environ['HARVEY_TASK'])")
    brain = Brain(state, BrainConfig(provider="command", command=cmd))
    assert await brain.preflight() == ""
    reply = await brain.think("hello", agent="scout", task="score", max_retries=0)
    assert reply == "you said: HELLO for scout/score"
    assert await state.get_usage_today(provider="command") == 1
    assert await state.get_usage_today(provider="claude") == 0


@pytest.mark.asyncio
async def test_a_reply_envelope_carries_the_model_and_tokens(state, tmp_path):
    cmd = program(tmp_path, "print(json.dumps({'harvey_reply': '{\"ok\": true}', 'model': 'my-model',"
                            " 'input_tokens': 12, 'output_tokens': 3, 'cost_usd': 0.001}))")
    brain = Brain(state, BrainConfig(provider="command", command=cmd))
    assert await brain.think_json("anything") == {"ok": True}
    import sqlite3
    row = sqlite3.connect(state.db_path).execute(
        "SELECT provider, model, input_tokens, output_tokens FROM usage_events").fetchone()
    assert row == ("command", "my-model", 12, 3)


@pytest.mark.asyncio
async def test_a_json_answer_without_the_envelope_is_the_reply_itself(state, tmp_path):
    cmd = program(tmp_path, "print(json.dumps({'name': 'x'}))")
    brain = Brain(state, BrainConfig(provider="command", command=cmd))
    assert await brain.think_json("anything") == {"name": "x"}


@pytest.mark.asyncio
async def test_a_failing_or_silent_command_gives_no_reply(state, tmp_path, monkeypatch):
    monkeypatch.setattr(brain_module, "RETRY_BASE_DELAY", 0.0)
    fails = Brain(state, BrainConfig(provider="command", command=program(tmp_path, "sys.exit(3)")))
    assert await fails.think("hello", max_retries=1) == ""
    silent = Brain(state, BrainConfig(provider="command", command=program(tmp_path, "pass")))
    assert await silent.think("hello", max_retries=0) == ""
    assert await state.get_usage_today(provider="command") == 0


@pytest.mark.asyncio
async def test_a_command_that_hangs_is_stopped(state, tmp_path):
    cmd = program(tmp_path, "import time; time.sleep(30)")
    brain = Brain(state, BrainConfig(provider="command", command=cmd))
    assert await brain.think("hello", timeout=0.5, max_retries=0) == ""


@pytest.mark.asyncio
async def test_a_missing_program_is_reported_before_any_call(state):
    brain = Brain(state, BrainConfig(provider="command", command=["/no/such/program"]))
    assert "not found" in await brain.preflight()
    assert await brain.think("hello", max_retries=0) == ""
    assert await brain.is_within_budget(max_daily_calls=5, max_percent=80.0) is True


def test_the_environment_can_point_harvey_at_its_model(monkeypatch):
    monkeypatch.setenv("HARVEY_BRAIN_PROVIDER", "command")
    monkeypatch.setenv("HARVEY_BRAIN_COMMAND", "/usr/bin/python3 /opt/model client")
    config = BrainConfig(provider="claude")
    assert config.provider == "command"
    assert config.command == ["/usr/bin/python3", "/opt/model", "client"]
    monkeypatch.delenv("HARVEY_BRAIN_PROVIDER")
    monkeypatch.delenv("HARVEY_BRAIN_COMMAND")
    assert BrainConfig().provider == "claude"


@pytest.mark.asyncio
async def test_what_the_program_said_is_kept_for_the_person_waiting(state, tmp_path):
    cmd = program(tmp_path, "sys.exit('no answer: the monthly limit is reached')")
    brain = Brain(state, BrainConfig(provider="command", command=cmd))
    assert await brain.think("hello", max_retries=0) == ""
    assert "monthly limit is reached" in brain.last_error
