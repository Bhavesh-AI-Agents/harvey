"""Sending from several mailboxes.

One mailbox can only send so much cold email a day before it starts landing
in spam, so volume comes from more mailboxes, not from pushing one harder.
This module keeps the list: which mailboxes Harvey may send from, the daily
limit of each, and when each one's warmup began. The list is managed from
the dashboard (Settings > Sending mailboxes) and lives in
``data/mailboxes.json``, next to each mailbox's own Gmail sign-in in
``data/gmail_tokens/``. Both are owner-only files.

With no mailboxes listed, nothing changes: Harvey sends from the single
mailbox in ``persona.email`` exactly as before.

Rules the rest of Harvey relies on:

- A person hears from one mailbox. The mailbox is chosen when their first
  email is queued, and every follow-up and reply uses the same one.
- Each mailbox has its own daily limit and its own warmup ramp (the ramp's
  shape comes from ``channels.email.warmup``; the start date is per mailbox).
- A mailbox that is switched off, or not signed in, sends nothing. Email
  already assigned to it waits.
"""

import json
import logging
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from harvey.paths import PROJECT_ROOT

logger = logging.getLogger("harvey.mailboxes")

MAILBOXES_FILE = PROJECT_ROOT / "data" / "mailboxes.json"
TOKEN_DIR = PROJECT_ROOT / "data" / "gmail_tokens"
MAX_MAILBOXES = 50
MAX_DAILY_LIMIT = 500
ADDRESS_RE = re.compile(r"^[a-z0-9][a-z0-9._%+-]{0,63}@[a-z0-9][a-z0-9.-]{0,251}\.[a-z]{2,24}$")


class MailboxError(ValueError):
    """The mailbox can't be saved as asked. The message says why."""


@dataclass
class Mailbox:
    address: str
    name: str = ""
    daily_limit: int = 50
    enabled: bool = True
    # YYYY-MM-DD the mailbox's warmup began. Empty: no ramp, the daily limit
    # applies from the first day.
    warmup_start: str = ""
    added: str = ""

    @property
    def first_name(self) -> str:
        return self.name.split(" ")[0] if self.name else ""


def clean_address(raw) -> str:
    address = raw.strip().lower() if isinstance(raw, str) else ""
    if not ADDRESS_RE.match(address) or ".." in address:
        raise MailboxError("That doesn't look like an email address.")
    return address


def _clean(entry: dict) -> Mailbox:
    address = clean_address(entry.get("address"))
    name = entry.get("name")
    name = re.sub(r"\s+", " ", name).strip() if isinstance(name, str) else ""
    if len(name) > 80 or any(c in name for c in '<>"\r\n'):
        raise MailboxError("The sender name can't contain < > \" or line breaks.")
    limit = entry.get("daily_limit", 50)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= MAX_DAILY_LIMIT:
        raise MailboxError(f"The daily limit must be a whole number from 0 to {MAX_DAILY_LIMIT}.")
    start = entry.get("warmup_start") or ""
    if start:
        try:
            date.fromisoformat(start)
        except (TypeError, ValueError):
            raise MailboxError("The warmup start date must be YYYY-MM-DD.") from None
    added = entry.get("added") if isinstance(entry.get("added"), str) else ""
    return Mailbox(address=address, name=name, daily_limit=limit,
                   enabled=entry.get("enabled") is not False, warmup_start=start, added=added)


def load() -> list[Mailbox]:
    """The mailboxes Harvey may send from, in the order they were added.
    An unreadable file is no mailboxes plus a warning, never a crash."""
    try:
        if not MAILBOXES_FILE.is_file():
            return []
        data = json.loads(MAILBOXES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning(f"Could not read {MAILBOXES_FILE.name}: {e}")
        return []
    out, seen = [], set()
    for entry in data if isinstance(data, list) else []:
        try:
            box = _clean(entry if isinstance(entry, dict) else {})
        except MailboxError:
            continue
        if box.address not in seen:
            seen.add(box.address)
            out.append(box)
    return out[:MAX_MAILBOXES]


def save(boxes: list[Mailbox]) -> None:
    """Write the list atomically, owner-only."""
    MAILBOXES_FILE.parent.mkdir(parents=True, exist_ok=True)
    if MAILBOXES_FILE.is_symlink():
        raise OSError(f"{MAILBOXES_FILE} is a symbolic link; edit it by hand.")
    fd, tmp = tempfile.mkstemp(dir=str(MAILBOXES_FILE.parent), prefix=".mailboxes-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump([asdict(b) for b in boxes], f, indent=2)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, MAILBOXES_FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get(address: str) -> Mailbox | None:
    address = (address or "").strip().lower()
    return next((b for b in load() if b.address == address), None)


def add(address, name="", daily_limit=50, warmup_start=None) -> Mailbox:
    boxes = load()
    box = _clean({"address": address, "name": name, "daily_limit": daily_limit,
                  "warmup_start": date.today().isoformat() if warmup_start is None
                  else warmup_start,
                  "added": date.today().isoformat()})
    if any(b.address == box.address for b in boxes):
        raise MailboxError("That mailbox is already on the list.")
    if len(boxes) >= MAX_MAILBOXES:
        raise MailboxError(f"Harvey sends from at most {MAX_MAILBOXES} mailboxes.")
    boxes.append(box)
    save(boxes)
    return box


def update(address: str, **changes) -> Mailbox:
    boxes = load()
    for i, box in enumerate(boxes):
        if box.address == (address or "").strip().lower():
            merged = {**asdict(box), **{k: v for k, v in changes.items()
                                        if k in ("name", "daily_limit", "enabled", "warmup_start")}}
            boxes[i] = _clean(merged)
            save(boxes)
            return boxes[i]
    raise MailboxError("That mailbox isn't on the list.")


def remove(address: str) -> None:
    address = (address or "").strip().lower()
    boxes = load()
    kept = [b for b in boxes if b.address != address]
    if len(kept) == len(boxes):
        raise MailboxError("That mailbox isn't on the list.")
    save(kept)
    try:
        token_path(address).unlink(missing_ok=True)
    except OSError as e:
        logger.warning(f"Could not remove the saved sign-in for a mailbox: {e}")


def token_path(address: str) -> Path:
    """Where a mailbox's Gmail sign-in is kept. The name is built from the
    address's own safe characters, so it can never leave TOKEN_DIR."""
    safe = re.sub(r"[^a-z0-9._@+-]", "_", clean_address(address)).replace("..", "_")
    return TOKEN_DIR / f"{safe}.json"


def cold_cap(box: Mailbox, email_config, today: date | None = None) -> int:
    """Today's cold-email cap for one mailbox: its daily limit under the
    warmup ramp, counted from the mailbox's own start date."""
    from harvey.config import WarmupConfig
    base = getattr(email_config, "warmup", None)
    if not box.warmup_start or not isinstance(base, WarmupConfig):
        return box.daily_limit
    ramp = WarmupConfig(start_date=box.warmup_start, min_days=base.min_days,
                        start_sends=base.start_sends, daily_increase=base.daily_increase)
    return ramp.daily_cap(box.daily_limit, today)


def pick(boxes: list[Mailbox], open_counts: dict[str, int]) -> Mailbox | None:
    """The mailbox a new person should hear from: the one with the least
    queued email for its size, so work spreads in proportion to the limits."""
    usable = [b for b in boxes if b.enabled and b.daily_limit > 0]
    if not usable:
        return None
    return min(usable, key=lambda b: (open_counts.get(b.address, 0) / b.daily_limit,
                                      -b.daily_limit, b.address))


def resign(body: str, persona_name: str, box: Mailbox) -> str:
    """Sign the email as the mailbox's sender.

    Emails are written signed with the persona's name. When another person's
    mailbox sends one, the closing name is swapped for theirs. Only a last
    line that is exactly the persona's full or first name is touched; any
    other mention is left alone, so nothing in the message itself changes.
    """
    persona = re.sub(r"\s+", " ", persona_name or "").strip()
    if not body or not persona or not box.name or box.name.lower() == persona.lower():
        return body
    lines = body.rstrip().split("\n")
    last = lines[-1].strip()
    lead = re.match("^[-\\u2013\\u2014 ]*", last).group(0)   # hyphen or dash before the name
    bare = last[len(lead):].strip()
    if bare.lower() == persona.lower() and " " in persona:
        replacement = box.name
    elif bare.lower() == persona.split(" ")[0].lower():
        replacement = box.first_name
    else:
        return body
    lines[-1] = lines[-1].replace(last, lead + replacement)
    return "\n".join(lines) + body[len(body.rstrip()):]
