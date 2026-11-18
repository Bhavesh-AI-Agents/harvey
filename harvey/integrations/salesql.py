"""SalesQL (salesql.com): a B2B contact database, read through its MCP server.

Harvey uses it for the one thing its own prospecting can't do: find people by
job title, industry, company size and location, and ask whether a company has
anyone with a software job title. The writing model never sees this
connection. Harvey's own code talks to SalesQL, and the model keeps running
with no tools.

What costs money and what doesn't:

- ``lookup``, ``search_people``, ``search_organizations`` and
  ``get_account_status`` are free. Search results come back with emails
  masked, so a search can never spend a credit.
- ``enrich_person_bulk`` reveals contact data and spends up to one credit per
  person found. Nothing in this module calls it on its own: the collector
  only does so for people a human ticked, inside a daily cap.

The key (SALESQL_API_KEY, ``harvey salesql connect`` or the dashboard) is a
SalesQL "API/MCP key". It works with the MCP server on every SalesQL plan. It
goes in the Authorization header and nowhere else.

The MCP server speaks JSON-RPC over HTTP (the "Streamable HTTP" transport):
one POST per message, answered with JSON or with a short event stream. Read
from salesql.com/docs/mcp on 2026-11-17.
"""

import hashlib
import json

import httpx

MCP_URL = "https://mcp.salesql.com/mcp"
KEY_URL = "https://app.salesql.com/dashboard/enrichment/api"
PROTOCOL_VERSION = "2025-06-18"
TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)
BULK_LIMIT = 100
MAX_BODY = 8_000_000

KEY_REJECTED = ("SalesQL rejected the key. Create an API/MCP key in SalesQL "
                "(Dashboard > Enrichment > API) and connect it again.")
RATE_LIMITED = ("SalesQL's limit for your plan is reached (per minute or per day). "
                "Try again later, or check the limits with 'harvey salesql'.")
NO_ANSWER = "SalesQL didn't answer. Try again in a minute."
UNREADABLE = "SalesQL sent a reply Harvey couldn't read."
SUSPENDED = "SalesQL reports this account as suspended. Check it in SalesQL."

# Hashes of keys SalesQL rejected. Nothing sends them again in this process.
_REJECTED: set[str] = set()


class SalesQLError(RuntimeError):
    """Something went wrong talking to SalesQL. The message says what to do."""


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def forget_rejected(key: str) -> None:
    _REJECTED.discard(_hash((key or "").strip()))


def _dict(x) -> dict:
    return x if isinstance(x, dict) else {}


def _list(x) -> list:
    return x if isinstance(x, list) else []


def _str(x) -> str:
    return x.strip() if isinstance(x, str) else ""


def _int(x) -> int:
    if isinstance(x, bool):
        return 0
    if isinstance(x, (int, float)) and x == x and abs(x) != float("inf"):
        return int(x)
    if isinstance(x, str) and x.strip().lstrip("-").isdigit():
        return int(x.strip())
    return 0


def _parse_stream(text: str, want_id) -> dict | None:
    """The JSON-RPC answer to ``want_id`` from a server-sent event stream."""
    found = None
    for block in text.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(line[5:].lstrip(" ") for line in block.split("\n")
                         if line.startswith("data:"))
        if not data:
            continue
        try:
            message = json.loads(data)
        except ValueError:
            continue
        if isinstance(message, dict) and message.get("id") == want_id \
                and ("result" in message or "error" in message):
            found = message
    return found


class SalesQL:
    """One connection to SalesQL's MCP server. Use as ``async with``."""

    def __init__(self, api_key: str, *, transport: httpx.AsyncBaseTransport | None = None):
        if not isinstance(api_key, str) or not api_key.strip() \
                or not (api_key.isascii() and api_key.isprintable()):
            raise SalesQLError(KEY_REJECTED)
        self._key = api_key.strip()
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._session = ""
        self._next_id = 0
        self._ready = False

    def __repr__(self) -> str:
        return "SalesQL()"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def aclose(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._ready = False
        self._session = ""

    # ── transport ──

    async def _post(self, payload: dict) -> dict | None:
        """Send one JSON-RPC message. Returns the answer, or None for a notification."""
        if _hash(self._key) in _REJECTED:
            raise SalesQLError(KEY_REJECTED)
        if self._client is None:
            self._client = httpx.AsyncClient(transport=self._transport, timeout=TIMEOUT)
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        try:
            resp = await self._client.post(MCP_URL, json=payload, headers=headers)
        except httpx.HTTPError:
            raise SalesQLError(NO_ANSWER) from None
        if resp.status_code in (401, 403):
            _REJECTED.add(_hash(self._key))
            raise SalesQLError(KEY_REJECTED)
        if resp.status_code == 429:
            raise SalesQLError(RATE_LIMITED)
        if resp.status_code >= 500:
            raise SalesQLError(NO_ANSWER)
        if not resp.is_success:
            raise SalesQLError(f"SalesQL returned {resp.status_code}.")
        session = resp.headers.get("mcp-session-id", "")
        if session and session.isascii() and session.isprintable() and len(session) <= 256:
            self._session = session
        if "id" not in payload:
            return None
        if len(resp.content) > MAX_BODY:
            raise SalesQLError(UNREADABLE)
        if "text/event-stream" in resp.headers.get("content-type", ""):
            message = _parse_stream(resp.text, payload["id"])
        else:
            try:
                message = resp.json()
            except ValueError:
                message = None
        if not isinstance(message, dict):
            raise SalesQLError(UNREADABLE)
        return message

    async def _rpc(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            payload["params"] = params
        message = await self._post(payload)
        error = _dict(message.get("error"))
        if error:
            raise SalesQLError(self._clean(_str(error.get("message"))) or UNREADABLE)
        return _dict(message.get("result"))

    def _clean(self, text: str) -> str:
        return text.replace(self._key, "[hidden]")[:300]

    async def _start(self):
        if self._ready:
            return
        await self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "harvey", "version": "1"},
        })
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._ready = True

    async def _tool(self, name: str, arguments: dict):
        """Call one SalesQL tool and return its parsed result."""
        await self._start()
        result = await self._rpc("tools/call", {"name": name, "arguments": arguments})
        text = ""
        for part in _list(result.get("content")):
            if _dict(part).get("type") == "text":
                text = _str(part.get("text"))
                break
        if result.get("isError"):
            lowered = text.lower()
            if "rate limit" in lowered or "too many" in lowered or "429" in lowered:
                raise SalesQLError(RATE_LIMITED)
            if "suspend" in lowered or "banned" in lowered:
                raise SalesQLError(SUSPENDED)
            raise SalesQLError("SalesQL: " + (self._clean(text) or "the request failed."))
        structured = result.get("structuredContent")
        if isinstance(structured, (dict, list)):
            return structured
        try:
            return json.loads(text)
        except ValueError:
            raise SalesQLError(UNREADABLE) from None

    # ── tools ──

    async def account_status(self) -> dict:
        """Plan, credits left and daily limits. Free."""
        data = _dict(await self._tool("get_account_status", {}))
        if data.get("banned") is True:
            raise SalesQLError(SUSPENDED)
        limits = _dict(data.get("rate_limits"))
        return {
            "plan": _str(data.get("plan")),
            "credits": _int(data.get("credits")),
            "monthly_included": _int(data.get("monthly_included")),
            "credits_refresh_date": _str(data.get("credits_refresh_date"))[:10],
            "searches_per_day": _int(_dict(limits.get("search_people")).get("daily")),
            "reveals_per_day": _int(_dict(limits.get("persons/enrich")).get("daily")),
        }

    async def lookup(self, kind: str, q: str = "", size: int = 20) -> list[dict]:
        """SalesQL's own filter values: [{value, label}]. Free."""
        params: dict = {"type": kind, "size": size}
        if q:
            params["q"] = q[:100]
        data = _dict(await self._tool("lookup", {"params": params}))
        return [{"value": _str(v.get("value")), "label": _str(v.get("label"))}
                for v in _list(data.get("values"))
                if isinstance(v, dict) and _str(v.get("value"))]

    async def search_people(self, filters: dict, page: int = 1, page_size: int = 20) -> dict:
        """People matching the filters, contact data masked. Free."""
        params = {k: v for k, v in filters.items() if v not in (None, [], {}, "")}
        params["page"] = max(1, page)
        params["page_size"] = min(100, max(20, page_size))
        data = _dict(await self._tool("search_people", {"params": params}))
        return {"results": [r for r in _list(data.get("results")) if isinstance(r, dict)],
                "total": _int(data.get("total"))}

    async def search_organizations(self, filters: dict, page_size: int = 20) -> dict:
        """Companies matching the filters. Free."""
        params = {k: v for k, v in filters.items() if v not in (None, [], {}, "")}
        params["page_size"] = min(100, max(20, page_size))
        data = _dict(await self._tool("search_organizations", {"params": params}))
        return {"results": [r for r in _list(data.get("results")) if isinstance(r, dict)],
                "total": _int(data.get("total"))}

    async def enrich_people(self, persons: list[dict]) -> list[dict]:
        """Reveal contact data for up to 100 people. SPENDS CREDITS: up to one
        per person found. Returns one entry per request item, in order: the
        person's profile, or {} when SalesQL found nobody or returned an error
        for that item."""
        persons = [p for p in persons if isinstance(p, dict) and p][:BULK_LIMIT]
        if not persons:
            return []
        data = await self._tool("enrich_person_bulk", {
            "params": {"persons": persons, "include_work_experience": False}})
        items = data if isinstance(data, list) else None
        if items is None:
            for key in ("results", "persons", "items", "data"):
                if isinstance(_dict(data).get(key), list):
                    items = data[key]
                    break
        if items is None:
            raise SalesQLError(UNREADABLE)
        out = []
        for item in items[:len(persons)]:
            item = _dict(item)
            if item.get("error"):
                out.append({})
                continue
            for key in ("person", "result", "data"):
                if isinstance(item.get(key), dict):
                    item = item[key]
                    break
            out.append(item if (_list(item.get("emails")) or _str(item.get("full_name"))
                                or _str(item.get("uuid"))) else {})
        out += [{}] * (len(persons) - len(out))
        return out


    async def verify_email(self, email: str) -> str:
        """Ask SalesQL whether one address accepts mail: "valid", "invalid",
        "catch_all" or "unknown". SPENDS ONE VERIFICATION CREDIT, a balance
        separate from the reveal credits (plans include a monthly allowance).
        The check can take up to 45 seconds. Never repeat a failed call at
        once: it may have completed and been charged."""
        data = _dict(await self._tool("verify_email", {"params": {"email": email}}))
        for key in ("result", "data"):
            if isinstance(data.get(key), dict):
                data = data[key]
                break
        status = _str(data.get("status")).lower().replace("-", "_")
        return status if status in ("valid", "invalid", "catch_all") else "unknown"

def key_from_env() -> str:
    import os
    return os.getenv("SALESQL_API_KEY", "").strip()
