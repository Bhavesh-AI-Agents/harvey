"""Find people through SalesQL, sort them, and reveal only the ones a human ticks.

Three steps, and only the last one costs anything:

1. SEARCH (free). Build a SalesQL people search from the ICP (job titles,
   industries or company-name keywords, company size, country) and store what
   comes back in ``salesql_candidates``. Contact data is still masked.
2. QUALIFY (free). Sort each candidate into qualified, unsure or rejected,
   with the reason, from two questions: is this person technical (title,
   headline; past titles are excluded in the search itself), and does their
   company have people with software job titles (one more SalesQL search,
   covering many companies at once)?
3. REVEAL (one SalesQL credit per person found). Only for candidates a human
   ticked, inside ``salesql.max_reveals_per_day`` and the credits the account
   has left. Each revealed address is graded by Harvey's own verifier before
   it can be written to, and the do-not-contact list is honoured.

Nothing here opens or scrapes linkedin.com. A LinkedIn URL is only an
identifier SalesQL returns and accepts.
"""

import asyncio
import logging
import re
from datetime import datetime, timezone

from harvey.integrations import salesql as sq
from harvey.models.company import Company
from harvey.models.prospect import Prospect

logger = logging.getLogger("harvey.collectors.salesql")

COLLECTOR = "salesql"
SIZE_RANGES = (("1-10", 1, 10), ("11-50", 11, 50), ("51-200", 51, 200),
               ("201-500", 201, 500), ("501-1000", 501, 1000),
               ("1001-5000", 1001, 5000), ("5001-10000", 5001, 10000),
               ("10001+", 10001, 10 ** 9))
COUNTRY_ALIASES = {"us": "United States", "usa": "United States", "u.s.": "United States",
                   "united states of america": "United States", "uk": "United Kingdom",
                   "great britain": "United Kingdom"}
DEV_CHECK_BATCH = 10       # companies per developer-check search
MAX_DEV_SEARCHES = 3       # developer-check searches per run
MAX_KEYWORD_SEARCHES = 3   # company-name searches per run
MAX_ORGS = 50              # companies a people search is narrowed to


def _s(x) -> str:
    return x.strip() if isinstance(x, str) else ""


def _d(x) -> dict:
    return x if isinstance(x, dict) else {}


def _host(value: str) -> str:
    host = _s(value).lower()
    host = re.sub(r"^[a-z]+://", "", host).split("/")[0].split("?")[0]
    return host[4:] if host.startswith("www.") else host


def sizes_for(company_size: str) -> list[str]:
    """SalesQL size ranges overlapping an ICP size such as "2-50" or "under 200"."""
    numbers = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", company_size or "")]
    if not numbers:
        return []
    low, high = (numbers[0], numbers[1]) if len(numbers) > 1 else (1, numbers[0])
    if re.search(r"\+|over|more than|above", company_size or "", re.I) and len(numbers) == 1:
        low, high = numbers[0], 10 ** 9
    if low > high:
        low, high = high, low
    return [name for name, lo, hi in SIZE_RANGES if lo <= high and hi >= low]


def technical_word(text: str, words) -> str:
    """The first technical word found in ``text`` on a word boundary, or ""."""
    lowered = (text or "").lower()
    for word in words or ():
        word = _s(word).lower()
        if word and re.search(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])", lowered):
            return word
    return ""


def grade_candidate(person: dict, *, technical_words, allowed_sizes, dev_count,
                    require_no_developers: bool = True) -> tuple[str, str]:
    """(grade, reason) for one candidate. ``dev_count`` is None when the
    company wasn't checked."""
    word = technical_word(person.get("title", ""), technical_words) \
        or technical_word(person.get("headline", ""), technical_words)
    if word:
        return "rejected", f"Looks technical: \"{word}\" in the title or headline."
    if not person.get("org_uuid"):
        return "unsure", "SalesQL has no company record for this person."
    if not person.get("has_work_email"):
        return "rejected", "SalesQL has no work email for this person."
    if allowed_sizes and person.get("org_size") and person["org_size"] not in allowed_sizes:
        return "unsure", (f"Their main company is {person['org_size']} people, outside the "
                          "size you asked for.")
    if not require_no_developers:
        return "qualified", "Matches the title, company size and location."
    if dev_count is None:
        return "unsure", "The company wasn't checked for software staff yet."
    if dev_count > 0:
        return "rejected", (f"{dev_count} {'person' if dev_count == 1 else 'people'} with a "
                            "software job title at the company.")
    return "qualified", "No software job titles found at the company."


def _size_label(raw: str) -> str:
    return re.sub(r"\s+", "", _s(raw))


def candidate_from_result(result: dict) -> dict:
    """The fields Harvey keeps from one SalesQL search result."""
    org = _d(result.get("organization"))
    loc = _d(result.get("location"))
    emails = [e for e in result.get("emails") or [] if isinstance(e, dict)]
    work = [e for e in emails if _s(e.get("type")).upper() != "PE"]
    return {
        "id": _s(result.get("uuid")),
        "full_name": _s(result.get("full_name")),
        "title": _s(result.get("title")),
        "headline": _s(result.get("headline"))[:300],
        "linkedin_url": _s(result.get("linkedin_url")),
        "location": ", ".join(p for p in (_s(loc.get("city")), _s(loc.get("state")),
                                          _s(loc.get("country"))) if p),
        "org_uuid": _s(org.get("uuid")),
        "org_name": _s(org.get("name")),
        "org_domain": _host(org.get("website_domain") or org.get("website") or ""),
        "org_industry": _s(org.get("industry")),
        "org_size": _size_label(org.get("number_of_employees")),
        "has_work_email": bool(work) or _int(result.get("n_work_emails")) > 0,
        "has_verified_email": any(e.get("verified") is True for e in work),
    }


def _int(x) -> int:
    return x if isinstance(x, int) and not isinstance(x, bool) else 0


# ── lookups (cached forever in settings: they are SalesQL's own identifiers) ──

async def _lookup(client, state, kind: str, name: str) -> str:
    """SalesQL's identifier for a country or industry name, or ""."""
    name = _s(name)
    if not name:
        return ""
    key = f"salesql:lookup:{kind}:{name.lower()}"
    cached = await state.get_setting(key, "")
    if cached:
        return "" if cached == "-" else cached
    values = await client.lookup(kind, q=name, size=10)
    exact = [v for v in values if v["label"].lower() == name.lower()]
    if not exact and kind == "region":
        # A state is often listed with its country: "California, United States".
        exact = [v for v in values if v["label"].lower().split(",")[0].strip() == name.lower()]
    # A country or state must match by name. An industry may take SalesQL's closest.
    chosen = exact[0] if exact else (values[0] if values and kind == "industry" else None)
    await state.set_setting(key, chosen["value"] if chosen else "-")
    return chosen["value"] if chosen else ""


async def build_filters(client, state, config) -> tuple[dict, list[str]]:
    """The people-search filters for this config, and notes on anything left out."""
    cfg, icp = config.salesql, config.icp
    notes: list[str] = []
    filters: dict = {}

    titles = [t for t in (cfg.job_titles or icp.titles) if _s(t)]
    if not titles:
        raise sq.SalesQLError("No job titles to search for. Set icp.titles or "
                              "salesql.job_titles in harvey.yaml.")
    tech = [w for w in cfg.technical_words if _s(w)]
    filters["job_titles"] = {"include": titles}
    if tech:
        filters["past_job_titles"] = {"exclude": cfg.developer_titles or tech}

    sizes = [s for s in cfg.company_sizes if s in {r[0] for r in SIZE_RANGES}] \
        or sizes_for(icp.company_size)
    if sizes:
        filters["organization_employee_ranges"] = sizes

    countries = []
    for name in (cfg.countries or icp.geography):
        name = COUNTRY_ALIASES.get(_s(name).lower(), _s(name))
        value = await _lookup(client, state, "country", name)
        if value and value not in countries:
            countries.append(value)
        elif not value and cfg.countries:
            notes.append(f"SalesQL has no country called \"{name}\".")
    if countries:
        filters["organization_countries"] = countries
        filters["countries"] = countries

    states = []
    for name in cfg.states:
        value = await _lookup(client, state, "region", name)
        if value and value not in states:
            states.append(value)
        elif not value:
            notes.append(f"SalesQL has no state or city called \"{_s(name)}\".")
    if cfg.states and not states:
        raise sq.SalesQLError("SalesQL recognised none of salesql.states, so the search "
                              "would have covered the whole country. Check the names.")
    if states:
        filters["organization_states_or_cities"] = states
        filters["states_or_cities"] = states

    filters["has_work_emails"] = True

    if cfg.company_keywords:
        org_filters = {"name": [k for k in cfg.company_keywords if _s(k)][:MAX_KEYWORD_SEARCHES * 5]}
        if sizes:
            org_filters["employee_ranges"] = sizes
        if countries:
            org_filters["countries"] = countries
        if states:
            org_filters["states_or_cities"] = states
        uuids: list[str] = []
        for keyword in org_filters["name"][:MAX_KEYWORD_SEARCHES]:
            found = await client.search_organizations({**org_filters, "name": [keyword]},
                                                      page_size=100)
            for org in found["results"]:
                uuid = _s(org.get("uuid"))
                if uuid and uuid not in uuids:
                    uuids.append(uuid)
        if not uuids:
            raise sq.SalesQLError("SalesQL found no companies for salesql.company_keywords "
                                  "with that size and country.")
        filters["organization_uuids"] = uuids[:MAX_ORGS]
    else:
        industries = []
        for name in (cfg.industries or icp.industries):
            value = await _lookup(client, state, "industry", name)
            if value and value not in industries:
                industries.append(value)
            elif not value:
                notes.append(f"SalesQL has no industry matching \"{_s(name)}\".")
        if industries:
            filters["organization_industries"] = industries
    return filters, notes


async def count_developers(client, org_uuids: list[str], developer_titles) -> dict[str, int | None]:
    """People with a software title at each company: {org_uuid: count or None}.

    One search covers up to DEV_CHECK_BATCH companies. A company with no hit
    is only counted as zero when the search returned every match; when the
    page was cut short, that company stays unknown (None) rather than being
    wrongly called developer-free.
    """
    counts: dict[str, int | None] = {u: None for u in org_uuids}
    titles = [t for t in developer_titles if _s(t)]
    if not titles:
        return counts
    batches = [org_uuids[i:i + DEV_CHECK_BATCH]
               for i in range(0, len(org_uuids), DEV_CHECK_BATCH)][:MAX_DEV_SEARCHES]
    for batch in batches:
        found = await client.search_people(
            {"organization_uuids": batch, "job_titles": {"include": titles}}, page_size=100)
        seen: dict[str, int] = {}
        for person in found["results"]:
            uuid = _s(_d(person.get("organization")).get("uuid"))
            if uuid in batch:
                seen[uuid] = seen.get(uuid, 0) + 1
        complete = found["total"] <= len(found["results"])
        attributed = sum(seen.values())
        for uuid in batch:
            if uuid in seen:
                counts[uuid] = seen[uuid]
            elif complete and attributed == len(found["results"]):
                counts[uuid] = 0
    return counts


async def search(state, config, api_key: str, *, transport=None) -> dict:
    """Run one search, grade the results and store them. Spends no credits."""
    cfg = config.salesql
    async with sq.SalesQL(api_key, transport=transport) as client:
        filters, notes = await build_filters(client, state, config)
        found = await client.search_people(filters, page_size=cfg.page_size)
        people = [c for c in (candidate_from_result(r) for r in found["results"]) if c["id"]]
        sizes = set(filters.get("organization_employee_ranges") or [])
        tech = cfg.technical_words
        counts: dict[str, int | None] = {}
        if cfg.require_no_developers:
            # Only companies whose candidate could still qualify are worth a check.
            orgs = []
            for p in people:
                grade, _ = grade_candidate(p, technical_words=tech, allowed_sizes=sizes,
                                           dev_count=0, require_no_developers=True)
                if grade == "qualified" and p["org_uuid"] not in orgs:
                    orgs.append(p["org_uuid"])
            if orgs:
                counts = await count_developers(client, orgs, cfg.developer_titles)

    summary = {"found": len(people), "total": found["total"], "new": 0,
               "qualified": 0, "unsure": 0, "rejected": 0, "notes": notes}
    for p in people:
        dev_count = counts.get(p["org_uuid"])
        grade, reason = grade_candidate(
            p, technical_words=tech, allowed_sizes=sizes, dev_count=dev_count,
            require_no_developers=cfg.require_no_developers)
        p.update(grade=grade, reason=reason, dev_count=dev_count)
        if await state.upsert_salesql_candidate(p):
            summary["new"] += 1
        summary[grade] += 1
    await state.log_action("salesql_search", "scout", {
        k: summary[k] for k in ("found", "new", "qualified", "unsure", "rejected")})
    return summary


# ── reveal ──

def _usage_key() -> str:
    return f"salesql_reveals:{datetime.now(timezone.utc):%Y-%m-%d}"


CAP_KEY = "salesql:max_reveals_per_day"
MAX_CAP = 1000


async def daily_cap(state, config) -> int:
    """The most contacts Harvey reveals in a day: the value set on the
    dashboard when there is one, else salesql.max_reveals_per_day."""
    raw = await state.get_setting(CAP_KEY, "")
    if raw.strip().isdigit() and int(raw) <= MAX_CAP:
        return int(raw)
    return config.salesql.max_reveals_per_day


async def set_daily_cap(state, value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_CAP:
        raise ValueError(f"The daily cap must be a whole number from 0 to {MAX_CAP}.")
    await state.set_setting(CAP_KEY, str(value))
    return value


async def reveals_left_today(state, config) -> int:
    try:
        used = int(await state.get_setting(_usage_key(), "0") or "0")
    except (TypeError, ValueError):
        used = 0
    return max(await daily_cap(state, config) - used, 0)


def pick_email(person: dict, org_domain: str) -> tuple[str, bool]:
    """(address, salesql_verified) of the best work address, or ("", False).

    Personal addresses are never used: this is business outreach. An address
    at the company's own domain wins, because many founders have addresses
    at several businesses and the email is about this one.
    """
    best, best_rank = ("", False), -1
    for e in person.get("emails") or []:
        if not isinstance(e, dict) or _s(e.get("type")).upper() == "PE":
            continue
        address = _s(e.get("email")).lower()
        if "@" not in address or "..." in address or " " in address:
            continue
        verified = e.get("verified") is True or _s(e.get("status")).lower() == "valid"
        at_company = bool(org_domain) and (
            address.endswith("@" + org_domain) or address.endswith("." + org_domain))
        rank = (2 if at_company else 0) + (1 if verified else 0)
        if rank > best_rank:
            best, best_rank = (address, verified), rank
    return best


def pick_phone(person: dict) -> tuple[str, bool]:
    """(number, salesql_verified) of the best phone number in a revealed
    profile, or ("", False). A reveal already returns the numbers SalesQL
    holds, at no extra credit. A number SalesQL marks valid wins, then a
    direct or mobile one over a company switchboard."""
    best, best_rank = ("", False), -1
    for entry in person.get("phones") or []:
        if isinstance(entry, str):
            entry = {"phone": entry}
        if not isinstance(entry, dict):
            continue
        number = next((_s(entry.get(k)) for k in ("phone", "number", "phone_number", "value")
                       if _s(entry.get(k))), "")
        digits = re.sub(r"\D", "", number)
        if not 7 <= len(digits) <= 15 or "*" in number or "..." in number:
            continue
        number = re.sub(r"[^0-9+() .x-]", "", number).strip()[:32]
        verified = entry.get("is_valid") is True or entry.get("verified") is True \
            or _s(entry.get("status")).lower() == "valid"
        kind = _s(entry.get("type")).lower()
        direct = any(word in kind for word in ("mobile", "direct", "cell", "personal"))
        rank = (2 if verified else 0) + (1 if direct else 0)
        if rank > best_rank:
            best, best_rank = (number, verified), rank
    return best


def split_name(full_name: str) -> tuple[str, str]:
    """First and last name, dropping post-nominals after a comma ("Sam Lee, MBA")."""
    name = re.sub(r"\s+", " ", _s(full_name).split(",")[0])
    parts = name.split(" ")
    if len(parts) < 2:
        return name, ""
    return parts[0], " ".join(parts[1:])


async def reveal(state, config, api_key: str, candidate_ids: list[str], *,
                 env=None, verify=None, transport=None) -> dict:
    """Reveal the ticked candidates and turn them into prospects.

    Spends up to one SalesQL credit per person found. Trimmed to what the
    daily cap and the account's credits allow; the rest stay ``found``.
    """
    from harvey.config import live_do_not_contact
    from harvey.gate import do_not_contact_domain_match, do_not_contact_match
    if verify is None:
        from harvey.integrations.email_finder import verify_published_address as verify

    summary = {"asked": len(candidate_ids), "revealed": 0, "no_email": 0, "skipped": 0,
               "failed": 0, "held_back": 0, "credits_used": 0, "verifications_used": 0,
               "messages": []}
    rows = [r for r in await state.get_salesql_candidates(ids=list(dict.fromkeys(candidate_ids)))
            if r["status"] == "found"]
    if not rows:
        summary["messages"].append("Nobody to reveal: those people were already handled.")
        return summary

    blocked = live_do_not_contact(config)
    keep = []
    for r in rows:
        if r["org_domain"] and do_not_contact_domain_match(r["org_domain"], blocked):
            await state.update_salesql_candidate(
                r["id"], status="skipped", note="Company is on your do-not-contact list.")
            summary["skipped"] += 1
        else:
            keep.append(r)
    rows = keep

    left = await reveals_left_today(state, config)
    async with sq.SalesQL(api_key, transport=transport) as client:
        account = await client.account_status()
        allowed = min(left, account["credits"], sq.BULK_LIMIT)
        if len(rows) > allowed:
            summary["held_back"] = len(rows) - allowed
            why = ("today's cap on reveals" if left <= account["credits"]
                   else "the SalesQL credits you have left")
            summary["messages"].append(
                f"{summary['held_back']} held back by {why}.")
            rows = rows[:allowed]
        if not rows:
            return summary
        requests = []
        for r in rows:
            if r["linkedin_url"]:
                requests.append({"linkedin_url": r["linkedin_url"]})
            elif r["org_domain"]:
                requests.append({"full_name": r["full_name"],
                                 "organization_domain": r["org_domain"]})
            else:
                requests.append({"full_name": r["full_name"],
                                 "organization_name": r["org_name"]})
        try:
            people = await client.enrich_people(requests)
        except sq.SalesQLError:
            # The request may have been charged. Count it against today's cap
            # so a retry loop can't run the balance down.
            await state.increment_setting(_usage_key(), len(rows))
            raise

    matched = sum(1 for p in people if p)
    summary["credits_used"] = matched
    await state.increment_setting(_usage_key(), matched)
    confirmed = await state.confirmed_signal_codes()

    checker = sq.SalesQL(api_key, transport=transport)
    try:
        await _file_people(state, config, rows, people, summary, blocked, confirmed,
                           verify, env, checker)
    finally:
        await checker.aclose()

    await state.log_action("salesql_reveal", "scout", {
        k: summary[k] for k in ("revealed", "no_email", "skipped", "failed", "credits_used",
                                "verifications_used")})
    return summary


CHECKS_AT_ONCE = 5         # address checks running side by side


async def _salesql_checks(checker, emails: list[str], summary: dict) -> dict[str, str]:
    """SalesQL's verdict on each address: {email: valid|invalid|catch_all|unknown}.

    A few run side by side, since one check can take most of a minute. The
    first refusal stops the checks not yet started: a key without the Verify
    scope or a spent balance fails every time, and a failed call may still
    have been charged. An address with no entry was not checked.
    """
    answers: dict[str, str] = {}
    emails = list(dict.fromkeys(emails))
    if not emails:
        return answers
    gate = asyncio.Semaphore(CHECKS_AT_ONCE)
    stopped: list[str] = []

    async def one(email):
        async with gate:
            if stopped:
                return
            try:
                answers[email] = await checker.verify_email(email)
            except sq.SalesQLError as e:
                stopped.append(str(e))

    await checker.account_status()      # open the session once, before the checks share it
    await asyncio.gather(*(one(e) for e in emails))
    summary["verifications_used"] += len(answers)
    if stopped:
        summary["messages"].append(
            f"SalesQL couldn't check {len(emails) - len(answers)} of {len(emails)} addresses "
            f"({stopped[0]}) If the key lacks the Verify scope, tick it on the key in SalesQL.")
    return answers


async def _file_people(state, config, rows, people, summary, blocked, confirmed,
                       verify, env, checker):
    """Turn each revealed profile into a prospect with a checked address."""
    from harvey.gate import do_not_contact_match
    ready = []      # [row, person, email, sq_verified, phone, phone_verified, status]
    for row, person in zip(rows, people):
        if not person:
            await state.update_salesql_candidate(
                row["id"], status="failed", note="SalesQL returned no profile.")
            summary["failed"] += 1
            continue
        email, sq_verified = pick_email(person, row["org_domain"])
        phone, phone_verified = pick_phone(person)
        if not email:
            await state.update_salesql_candidate(
                row["id"], status="no_email",
                note="No work email in the SalesQL profile."
                     + (f" Phone: {phone}" if phone else ""))
            summary["no_email"] += 1
            continue
        if do_not_contact_match(email, blocked):
            await state.update_salesql_candidate(
                row["id"], status="skipped", note="Address is on your do-not-contact list.")
            summary["skipped"] += 1
            continue
        try:
            status = await verify(email, env=env)
        except Exception as e:  # a verifier outage must not lose a paid-for contact
            logger.warning("Email check failed for a SalesQL contact: %s", type(e).__name__)
            status = "guess"
        ready.append([row, person, email, sq_verified, phone, phone_verified, status])

    answers = await _salesql_checks(
        checker, [r[2] for r in ready if r[6] == "guess"], summary) \
        if config.salesql.verify_emails else {}

    for row, person, email, sq_verified, phone, phone_verified, status in ready:
        if status == "guess" and email in answers:
            # "unknown" is an answer too: the mail server looked wrong.
            status = {"valid": "verified", "invalid": "invalid",
                      "catch_all": "risky"}.get(answers[email], "guess")
        elif status == "guess" and sq_verified and config.salesql.trust_verified:
            status = "verified"

        first, last = split_name(person.get("full_name") or row["full_name"])
        domain = email.partition("@")[2] if not row["org_domain"] else row["org_domain"]
        company_id = await state.add_company(Company(
            name=row["org_name"], domain=domain,
            website=f"https://{domain}" if domain else "",
            industry=row["org_industry"], company_size=row["org_size"],
            source=COLLECTOR, external_id=f"salesql:{row['org_uuid']}" if row["org_uuid"] else ""))
        notes = [f"Found through SalesQL. {row['reason']}".strip()]
        if row["headline"]:
            notes.append(f"Headline: {row['headline']}")
        prospect_id = await state.add_prospect(Prospect(
            company_id=company_id, first_name=first, last_name=last, email=email,
            email_verified=status == "verified", email_status=status,
            phone=phone, phone_verified=phone_verified,
            linkedin_url=row["linkedin_url"], title=_s(person.get("title")) or row["title"],
            source=COLLECTOR, source_url=row["linkedin_url"], score=70,
            personalization_notes="\n".join(notes), company=row["org_name"],
            industry=row["org_industry"], company_size=row["org_size"]))
        await state.update_salesql_candidate(
            row["id"], status="revealed", prospect_id=prospect_id,
            note={"verified": "Address verified.", "risky": "Address is on a catch-all domain.",
                  "invalid": "Address failed verification and will not be emailed.",
                  }.get(status, "Address not verified yet, so it will not be emailed."))
        if "INHOUSE_DEV_COUNT" in confirmed and row["dev_count"] is not None:
            await state.add_observation(
                "INHOUSE_DEV_COUNT", company_id=company_id, collector=COLLECTOR,
                value_num=float(row["dev_count"]), confidence=0.7)
        if "FOUNDER_NON_TECHNICAL" in confirmed:
            await state.add_observation(
                "FOUNDER_NON_TECHNICAL", company_id=company_id, prospect_id=prospect_id,
                collector=COLLECTOR, value_num=1.0, value_text=row["title"][:120],
                confidence=0.7)
        summary["revealed"] += 1
