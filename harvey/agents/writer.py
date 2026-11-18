"""Writer — crafts personalized email sequences.

With a native mail provider (Gmail/SMTP), email 1 is written PER PROSPECT
from grounded facts — the company's own description, detected tech stack,
and buying signals — instead of one merge-tag template shared by a whole
batch. Mass-templated "token-swap" mail is exactly what inbox filters now
cluster and junk; a specific, verifiable first line is what earns replies.
Steps 2-3 stay campaign-level (proof point + breakup are less personal by
design). Every draft still passes the deterministic pre-send gate.
"""

import json
import logging
import re
from datetime import datetime, timezone

from harvey.brain import Brain
from harvey.config import EmailRulesError, HarveyConfig, compliance_of, load_email_rules
from harvey.humanize import humanize, humanizer_enabled
from harvey.integrations.mail_provider import NATIVE_PROVIDERS
from harvey.models.campaign import Campaign, EmailStep
from harvey.state import WRITABLE_EMAIL_STATUSES, StateManager
from harvey.sending_mode import sync_sending_mode
from harvey.startups import startup_facts
from harvey.untrusted import UNTRUSTED_RULE, fence

logger = logging.getLogger("harvey.writer")

# How many times the model may fail to write a usable sequence for a
# prospect before Harvey stops trying (status 'write_failed'). A provider
# outage never counts; only a reply that came back unusable does.
MAX_WRITE_ATTEMPTS = 3
WRITE_ATTEMPTS_KEY = "write_attempts:"

# Merge variables that name a person; a company inbox has none of them.
PERSON_VAR = re.compile(r"\{\{\s*(first_?name|last_?name|title)\s*\}\}", re.IGNORECASE)

# Native mode: one Claude call per prospect for email 1, so batches stay
# small — the rest of the 'new' pool is picked up on later cycles.
NATIVE_BATCH_CAP = 8
LEGACY_BATCH_CAP = 50

# The built-in "how" lines each prompt ends with: email 1 is an observation
# and a question, with no pitch. Operator email rules replace exactly these.
PERSONAL_SHAPE = """- Under 75 words. One specific observation from the FACTS above, one
  question. No pitch, no product name.
- Write the actual text (no merge variables — you know their name/company).
- Subject: lowercase, 2-5 words."""
SEQUENCE_SHAPE = """- Email 1: Personalized cold observation + one question. Under 75 words. No pitch.
- Email 2: Follow-up 3 days later. Different angle, one concrete proof point. Under 75 words.
- Email 3: Break-up email 4 days after that. Under 40 words. Gracious, not guilt-tripping."""
# Days to wait before email 2, 3, ... for each sequence length, when the
# user hasn't set their own (writer.sequence_delays). About 6 touches over
# 14 to 28 days is what the reply data favours; 3 is the long-standing default.
DEFAULT_DELAYS = {2: [4], 3: [3, 4], 4: [3, 4, 7], 5: [3, 4, 7, 7], 6: [3, 4, 5, 7, 7]}
# The built-in A/B pair: an easy question against a give-first offer.
AB_ANGLE_A = ("Lead with one specific observation about their business and end with one "
              "easy question they can answer in a line.")
AB_ANGLE_B = ("Lead with one specific observation, then offer one useful thing they get "
              "whether or not they buy, taken only from the product information above, "
              "and ask if they want it.")
CAMPAIGN_VARIANT_KEY = "campaign_variant:"


def sequence_plan(config) -> list[int]:
    """delay_days for each email of a sequence, first one 0."""
    writer = getattr(config, "writer", None)
    steps = int(getattr(writer, "sequence_steps", 3) or 3)
    steps = min(6, max(2, steps))
    delays = list(getattr(writer, "sequence_delays", None) or [])
    if len(delays) != steps - 1:
        delays = DEFAULT_DELAYS[steps]
    return [0] + [int(d) for d in delays]


def sequence_shape(plan: list[int]) -> str:
    """What each email of the sequence is, one line each."""
    last = len(plan)
    lines = ["- Email 1: Personalized cold observation + one question. Under 75 words. No pitch."]
    for n in range(2, last + 1):
        when = f"{plan[n - 1]} days " + ("later" if n == 2 else "after that")
        if n == last:
            lines.append(f"- Email {n}: Break-up email {when}. Under 40 words. "
                         "Gracious, not guilt-tripping.")
        elif n == 2:
            lines.append(f"- Email 2: Follow-up {when}. Different angle, one concrete "
                         "proof point. Under 75 words.")
        else:
            lines.append(f"- Email {n}: Bump {when}. One or two sentences, under 30 words. "
                         "One new small reason to reply or one easy question. Never "
                         "\"just following up\", never a re-pitch.")
    if last != 3:
        lines.append(f"- Where the STRICT EMAIL RULES give a word limit for \"Email 3\", "
                     f"it applies to the last email here (email {last}).")
    return "\n".join(lines)


def tag_links(text: str, config, campaign_id: str) -> str:
    """Add ?ref=h-<campaign> to links on the user's own domains, when
    channels.email.tag_links is on. Other links are left alone."""
    if not text or not campaign_id or not getattr(config.channels.email, "tag_links", False):
        return text
    from harvey.gate import allowed_link_domains
    try:
        own = allowed_link_domains(config)
    except Exception:
        return text
    tag = "ref=h-" + re.sub(r"[^A-Za-z0-9]", "", str(campaign_id))[:8]

    def one(match):
        url = match.group(0)
        trail = ""
        while url and url[-1] in ".,;:!?)'\"":
            url, trail = url[:-1], url[-1] + trail
        host = re.sub(r"^https?://", "", url).split("/")[0].split("?")[0].split("#")[0].lower()
        host = host.split("@")[-1].split(":")[0]
        if not any(host == d or host.endswith("." + d) for d in own) or "ref=" in url:
            return url + trail
        base, _, frag = url.partition("#")
        base += ("&" if "?" in base else "?") + tag
        return base + (("#" + frag) if frag else "") + trail

    return re.sub(r"https?://[^\s<>]+", one, text)


SEQUENCE_STYLE = """- Never be pushy or salesy. Be consultative and value-driven.
- Subject lines: lowercase, 2-5 words."""
# Why the Writer stopped: Today and `harvey status` show it.
EMAIL_RULES_PROBLEM_KEY = "email_rules_problem"
BUILT_IN_FOOTER = "- No postal address, unsubscribe or opt-out line: Harvey adds them when it sends."


def operator_rules_block(rules: str) -> str:
    """The operator's email rules as a prompt section ("" when there are none).

    Trusted text from the person running Harvey, so it isn't fenced. It wins
    over the built-in style and shape rules, never over the FACTS rule."""
    if not rules:
        return ""
    return (
        "\n\nOPERATOR'S EMAIL RULES — written by the person running Harvey. For\n"
        "length, structure, what to say about the product, links, subject lines\n"
        "and sign-off, these replace the STRICT EMAIL RULES and skills above\n"
        "wherever the two disagree. They never override the FACTS rule: what you\n"
        "say about the prospect comes only from the facts given, never invented.\n"
        "Nor do they allow a fake \"Re:\" or \"Fwd:\" subject line, or a claim of a\n"
        "conversation that never happened.\n"
        + rules
    )


def footer_rule(config, rules: str) -> str:
    """What the prompt says about the lines Harvey adds when it sends.

    With operator rules it names only what Harvey really adds: an opt-out
    sentence the operator writes into the body must not sit on top of the
    configured opt_out_line, and must not be dropped when there is none."""
    if not rules:
        return BUILT_IN_FOOTER
    compliance = compliance_of(config)
    lines = []
    if compliance.postal_address.strip():
        lines.append("- No postal address: Harvey adds it under the email when it sends.")
    opt_out = compliance.opt_out_line.strip()
    if opt_out:
        lines.append(
            f'- Harvey adds this opt-out line under the email when it sends: "{opt_out}".\n'
            "  Don't write another way to opt out, even if the operator's rules give one.")
    else:
        lines.append(
            "- Harvey adds no opt-out line. If the operator's rules give a sentence that\n"
            "  lets the reader say no, write it into the body as they say.")
    return "\n".join(lines)


class Writer:
    def __init__(
        self,
        brain: Brain,
        state: StateManager,
        config: HarveyConfig,
        env=None,
    ):
        self.brain = brain
        self.state = state
        self.config = config
        self.env = env

    @property
    def is_native(self) -> bool:
        return self.config.channels.email.provider in NATIVE_PROVIDERS

    async def run(self):
        """Create email campaigns for prospects that need outreach."""
        logger.info("Writer: Crafting email campaigns...")

        # Load foundational skills for this agent
        self.skills = self.brain.load_skills_for_agent("writer")
        try:
            self.email_rules = load_email_rules(self.config)
        except EmailRulesError as e:
            # The operator replaced the built-in shape on purpose: drafting in
            # it anyway would put emails they didn't want in the outbox.
            logger.warning(
                f"Writer: your email rules (writer.email_rules) can't be used: {e}. "
                "Writing nothing until that's fixed.")
            await self.state.set_setting(EMAIL_RULES_PROBLEM_KEY, str(e))
            return
        if await self.state.get_setting(EMAIL_RULES_PROBLEM_KEY):
            await self.state.delete_setting(EMAIL_RULES_PROBLEM_KEY)

        # Get prospects that haven't been contacted yet
        new_prospects = await self.state.get_prospects_by_status("new")
        if not new_prospects:
            logger.info("Writer: No new prospects to write for.")
            return

        # Only write for prospects we can actually deliver to. Guessed and
        # invalid addresses are skipped so we don't spend Claude calls (or
        # sending reputation) on mail that will bounce.
        prospects_with_email = [
            p for p in new_prospects
            if p.email and (p.email_status or "guess") in WRITABLE_EMAIL_STATUSES
        ]
        # Someone at their company already replied: no cold email to write.
        prospects_with_email = [
            p for p in prospects_with_email
            if not await self.state.colleague_who_replied(p.id)
        ]
        if not prospects_with_email:
            logger.info(
                "Writer: No prospects with deliverable (verified/risky) emails yet."
            )
            return

        # Batch prospects into campaign groups (by industry/title for relevance)
        batches = self._group_prospects(prospects_with_email)

        batches = self._ab_split(batches)

        for batch_name, prospects in batches.items():
            if not prospects:
                continue
            angle = self._batch_angles.get(batch_name, ("", ""))

            logger.info(
                f"Writer: Creating campaign '{batch_name}' for "
                f"{len(prospects)} prospects."
            )

            # Generate the email sequence
            self._no_reply = False
            sequence = await self._write_sequence(prospects, angle=angle[1])
            if not sequence:
                paused = getattr(self.brain, "paused_reason", "") or (
                    "no reply from the model" if self._no_reply else "")
                if paused:
                    # The provider failed (outage, logout, rate limit,
                    # timeout), not the draft: no attempt is counted, and the
                    # other batches would fail the same way.
                    logger.warning(f"Writer: {paused}; leaving '{batch_name}' for later.")
                    return
                logger.warning(f"Writer: Failed to generate sequence for {batch_name}")
                await self._count_failed_attempt(batch_name, prospects)
                continue

            # Create the campaign
            campaign = Campaign(
                id="",
                name=batch_name,
                channel="email",
                sequence=sequence,
                prospect_ids=[p.id for p in prospects],
                status="draft",
            )
            campaign_id = await self.state.add_campaign(campaign)
            if angle[0]:
                await self.state.set_setting(
                    CAMPAIGN_VARIANT_KEY + campaign_id,
                    json.dumps({"variant": angle[0], "angle": angle[1]}))

            # Mark prospects so they aren't picked up again
            for p in prospects:
                await self.state.queue_prospect_if_unsent(p.id)
                await self.state.delete_setting(WRITE_ATTEMPTS_KEY + p.id)

            # Native providers: replace the shared template for email 1 with
            # a per-prospect draft grounded in that company's actual facts.
            if self.is_native:
                await self._personalize_first_emails(campaign_id, prospects, angle=angle[1])

            await self.state.log_action(
                action_type="write_campaign",
                agent="writer",
                details={
                    "campaign_id": campaign_id,
                    "campaign_name": batch_name,
                    "prospect_count": len(prospects),
                    "steps": len(sequence),
                },
            )
            logger.info(
                f"Writer: Campaign '{batch_name}' created with "
                f"{len(sequence)} emails for {len(prospects)} prospects."
            )

    _batch_angles: dict = {}

    def _ab_split(self, batches: dict) -> dict:
        """With writer.ab_test on, split each batch that is big enough into
        an A half and a B half (alternating, so both halves look alike), and
        remember the angle each half is written with."""
        self._batch_angles = {}
        writer = getattr(self.config, "writer", None)
        if not getattr(writer, "ab_test", False):
            return batches
        minimum = int(getattr(writer, "ab_min_prospects", 20) or 20)
        angle_a = getattr(writer, "ab_angle_a", "") or AB_ANGLE_A
        angle_b = getattr(writer, "ab_angle_b", "") or AB_ANGLE_B
        out = {}
        for name, prospects in batches.items():
            if len(prospects) < minimum:
                out[name] = prospects
                continue
            out[name + " (A)"], out[name + " (B)"] = prospects[0::2], prospects[1::2]
            self._batch_angles[name + " (A)"] = ("A", angle_a)
            self._batch_angles[name + " (B)"] = ("B", angle_b)
        return out

    async def _results_block(self) -> str:
        """How earlier campaigns did, for the Writer to lean on. Empty until
        writer.learn_from_results is on and a campaign has enough sends."""
        writer = getattr(self.config, "writer", None)
        if not getattr(writer, "learn_from_results", False):
            return ""
        try:
            rows = await self.state.campaign_results(
                int(getattr(writer, "learn_min_sent", 20) or 20))
        except Exception as e:
            logger.warning(f"Writer: could not read campaign results: {e}")
            return ""
        if not rows:
            return ""
        lines = []
        for r in rows[:8]:
            rate = round(r["replies"] / r["sent"] * 100, 1) if r["sent"] else 0
            line = (f"- {r['sent']} sent, {r['replies']} replies ({rate}%), "
                    f"{r['interested']} interested")
            if r.get("angle"):
                line += f" | angle: {r['angle']}"
            if r.get("subject"):
                line += f" | follow-up subject: {r['subject']}"
            lines.append(line)
        return (
            "\n\nRESULTS SO FAR — how this business's earlier campaigns did, best reply\n"
            "rate first. Lean toward the angles and subject styles that earned replies\n"
            "and away from those that did not. Small numbers are weak evidence: treat a\n"
            "difference of a reply or two as noise. This never overrides the STRICT\n"
            "EMAIL RULES or the FACTS rule:\n" + "\n".join(lines)
        )

    async def _count_failed_attempt(self, batch_name: str, prospects: list):
        """Count one unusable sequence against each prospect in the batch.

        A prospect left 'new' makes the heartbeat choose write_campaign every
        cycle, so after MAX_WRITE_ATTEMPTS it moves to 'write_failed': visible
        in status and the dashboard, and out of the way of prospecting.
        """
        given_up = []
        for p in prospects:
            key = WRITE_ATTEMPTS_KEY + p.id
            try:
                attempts = int(await self.state.get_setting(key) or 0) + 1
            except ValueError:
                attempts = 1
            if attempts >= MAX_WRITE_ATTEMPTS:
                await self.state.update_prospect_status(p.id, "write_failed")
                await self.state.delete_setting(key)
                given_up.append(p.email)
            else:
                await self.state.set_setting(key, str(attempts))
        if given_up:
            await self.state.log_action(
                action_type="write_failed",
                agent="writer",
                details={"campaign_name": batch_name, "prospects": given_up,
                         "attempts": MAX_WRITE_ATTEMPTS},
            )
            logger.warning(
                f"Writer: Gave up on {len(given_up)} prospect(s) in '{batch_name}' after "
                f"{MAX_WRITE_ATTEMPTS} unusable drafts; marked write_failed."
            )

    async def _personalize_first_emails(self, campaign_id: str, prospects: list, angle: str = ""):
        """Draft a grounded, per-prospect email 1 and stage it in the outbox.

        The outbox unique index means the sender's later template staging
        can't overwrite these rows — a personalized draft always wins the
        (campaign, prospect, step 1) slot. If a draft fails, the slot stays
        empty and the sender falls back to the campaign template.
        """
        await sync_sending_mode(self.config, self.state)
        require_approval = getattr(
            self.config.channels.email, "require_approval", True
        )
        status = "pending_review" if require_approval else "approved"
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        provider = self.config.channels.email.provider

        pending = []
        for prospect in prospects:
            try:
                draft = await self._write_personal_email(prospect, angle=angle)
            except Exception as e:
                logger.warning(f"Writer: personal draft failed for {prospect.email}: {e}")
                continue
            if draft:
                pending.append((prospect, draft))

        # One Humanizer pass over the whole batch before anything is staged.
        polished = await humanize(
            self.brain, [d for _, d in pending], agent="writer",
            enabled=humanizer_enabled(self.config),
        )

        drafted = 0
        for (prospect, _), draft in zip(pending, polished):
            item_id = await self.state.add_outbox_item(
                prospect_id=prospect.id,
                campaign_id=campaign_id,
                step=1,
                to_email=prospect.email,
                subject=draft["subject"],
                body=tag_links(draft["body"], self.config, campaign_id),
                send_at=now,
                status=status,
                provider=provider,
                approved_by="" if require_approval else "autopilot",
            )
            if item_id:
                drafted += 1

        if drafted:
            logger.info(
                f"Writer: drafted {drafted} personalized first email(s) "
                f"({'awaiting approval' if require_approval else 'approved'})."
            )

    async def _startup(self, company_id) -> list[str] | None:
        """Fact lines when the company is a startup (YC, another accelerator,
        a raise it announced; see harvey.startups), else None."""
        return await startup_facts(getattr(self, "state", None), company_id)

    def _startup_playbook(self) -> str:
        """skills/selling_to_startups.md, for startup prospects ("" if absent)."""
        text = self.brain.load_skill("selling_to_startups") if hasattr(self.brain, "load_skill") else ""
        return "\n\nSELLING TO STARTUP FOUNDERS\n" + text if text else ""

    async def _feedback_block(self, prospect_ids: list[str]) -> str:
        """The owner's notes from the dashboard's Feedback buttons, as prompt
        text ("" when there are none). Never lets a failed read stop writing."""
        try:
            notes = await self.state.get_writing_feedback(prospect_ids)
        except Exception as e:
            logger.warning(f"Writer: could not read feedback: {e}")
            return ""
        if not notes:
            return ""
        lines = "\n".join(
            f"- {str(n.get('created_at') or '')[:10]} on {n['about']}: "
            f"{' '.join(str(n['comment']).split())[:500]}" for n in notes)
        return (
            "\n\nOWNER FEEDBACK — notes the person running Harvey left on earlier\n"
            "campaigns and contacts, newest first (a newer note wins). Apply them to\n"
            "this writing. They never override the STRICT EMAIL RULES or the FACTS rule:\n"
            + lines
        )

    async def _write_personal_email(self, prospect, angle: str = "") -> dict | None:
        """One grounded draft for one person. Facts in, one email out."""
        inbox_rule = ""
        if prospect.is_company_inbox:
            facts = [
                f"- Name: unknown (the company's general inbox, {prospect.email})",
                f"- Company: {prospect.company}",
            ]
            inbox_rule = (
                "\n- This goes to the company's general inbox, most likely read by the owner"
                "\n  or the office manager. Use no first name; open with 'Hi there' or"
                "\n  address the company's team.")
        else:
            facts = [
                f"- Name: {prospect.full_name()}",
                f"- Title: {prospect.title}",
                f"- Company: {prospect.company}",
            ]
        if prospect.industry:
            facts.append(f"- Industry: {prospect.industry}")

        company = None
        if prospect.company_id:
            try:
                company = await self.state.get_company(prospect.company_id)
            except Exception:
                company = None
        if company:
            if company.description:
                facts.append(f"- What the company says about itself: {company.description}")
            if company.tech_stack:
                facts.append(f"- Tools detected on their website: {', '.join(company.tech_stack[:6])}")
            for signal in company.signals[:3]:
                facts.append(
                    f"- Signal ({signal.get('type', 'signal')}): {signal.get('detail', '')}"
                )
        if prospect.personalization_notes:
            facts.append(f"- Research notes: {prospect.personalization_notes}")
        startup = await self._startup(prospect.company_id)
        if startup is not None:
            facts.extend(startup)

        prompt = self.brain.load_prompt(
            "writer",
            product_name=self.config.product.name,
            product_description=self.config.product.description,
            product_benefits="\n".join(f"- {b}" for b in self.config.product.key_benefits),
            product_pricing=self.config.product.pricing,
            persona_name=self.config.persona.name,
            persona_company=self.config.persona.company,
            persona_role=self.config.persona.role,
            persona_tone=self.config.persona.tone,
        ) or ""
        if self.skills:
            prompt += "\n\n" + self.skills
        if startup is not None:
            prompt += self._startup_playbook()
        prompt += await self._feedback_block([prospect.id])
        prompt += await self._results_block()
        rules = getattr(self, "email_rules", "")
        prompt += operator_rules_block(rules)
        if rules:
            shape = (
                "- Follow the OPERATOR'S EMAIL RULES above for this first email: its length,\n"
                "  structure, what it says about the product, links and subject line.\n"
                "- Write the actual text (no merge variables — you know their name/company).")
            strict = "- Follow every STRICT EMAIL RULE above that the operator's rules don't replace."
        else:
            shape = PERSONAL_SHAPE
            strict = "- Follow every STRICT EMAIL RULE above."

        prompt += f"""

Write ONE cold email (the very first touch) to this specific person.

FACTS — everything you know about them, gathered from their website and
the web. Every claim about the prospect in your email must come from these
facts. If a fact isn't listed here, you don't know it — do not invent
funding rounds, mutual contacts, metrics, or anything else:
{fence(chr(10).join(facts), "prospect research")}

{UNTRUSTED_RULE}

Requirements:
{shape}
{footer_rule(self.config, rules)}
{strict}{inbox_rule}{self._angle_rule(angle)}

Return ONLY JSON: {{"subject": "...", "body": "..."}}"""

        result = await self.brain.think_json(
            prompt, session_id="harvey-writer",
            agent="writer", task="personal_email",
        )
        if not isinstance(result, dict):
            return None
        subject = str(result.get("subject") or "").strip()
        body = str(result.get("body") or "").strip()
        if not subject or not body:
            return None
        return {"subject": subject[:120], "body": body[:2000]}

    @staticmethod
    def _angle_rule(angle: str) -> str:
        if not angle:
            return ""
        return ("\n- ANGLE FOR THIS TEST GROUP (set by the person running Harvey; it shapes\n"
                "  the first email and the follow-up, inside every rule above): " + angle)

    async def _write_sequence(self, prospects: list, angle: str = "") -> list[EmailStep]:
        """Ask the brain to write the sequence (writer.sequence_steps emails)."""
        plan = sequence_plan(self.config)
        count = len(plan)
        # Build context about the prospects
        prospect_summary = "\n".join(
            f"- {p.full_name()}, {p.title} at {p.company}"
            + (f" | Notes: {p.personalization_notes}" if p.personalization_notes else "")
            for p in prospects[:5]  # Show sample for context
        )
        inbox_batch = all(p.is_company_inbox for p in prospects)
        if inbox_batch:
            merge_rule = (
                "- These go to each company's general inbox, so no names are known.\n"
                "  Use {{company}} as the merge variable in every email and NEVER\n"
                "  {{first_name}} or {{title}}. Greet with 'Hi there' or the company's team."
            )
        else:
            merge_rule = (
                "- Use {{first_name}}, {{company}}, {{title}} as merge variables — every email\n"
                "  must use at least one, and email 1 must reference something specific to\n"
                "  these prospects' industry or role (use the notes above)."
            )

        prompt = self.brain.load_prompt(
            "writer",
            product_name=self.config.product.name,
            product_description=self.config.product.description,
            product_benefits="\n".join(f"- {b}" for b in self.config.product.key_benefits),
            product_pricing=self.config.product.pricing,
            persona_name=self.config.persona.name,
            persona_company=self.config.persona.company,
            persona_role=self.config.persona.role,
            persona_tone=self.config.persona.tone,
        )

        if not prompt:
            prompt = f"""You are {self.config.persona.name}, {self.config.persona.role} at {self.config.persona.company}.
Your tone is: {self.config.persona.tone}

Product: {self.config.product.name}
Description: {self.config.product.description}
Key benefits: {', '.join(self.config.product.key_benefits)}
Pricing: {self.config.product.pricing}"""

        # Inject email framework skills
        if self.skills:
            prompt += "\n\n" + self.skills
        # One template goes to the whole batch: founder framing only when
        # every prospect in it is a startup.
        backed = [await self._startup(p.company_id) for p in prospects]
        if prospects and all(b is not None for b in backed):
            prompt += self._startup_playbook()
        # Campaign notes only: this template goes to the whole batch, so a
        # note about one person ("met her at the expo") must never reach it.
        prompt += await self._feedback_block([])
        prompt += await self._results_block()
        rules = getattr(self, "email_rules", "")
        prompt += operator_rules_block(rules)
        if rules:
            if count == 3 and plan == [0, 3, 4]:
                timing = ("- Email 1 is the first touch, email 2 a follow-up 3 days later, email 3 a\n"
                          "  last one 4 days after that.")
            else:
                timing = ("- Email 1 is the first touch; " + "; ".join(
                    f"email {n} follows {plan[n - 1]} days after email {n - 1}"
                    for n in range(2, count + 1)) + f". Email {count} is the last one.")
            shape = (
                timing + " Follow the OPERATOR'S EMAIL RULES above for\n"
                "  each one's length, structure, what it says about the product, links and\n"
                "  subject line.")
            style = ""
            strict = ("- Follow every rule in the STRICT EMAIL RULES above that the operator's\n"
                      "  rules don't replace.")
        else:
            shape, style = sequence_shape(plan), "\n" + SEQUENCE_STYLE
            strict = "- Follow every rule in the STRICT EMAIL RULES above. No exceptions."

        example = ",\n".join(
            f'  {{"step": {n}, "subject": "...", "body": "...", "delay_days": {d}}}'
            for n, d in enumerate(plan, 1))
        prompt += f"""

Write a {count}-email cold outreach sequence for prospects like these:
{fence(prospect_summary, "sample prospects")}

{UNTRUSTED_RULE}

Requirements:
{shape}
{merge_rule}{style}
{footer_rule(self.config, rules)}
{strict}{self._angle_rule(angle)}

Return ONLY a JSON array of exactly {count} emails (no markdown fences, no commentary):
[
{example}
]"""

        result = await self.brain.think_json(
            prompt, session_id="harvey-writer",
            agent="writer", task="write_sequence",
        )
        # Read right after the call, before anything else can use the brain.
        self._no_reply = result is None and bool(getattr(self.brain, "last_reply_empty", False))
        steps = self._parse_sequence(result, expected=count)
        # The spacing is the user's setting, not the model's choice.
        for step, delay in zip(steps, plan):
            step.delay_days = delay
        if inbox_batch and any(
            PERSON_VAR.search(s.subject + s.body) for s in steps
        ):
            # Nobody's name is known: "Hi {{first_name}}" or "as {{title}}"
            # would render as "Hi there" at best and "as General inbox" at worst.
            logger.warning("Writer: inbox sequence used a person merge variable; discarding it.")
            return []
        polished = await humanize(
            self.brain, [{"subject": s.subject, "body": s.body} for s in steps],
            agent="writer", enabled=humanizer_enabled(self.config),
        )
        for step, draft in zip(steps, polished):
            step.subject, step.body = draft["subject"], draft["body"]
        return steps

    def _parse_sequence(self, result, expected: int = 3) -> list[EmailStep]:
        """Robustly coerce LLM output into a validated EmailStep list.

        Never raises. Tolerates a wrapper dict ({"emails": [...]}), missing
        or wrong-typed fields, and extra keys. Drops invalid steps rather
        than failing the whole sequence.
        """
        if isinstance(result, dict):
            # Model wrapped the array in an object — unwrap common keys.
            for key in ("emails", "sequence", "steps", "campaign"):
                if isinstance(result.get(key), list):
                    result = result[key]
                    break
        if not isinstance(result, list) or not result:
            logger.error(f"Writer: Brain did not return an email array (got {type(result).__name__}).")
            return []

        steps: list[EmailStep] = []
        for i, raw in enumerate(result[:max(5, expected)]):  # never accept absurdly long sequences
            if not isinstance(raw, dict):
                logger.warning(f"Writer: Skipping non-dict step at index {i}.")
                continue
            subject = str(raw.get("subject") or "").strip()
            body = str(raw.get("body") or "").strip()
            if not subject or not body:
                logger.warning(f"Writer: Skipping step {i + 1} with empty subject/body.")
                continue
            try:
                delay = max(0, int(raw.get("delay_days", 3 if steps else 0)))
            except (TypeError, ValueError):
                delay = 3 if steps else 0
            try:
                steps.append(
                    EmailStep(
                        step=len(steps) + 1,
                        subject=subject,
                        body=body,
                        delay_days=delay,
                    )
                )
            except Exception as e:
                logger.warning(f"Writer: Skipping invalid step {i + 1}: {e}")

        if steps and steps[0].delay_days != 0:
            steps[0].delay_days = 0  # first email always sends immediately

        if len(steps) < expected:
            logger.warning(f"Writer: Expected {expected} emails, got {len(steps)}.")
        return steps

    def _group_prospects(self, prospects: list) -> dict[str, list]:
        """Group prospects into campaign batches by industry/title combo."""
        batches: dict[str, list] = {}

        for prospect in prospects:
            # Group by industry + rough title category
            industry = prospect.industry or "general"
            # Inbox prospects get their own campaign: their templates can't
            # greet anyone by name.
            kind = "inbox-outreach" if prospect.is_company_inbox else "outreach"
            key = f"{industry}-{kind}"

            if key not in batches:
                batches[key] = []
            batches[key].append(prospect)

        # Cap batch size. Native mode stays small because email 1 is drafted
        # per prospect (one Claude call each); leftovers ride the next cycle.
        # Small segments also outperform blasts on deliverability.
        cap = NATIVE_BATCH_CAP if self.is_native else LEGACY_BATCH_CAP
        return {key: group[:cap] for key, group in batches.items()}
