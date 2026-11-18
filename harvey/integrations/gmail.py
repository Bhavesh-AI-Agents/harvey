"""Gmail / Google Workspace mailbox provider — raw REST, no SDK weight.

Setup (one-time):
1. Google Cloud Console → create a project → enable the Gmail API.
2. OAuth consent screen (internal or testing) → create an OAuth client of
   type "Desktop app". Put its id/secret in .env as GMAIL_CLIENT_ID /
   GMAIL_CLIENT_SECRET.
3. Run ``harvey gmail auth`` — opens a browser, stores the refresh token
   in data/gmail_token.json. Done: Harvey can send and read.

Deliverability notes: use a mailbox on a dedicated SECONDARY domain (never
your main one), configure SPF/DKIM/DMARC, ramp volume slowly, and watch
Google Postmaster Tools. Harvey's outbox pacing + kill switches handle the
rest.
"""

import base64
import codecs
import json
import os
import logging
import time
import webbrowser
from email.message import EmailMessage, Message
from email.utils import parseaddr

import httpx

from harvey.paths import PROJECT_ROOT
from harvey.integrations.mail_provider import (
    InboundMessage,
    MailProvider,
    SendResult,
    looks_like_bounce,
)
from harvey.integrations.signature import SignatureError, load_signature, set_body

logger = logging.getLogger("harvey.gmail")

TOKEN_FILE = PROJECT_ROOT / "data" / "gmail_token.json"
API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPE = "https://www.googleapis.com/auth/gmail.modify"
LOOPBACK_PORT = 8765
MAX_INBOX_PAGES = 40
# A full 7-day read runs at least this often, whatever the history says.
FULL_READ_EVERY_SECONDS = 24 * 3600
# Labels that mean "not a reply to handle".
_NOT_REPLIES = frozenset({"SENT", "DRAFT", "SPAM", "TRASH"})
# Codecs Python accepts that are no real mail charset (see _extract_text).
_NOT_MAIL_CHARSETS = frozenset({"punycode", "idna", "utf_7", "unicode_escape", "raw_unicode_escape"})
MAX_DECODE_BYTES = 200_000
# _request's answer for a 404 when the caller asked to know.
_GONE = object()

class GmailError(RuntimeError):
    pass


def _entry_id(entry, page: int) -> str:
    msg = entry.get("message") if isinstance(entry, dict) else None
    msg_id = msg.get("id") if isinstance(msg, dict) else None
    if not isinstance(msg_id, str) or not msg_id:
        raise GmailError(f"history page {page}: a change has no message id")
    return msg_id


TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)


def _load_token(path=None) -> dict | None:
    path = path or TOKEN_FILE
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("refresh_token"):
                return data
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Could not read Gmail token file: {e}")
    return None


def _save_token(data: dict, path=None):
    path = path or TOKEN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700) if path.parent.name == "gmail_tokens" else None
    except OSError:
        pass
    # Created owner-only from the start, so the sign-in is never readable by
    # other users, even for a moment.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=2))
    try:
        path.chmod(0o600)
    except OSError:
        pass


class GmailProvider(MailProvider):
    name = "gmail"

    def __init__(self, config, env, address: str = "", name: str = "", token_file=None):
        self.config = config
        # One of several sending mailboxes (harvey/mailboxes.py) has its own
        # address, sender name and sign-in. Without them this is the single
        # mailbox in persona.email, as it always was.
        self.address = address
        self.sender_name = name
        self._token_file = token_file
        self.client_id = getattr(env, "gmail_client_id", "")
        self.client_secret = getattr(env, "gmail_client_secret", "")
        self._access_token = ""
        self._access_expires = 0.0
        # Messages already read, by Gmail id (None: not a reply we use).
        # A Gmail message's content never changes, so each cycle's complete
        # read only downloads what's new. Memory only.
        self._read_cache: dict[str, InboundMessage | None] = {}
        self._label_cache: dict[str, frozenset] = {}
        self._token_problem = ""
        # Where the last confirmed read ended (Gmail history id), when the
        # last full 7-day read was confirmed, and what the read in flight
        # reached (kept by confirm_read).
        self._history_id: str | None = None
        self._last_full_read = 0.0
        self._verify_week = False      # list the week once more after a full read
        self._pending: tuple | None = None

    def _stored(self) -> dict | None:
        """This mailbox's saved sign-in."""
        return _load_token(self._token_file) if self._token_file else _load_token()

    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self._stored())

    async def _token(self) -> str | None:
        """Valid access token, refreshing via the stored refresh token."""
        if self._access_token and time.time() < self._access_expires - 60:
            return self._access_token
        stored = self._stored()
        if not stored:
            self._token_problem = "not signed in to Gmail. Run 'harvey gmail auth'."
            logger.error("Gmail: " + self._token_problem)
            return None
        # Why there's no token, for whoever asked (get_replies says it).
        unreachable = ("couldn't reach Google to refresh the Gmail sign-in{}; "
                       "Harvey tries again next cycle.")
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.post(TOKEN_URL, data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": stored["refresh_token"],
                    "grant_type": "refresh_token",
                })
        except httpx.HTTPError as e:
            logger.error(f"Gmail token refresh failed: {e}")
            self._token_problem = unreachable.format(f" ({type(e).__name__})")
            return None
        if resp.status_code in (400, 401, 403):
            self._token_problem = (f"Google rejected the saved Gmail sign-in ({resp.status_code}). "
                                   "Run 'harvey gmail auth'.")
            logger.error(f"Gmail token refresh rejected ({resp.status_code}). "
                         "Re-run 'harvey gmail auth'.")
            return None
        if resp.status_code != 200:
            self._token_problem = unreachable.format(f" (HTTP {resp.status_code})")
            logger.error(f"Gmail token refresh failed ({resp.status_code}).")
            return None
        # A garbled answer is no token, not an exception: one raised here
        # would escape a send and be recorded as "maybe sent".
        try:
            data = resp.json()
        except ValueError:
            data = None
        token = data.get("access_token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            self._token_problem = unreachable.format(" (no token in Google's answer)")
            logger.error("Gmail token refresh returned no access token.")
            return None
        self._token_problem = ""
        try:
            expires_in = float(data.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires_in = 3600.0
        self._access_token = token
        self._access_expires = time.time() + expires_in
        return self._access_token or None

    async def _request(
        self, method: str, path: str, *, sending: bool = False, not_found=None, **kwargs
    ):
        token = await self._token()
        if not token:
            return None
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.request(
                    method, f"{API_BASE}{path}", headers=headers, **kwargs
                )
        except httpx.HTTPError as e:
            logger.error(f"Gmail request failed ({path}): {e}")
            if sending:
                # The request may have reached Gmail before the connection failed.
                raise RuntimeError("Gmail send outcome is unknown") from e
            return None
        if sending and not (200 <= resp.status_code < 300
                            or 400 <= resp.status_code < 500):
            raise RuntimeError("Gmail send outcome is unknown")
        if resp.status_code == 404 and not_found is not None:
            return not_found
        if resp.status_code >= 400:
            logger.error(f"Gmail API {resp.status_code} on {path}: {resp.text[:200]}")
            return None
        try:
            data = resp.json()
            if sending and (not isinstance(data, dict)
                            or not isinstance(data.get("id"), str)
                            or not data["id"].strip()):
                raise ValueError("Missing Gmail send message ID")
            return data
        except ValueError as e:
            logger.error(f"Gmail returned malformed JSON ({path})")
            if sending:
                raise RuntimeError("Gmail send returned an unreadable response") from e
            return None

    async def send_email(
        self,
        to_email: str,
        subject: str,
        body: str,
        thread_ref: str = "",
        in_reply_to: str = "",
    ) -> SendResult:
        try:
            sig = load_signature(self.config)
        except SignatureError as e:
            return SendResult(ok=False, error=f"signature: {e}"[:300])
        msg = EmailMessage()
        persona = self.config.persona
        msg["To"] = to_email
        from_name = self.sender_name or persona.name
        from_address = self.address or persona.email
        msg["From"] = f"{from_name} <{from_address}>" if from_address else from_name
        msg["Subject"] = subject
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        set_body(msg, body, sig)

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        payload: dict = {"raw": raw}
        if thread_ref:
            payload["threadId"] = thread_ref

        data = await self._request("POST", "/messages/send", sending=True, json=payload)
        if data is None:
            return SendResult(ok=False, error="gmail send failed")

        # Fetch the assigned RFC Message-ID for bounce/thread matching later.
        message_id = ""
        gmail_id = data.get("id", "")
        if gmail_id:
            try:
                meta = await self._request(
                    "GET", f"/messages/{gmail_id}",
                    params={"format": "metadata", "metadataHeaders": "Message-ID"},
                )
            except Exception:
                # Sending already succeeded; optional metadata cannot undo it.
                logger.warning("Gmail sent message metadata could not be read")
                meta = None
            metadata = meta.get("payload") if isinstance(meta, dict) else None
            headers = metadata.get("headers") if isinstance(metadata, dict) else None
            for h in headers if isinstance(headers, list) else []:
                if (isinstance(h, dict) and isinstance(h.get("name"), str)
                        and h["name"].lower() == "message-id"
                        and isinstance(h.get("value"), str)):
                    message_id = h["value"]
        return SendResult(
            ok=True,
            message_id=message_id or gmail_id,
            thread_ref=(data.get("threadId", "")
                        if isinstance(data.get("threadId", ""), str) else ""),
        )

    async def get_replies(self, limit: int = 500) -> list[InboundMessage]:
        """Every reply since the last confirmed read, or raise, so sends wait.

        The first read, and one a day after that, is the complete 7-day
        inbox (_read_week). In between, Gmail's history list says only what
        was added since the marker, so a big mailbox costs what a small one
        does. The marker moves on only when the handler confirms every reply
        was handled (confirm_read), so a reply that couldn't be handled
        comes back. A marker Gmail no longer has (404, usually after about a
        week) means the 7-day read again.
        """
        if not await self._token():
            raise GmailError(self._token_problem
                             or "not signed in to Gmail. Run 'harvey gmail auth'.")
        self._pending = None
        recent_full = (self._last_full_read
                       and time.time() - self._last_full_read < FULL_READ_EVERY_SECONDS)
        if self._history_id and recent_full:
            changed = await self._read_history(self._history_id)
            if changed is not None:
                messages, marker = changed
                verified = False
                if self._verify_week:
                    # The full read listed through Gmail search, which can lag
                    # new mail by seconds: a reply just under the marker could
                    # be in neither read. A cycle later the index has caught
                    # up, so the first history read lists the week once more.
                    have = {m.provider_id for m in messages}
                    messages = messages + [m for m in await self._read_week(limit, prune=False)
                                           if m.provider_id not in have]
                    verified = True
                self._pending = (marker, False, verified)
                return messages
            logger.info("Gmail: the history marker has expired; reading the last week in full.")
        # The marker comes first: anything arriving during the read is in
        # the next history list (and deduplicated if it's in both).
        marker = await self._current_history_id()
        messages = await self._read_week(limit)
        self._pending = (marker, True, False)
        return messages

    def confirm_read(self):
        """The handler handled everything the last get_replies returned:
        the next read starts after it."""
        if not self._pending:
            return
        marker, full, verified = self._pending
        self._pending = None
        self._history_id = marker
        if full:
            self._last_full_read = time.time()
            self._verify_week = True
        elif verified:
            self._verify_week = False

    async def _current_history_id(self) -> str | None:
        """Gmail's current history marker, or None (the next read is the
        whole week again)."""
        try:
            profile = await self._request("GET", "/profile")
        except Exception as e:
            logger.warning(f"Gmail: couldn't read the history marker: {e}")
            return None
        marker = profile.get("historyId") if isinstance(profile, dict) else None
        return str(marker) if isinstance(marker, (str, int)) and str(marker) else None

    async def _fetch(self, msg_id: str, where: str):
        """(message or None, its labels) for ``msg_id``, from the cache when
        it was read before; _GONE when Gmail no longer has it (deleted)."""
        if msg_id in self._read_cache:
            return self._read_cache[msg_id], self._label_cache.get(msg_id, frozenset())
        data = await self._request(
            "GET", f"/messages/{msg_id}", params={"format": "full"}, not_found=_GONE,
        )
        if data is _GONE:
            return _GONE
        if not isinstance(data, dict) or data.get("id") != msg_id:
            raise GmailError(f"{where}: could not read message {msg_id}")
        msg = self._parse_message(data)
        labels = data.get("labelIds")
        self._read_cache[msg_id] = msg
        self._label_cache[msg_id] = frozenset(l for l in labels if isinstance(l, str)) \
            if isinstance(labels, list) else frozenset()
        return msg, self._label_cache[msg_id]

    async def _read_history(self, start: str):
        """(replies added to the inbox since ``start``, the new marker), or
        None when Gmail no longer has ``start``."""
        params = {"startHistoryId": start, "historyTypes": ["messageAdded", "labelAdded"],
                  "labelId": "INBOX", "maxResults": 500}
        wanted: list[tuple[str, bool]] = []      # (id, moved into the inbox)
        seen_tokens = set()
        marker = None
        for page in range(1, MAX_INBOX_PAGES + 1):
            data = await self._request("GET", "/history", params=dict(params), not_found=_GONE)
            if data is _GONE:
                return None
            if not isinstance(data, dict) or not isinstance(data.get("history", []), list):
                raise GmailError(f"history page {page}: could not read changes")
            if page == 1:
                # The first page's marker: anything recorded while paging is
                # read again next time (deduplicated) rather than skipped.
                # Without one there's no safe marker, so fail closed.
                marker = data.get("historyId") if isinstance(data, dict) else None
                if not isinstance(marker, (str, int)) or not str(marker):
                    raise GmailError("history page 1: no historyId")
            for record in data.get("history", []):
                if not isinstance(record, dict):
                    raise GmailError(f"history page {page}: a record is not an object")
                for entry in record.get("messagesAdded") or []:
                    wanted.append((_entry_id(entry, page), False))
                for entry in record.get("labelsAdded") or []:
                    if "INBOX" in ((entry.get("labelIds") if isinstance(entry, dict) else None) or []):
                        wanted.append((_entry_id(entry, page), True))
            token = data.get("nextPageToken")
            if token is None:
                break
            if not isinstance(token, str) or not token or token in seen_tokens:
                raise GmailError(f"history page {page}: invalid or repeated nextPageToken")
            seen_tokens.add(token)
            params["pageToken"] = token
        else:
            raise GmailError(f"history has more than {MAX_INBOX_PAGES} pages; "
                             "refusing a partial read")
        out, done = [], set()
        for msg_id, moved_in in wanted:
            if msg_id in done:
                continue
            done.add(msg_id)
            if moved_in:
                # Judge it on its labels now, not ones cached when it was,
                # say, in the trash.
                self._read_cache.pop(msg_id, None)
                self._label_cache.pop(msg_id, None)
            got = await self._fetch(msg_id, "history")
            if got is _GONE:
                continue
            msg, labels = got
            # Harvey's own mail and drafts aren't replies; a message added
            # outside the inbox counts once it's moved in (labelsAdded).
            if labels & _NOT_REPLIES or (not moved_in and "INBOX" not in labels):
                continue
            if msg:
                out.append(msg)
        return out, str(marker)

    async def _read_week(self, limit: int, prune: bool = True) -> list[InboundMessage]:
        """The complete 7-day inbox, or raise. ``limit`` is a page size,
        not a cap: a reply beyond the first page must still stop its
        follow-ups. Bounded like the Graph provider (MAX_INBOX_PAGES of
        Gmail's largest pages, which are ids only), never partial."""
        params = {"q": "in:inbox -from:me newer_than:7d",
                  "maxResults": max(1, min(500, int(limit)))}
        out = []
        seen_ids = set()
        seen_tokens = set()
        for page in range(1, MAX_INBOX_PAGES + 1):
            listing = await self._request("GET", "/messages", params=dict(params))
            if not isinstance(listing, dict):
                raise GmailError(f"inbox page {page}: could not read messages")
            # Gmail omits 'messages' when a successful query has no matches.
            messages = listing.get("messages", [])
            if not isinstance(messages, list):
                raise GmailError(f"inbox page {page}: messages is not a list")
            for stub in messages:
                msg_id = stub.get("id") if isinstance(stub, dict) else None
                if not isinstance(msg_id, str) or not msg_id:
                    raise GmailError(f"inbox page {page}: message has no id")
                if msg_id in seen_ids:
                    continue
                seen_ids.add(msg_id)
                got = await self._fetch(msg_id, f"inbox page {page}")
                if got is _GONE:          # deleted since the listing
                    continue
                if got[0]:
                    out.append(got[0])
            token = listing.get("nextPageToken")
            if token is None:
                # A complete read: forget what has left the week's window
                # (not on a recheck, which shouldn't drop what history fetched).
                if prune:
                    self._read_cache = {k: v for k, v in self._read_cache.items() if k in seen_ids}
                    self._label_cache = {k: v for k, v in self._label_cache.items() if k in seen_ids}
                return out
            if not isinstance(token, str) or not token or token in seen_tokens:
                raise GmailError(f"inbox page {page}: invalid or repeated nextPageToken")
            seen_tokens.add(token)
            params["pageToken"] = token
        raise GmailError(
            f"inbox has more than {MAX_INBOX_PAGES} pages; refusing a partial read"
        )

    @staticmethod
    def _parse_message(data: dict) -> InboundMessage | None:
        payload = data.get("payload") or {}
        headers = {
            h.get("name", "").lower(): h.get("value", "")
            for h in payload.get("headers", [])
        }
        from_email = parseaddr(headers.get("from", ""))[1].lower()
        if not from_email:
            return None
        subject = headers.get("subject", "")
        body = GmailProvider._extract_text(payload) or data.get("snippet", "")
        return InboundMessage(
            provider_id=data.get("id", ""),
            from_email=from_email,
            subject=subject,
            body=body[:5000],
            message_id=headers.get("message-id", ""),
            in_reply_to=headers.get("in-reply-to", ""),
            thread_ref=data.get("threadId", ""),
            date=headers.get("date", ""),
            is_bounce=looks_like_bounce(from_email, subject),
            headers=headers,
        )

    @staticmethod
    def _extract_text(payload: dict) -> str:
        """Depth-first hunt for the text/plain part of a MIME tree."""
        def decode(part) -> str:
            data = (part.get("body") or {}).get("data", "")
            if not data:
                return ""
            try:
                raw = base64.urlsafe_b64decode(data + "===")
            except Exception:
                return ""
            # Finding the charset must never cost the text: odd headers mean
            # UTF-8, as before, not an empty body (an unsubscribe in it would
            # be lost).
            charset = "utf-8"
            try:
                headers = part.get("headers")
                for header in headers if isinstance(headers, list) else []:
                    if (isinstance(header, dict) and isinstance(header.get("value"), str)
                            and str(header.get("name") or "").lower() == "content-type"):
                        mime = Message()
                        mime["Content-Type"] = header["value"]
                        charset = mime.get_content_charset() or "utf-8"
                        break
            except Exception:
                charset = "utf-8"
            try:
                # A sender picks the label. Codecs that aren't mail charsets
                # are refused: punycode is quadratic (megabytes stall the
                # heartbeat) and the escape codecs let them shape the text.
                if codecs.lookup(charset).name.replace("-", "_") in _NOT_MAIL_CHARSETS:
                    charset = "utf-8"
            except (LookupError, ValueError, TypeError):   # unknown, or a null in the name
                charset = "utf-8"
            # Harvey keeps 5,000 characters of a body; decode no further than
            # that could need.
            raw = raw[:MAX_DECODE_BYTES]
            try:
                return raw.decode(charset, errors="replace")
            except (LookupError, ValueError):
                # Unknown or invalid charsets keep the existing UTF-8 fallback.
                return raw.decode("utf-8", errors="replace")

        if payload.get("mimeType", "").startswith("text/plain"):
            return decode(payload)
        for part in payload.get("parts", []) or []:
            text = GmailProvider._extract_text(part)
            if text:
                return text
        if payload.get("mimeType", "").startswith("text/html"):
            import re
            html = decode(payload)
            return re.sub(r"<[^>]+>", " ", html)
        return ""

    async def test_connection(self) -> tuple[bool, str]:
        if not self.client_id or not self.client_secret:
            return False, "GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET missing from .env"
        if not self._stored():
            return False, "Not authorized yet — run 'harvey gmail auth'"
        data = await self._request("GET", "/profile")
        if data and data.get("emailAddress"):
            return True, f"Connected as {data['emailAddress']}"
        return False, "Token refresh or profile fetch failed"


def run_auth_flow(client_id: str, client_secret: str) -> bool:
    """One-time OAuth (installed-app loopback). Blocks until the browser
    round-trip completes; stores the refresh token for GmailProvider."""
    import http.server
    import secrets
    import threading
    import urllib.parse

    if not client_id or not client_secret:
        print("  Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in .env first.")
        print("  (Google Cloud Console → APIs → Credentials → OAuth client, Desktop app)")
        return False

    state = secrets.token_urlsafe(16)
    redirect_uri = f"http://localhost:{LOOPBACK_PORT}"
    auth_url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    })

    result: dict = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if query.get("state", [""])[0] != state:
                self.send_response(400); self.end_headers()
                return
            result["code"] = query.get("code", [""])[0]
            result["error"] = query.get("error", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<h2>Harvey is connected to Gmail.</h2>You can close this tab.")
            done.set()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("localhost", LOOPBACK_PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print(f"\n  Opening browser for Google sign-in...\n  {auth_url}\n")
    webbrowser.open(auth_url)
    if not done.wait(timeout=300):
        server.shutdown()
        print("  Timed out waiting for the browser round-trip.")
        return False
    server.shutdown()

    if result.get("error") or not result.get("code"):
        print(f"  Authorization failed: {result.get('error') or 'no code returned'}")
        return False

    resp = httpx.post(TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "code": result["code"],
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }, timeout=30)
    if resp.status_code != 200:
        print(f"  Token exchange failed ({resp.status_code}): {resp.text[:200]}")
        return False
    data = resp.json()
    if not data.get("refresh_token"):
        print("  Google returned no refresh token. Remove Harvey from your"
              " account's third-party access list and retry.")
        return False
    _save_token({
        "refresh_token": data["refresh_token"],
        "scope": data.get("scope", SCOPE),
        "obtained_at": int(time.time()),
    })
    print(f"  ✓ Gmail authorized. Token stored at {TOKEN_FILE}")
    return True


# ── Several mailboxes ──

class GmailPool(MailProvider):
    """Every mailbox on the sending list (harvey/mailboxes.py), as one provider.

    Sending names the mailbox (``from_address``); reading gathers replies
    from all of them and notes which mailbox each one arrived in. A mailbox
    that is switched off or not signed in sends nothing and is not read.
    """

    name = "gmail"
    is_pool = True

    def __init__(self, config, env):
        self.config = config
        self.env = env
        self._providers: dict[tuple[str, str], GmailProvider] = {}
        self._read_ok: list[GmailProvider] = []
        # address -> why the last read of that mailbox failed
        self.problems: dict[str, str] = {}

    def boxes(self):
        from harvey import mailboxes
        return mailboxes.load()

    def provider_for(self, box) -> GmailProvider:
        from harvey import mailboxes
        key = (box.address, box.name)
        if key not in self._providers:
            # A renamed mailbox gets a fresh provider; the old one is dropped.
            for stale in [k for k in self._providers if k[0] == box.address]:
                del self._providers[stale]
            self._providers[key] = GmailProvider(
                self.config, self.env, address=box.address, name=box.name,
                token_file=mailboxes.token_path(box.address))
        return self._providers[key]

    def signed_in(self, box) -> bool:
        return self.provider_for(box).is_configured()

    def usable(self) -> list:
        """Mailboxes that can send right now: switched on and signed in."""
        return [b for b in self.boxes() if b.enabled and self.signed_in(b)]

    def is_configured(self) -> bool:
        return bool(self.usable())

    async def send_email(self, to_email: str, subject: str, body: str, thread_ref: str = "",
                         in_reply_to: str = "", from_address: str = "") -> SendResult:
        box = next((b for b in self.usable() if b.address == (from_address or "").lower()), None)
        if box is None:
            return SendResult(ok=False, error=f"mailbox {from_address or '(none)'} is not available")
        return await self.provider_for(box).send_email(
            to_email, subject, body, thread_ref=thread_ref, in_reply_to=in_reply_to)

    async def get_replies(self, limit: int = 500) -> list[InboundMessage]:
        """Replies from every usable mailbox. One mailbox that can't be read
        is skipped and reported in ``problems``; it never blocks the others."""
        self._read_ok = []
        self.problems = {}
        out: list[InboundMessage] = []
        for box in self.usable():
            provider = self.provider_for(box)
            try:
                messages = await provider.get_replies(limit)
            except Exception as e:
                self.problems[box.address] = str(e)[:200]
                logger.warning(f"Gmail: couldn't read {box.address}: {e}")
                continue
            for message in messages:
                message.mailbox = box.address
            out.extend(messages)
            self._read_ok.append(provider)
        return out

    def confirm_read(self) -> None:
        for provider in self._read_ok:
            provider.confirm_read()
        self._read_ok = []

    async def test_connection(self) -> tuple[bool, str]:
        boxes = self.boxes()
        if not boxes:
            return False, "No sending mailboxes yet. Add one on the dashboard (Settings)."
        lines, all_ok = [], True
        for box in boxes:
            if not box.enabled:
                lines.append(f"{box.address}: switched off")
                continue
            ok, detail = await self.provider_for(box).test_connection()
            all_ok = all_ok and ok
            lines.append(f"{box.address}: {detail}")
        return all_ok, "; ".join(lines)


# ── Signing a mailbox in from the dashboard ──
#
# The dashboard runs where Harvey runs, which may be a server the user only
# reaches through a browser. So the round-trip ends at the dashboard's own
# front page (the redirect address), which hands the code back to Harvey.

def web_auth_url(client_id: str, redirect_uri: str, state: str, address: str = "") -> str:
    import urllib.parse
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    if address:
        params["login_hint"] = address
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


async def finish_web_auth(client_id: str, client_secret: str, code: str, redirect_uri: str,
                          address: str, token_file, *, transport=None) -> tuple[bool, str]:
    """Exchange the code, check it is for the mailbox being added, save the
    sign-in. Returns (ok, message for the user)."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport) as client:
            resp = await client.post(TOKEN_URL, data={
                "client_id": client_id, "client_secret": client_secret, "code": code,
                "grant_type": "authorization_code", "redirect_uri": redirect_uri})
            try:
                data = resp.json()
            except ValueError:
                data = None
            if resp.status_code != 200 or not isinstance(data, dict):
                return False, ("Google refused the sign-in. Check that this address is listed "
                               "as a redirect address on your Google OAuth client: " + redirect_uri)
            access, refresh = data.get("access_token"), data.get("refresh_token")
            if not isinstance(access, str) or not access:
                return False, "Google's answer had no access token. Try again."
            if not isinstance(refresh, str) or not refresh:
                return False, ("Google returned no lasting sign-in. Remove Harvey from the "
                               "account's third-party access list, then try again.")
            prof = await client.get(API_BASE + "/profile",
                                    headers={"Authorization": f"Bearer {access}"})
            try:
                signed_in_as = str(prof.json().get("emailAddress", "")).strip().lower()
            except (ValueError, AttributeError):
                signed_in_as = ""
    except httpx.HTTPError:
        return False, "Couldn't reach Google. Try again in a minute."
    if not signed_in_as:
        return False, ("Signed in, but Gmail wouldn't say which mailbox. Check that the Gmail "
                       "API is enabled for your Google Cloud project.")
    if signed_in_as != address:
        return False, (f"You signed in as {signed_in_as}, not {address}. "
                       "Sign in with the mailbox you are adding.")
    _save_token({"refresh_token": refresh, "scope": data.get("scope", SCOPE),
                 "obtained_at": int(time.time())}, token_file)
    return True, f"{address} is signed in."
