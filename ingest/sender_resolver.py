"""Decide which company an email belongs to (dry-run linking; not wired into run_sync).

Code, not Llama, decides the link: Llama's company name alone never creates a link.
Resolution runs in this order, stopping at the first step that decides:

  a. conflict  - the sender address/domain is a bootstrap link that maps to more than
                 one tracker company (shared parent / duplicate): review.
  b. linked    - the sender is already linked to a company. A 'user' link is used as
                 is; any other link whose company disagrees with Llama beyond
                 NAME_MATCH_THRESHOLD goes to review ("link disagrees with Llama").
  c. text      - a sender candidate (display name, Workday local part, domain brand)
                 agrees with Llama's company. A DOMAIN link is created only when the
                 registrable domain's own brand matches; a display/Workday match
                 creates at most an ADDRESS link (workday.com/myworkday.com only, and
                 only when the local part names the employer). May resolve with no link.
  d. tracked   - [TRACKED_MATCH_ENABLED] Llama's company agrees with exactly one
                 tracked name or alias (tracker snapshot + manual_aliases.csv): file
                 it under that company, no link. Several -> review. A name already
                 created earlier in the same run is "attached" (counts once).
     recruiter - senders in NOT_EMPLOYER_DOMAINS, or whose registrable domain holds a
                 RECRUITER_HINTS word, never reach search or the content checks.
  e. search    - the Tavily check, unchanged, with guards (not platform / free mail /
                 never-link). In replay an uncached query counts as "no result" and the
                 email moves on; a live query is never sent from replay.
  f. content   - [CONTENT_CHECKS_ENABLED] ingest.content_check: every check must pass,
                 then a NEW company is added as UNCONFIRMED (method "content"), no link.
  g. review    - otherwise, with a plain reason, a review_category and a sender_type.

Reuses matcher (normalisation, aliases, PLATFORM_NAMES, NAME_MATCH_THRESHOLD,
match_company) and sender_check. Never writes to any database.

STAGE 7 (v5). Three of the seven rule changes live here; all are behind the shared set
ingest.content_check.ENABLED_RULES, which is EMPTY by default (empty = exactly the v4 behaviour):
  prefix_attach             R5  at step f, when the ONLY failed content check is not_duplicate and the name is a
                                whole-word prefix pair with exactly one existing name, the email attaches to it
                                (method "attached", or "tracked" for a tracked target; via_prefix=1)
  linkedin_recruiter_names  R6  RECRUITER_HINTS are not applied to Llama's company name when the sender's
                                registrable domain is linkedin.com
  seed_quiet                R7  after the decision, an email that would go to review or would create a company and
                                whose Llama company key equals a SEED_DISCARDED_NAMES key gets method "quiet"
                                unless its status is in VISIBLE_STATUSES (or it resolves to a tracked company)

BUILD STAGE 2 (v6). Three more rules, same switch (ENABLED_RULES, empty by default = unchanged behaviour), plus the one shared
decision function that both the replay and scheduler/resolver_run.py call:
  webrecruit_platform       R8  WEBRECRUIT_DOMAINS are treated as PLATFORM_DOMAINS (recruiting software, not an agency)
  teamtailor_tenant         R9  a sender <label>.teamtailor-mail.com (exactly one label) counts as sender evidence when the label
                                agrees with Llama's company by the test sender_check uses; no link is written (step c)
  discard_visible_review    R10 a discarded name that would create a company with a VISIBLE status goes to review, category
                                discarded_company_replied (other statuses go quiet, R7)
  The discarded names come into resolve() as the ``discarded`` argument (a dict key -> name, or a set of keys; None = the four
  SEED_DISCARDED_NAMES, as in v5). decide_email() is the decision plus the state change it causes (created / confirmed / link).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from email.utils import parseaddr
from urllib.parse import urlparse

from ingest.classifier import VALID_STATUSES
from ingest.content_check import (
    CHECKS, DUPLICATE_SIM, duplicate_hits, is_word_prefix_pair, name_key, name_shape, name_words,
    not_duplicate, rule_on, sender_not_other, squash, strip_accents,
)
from ingest.matcher import (
    NAME_MATCH_THRESHOLD, PLATFORM_NAMES, _canonicalize, _normalize, _similarity, match_company,
)
from ingest.search_client import TOP_N, UNAVAILABLE
from ingest.sender_check import _agrees, _canon_norm, _display_candidate, check_sender, CONFIRMED

# --- stage switches (the replay passes these explicitly so stages compare on identical code)
TRACKED_MATCH_ENABLED = True
CONTENT_CHECKS_ENABLED = True
# Stage 5: a company added by the content checks (unconfirmed) is auto-CONFIRMED when a LATER email for the
# same company resolves by sender evidence (method linked, text or search) that agrees with Llama's company.
# A later email that resolves only through content, tracked or attached never confirms. Default off.
AUTO_CONFIRM_ENABLED = False

APP_STATUSES = frozenset(VALID_STATUSES) - {"unrelated"}

# --- registrable domain (built-in two-part public suffixes; no dependency) ---
# Included: UK (co/org/ac/gov/me/ltd/plc/net/sch/nhs .uk), AU (com/net/org/edu/gov .au),
# NZ (co.nz), JP (co.jp), IN (co.in), ZA (co.za), BR (com.br), SG (com.sg), HK (com.hk), and the
# CentralNic pseudo-TLDs uk.com / eu.com / us.com (so mail.acme.uk.com is "acme.uk.com", not "uk.com").
_TWO_PART_SUFFIXES = {
    "uk.com", "eu.com", "us.com",
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "ltd.uk", "plc.uk", "net.uk",
    "sch.uk", "nhs.uk", "com.au", "net.au", "org.au", "edu.au", "gov.au",
    "co.nz", "co.jp", "co.in", "co.za", "com.br", "com.sg", "com.hk",
}

# --- never-link domains (job boards, news/finance/reference, social, webmail) -
NEVER_LINK_DOMAINS = {
    "linkedin.com", "glassdoor.com", "glassdoor.co.uk", "indeed.com", "indeed.co.uk",
    "reed.co.uk", "totaljobs.com", "monster.com", "ziprecruiter.com", "jobs.ac.uk",
    "ycombinator.com", "jobright.ai", "prospects.ac.uk",
    "wikipedia.org", "yahoo.com", "bloomberg.com", "reuters.com", "ft.com",
    "crunchbase.com", "companieshouse.gov.uk",
    "facebook.com", "instagram.com", "x.com", "twitter.com", "youtube.com",
    "gmail.com", "outlook.com", "hotmail.com", "icloud.com",
}
FREE_MAIL_DOMAINS = {
    "gmail.com", "outlook.com", "yahoo.com", "hotmail.com", "icloud.com", "aol.com",
    "protonmail.com", "proton.me", "gmx.com", "live.com", "msn.com", "yandex.com", "mail.com",
}
# ATS / assessment / HR-platform & vendor domains: never linked at DOMAIN level and never
# treated as the employer (many employers share one platform domain).
PLATFORM_DOMAINS = {
    "myworkday.com", "ashbyhq.com", "smartrecruiters.com", "greenhouse.io",
    "greenhouse-mail.io", "lever.co", "icims.com", "talent.icims.com", "icims.eu",
    "workablemail.com", "teamtailor-mail.com", "successfactors.eu", "oraclecloud.com",
    "pinpoint.email", "hireful.app", "recruitee-email.com", "eploy.co.uk",
    "workday.com", "hirevue.com", "vervoe.net", "deel.com", "arcticshores.com",
    "pinpointhq.com", "successfactors.com", "workable.com", "taleo.net",
    "eightfold.ai", "avature.net", "jobvite.com", "recruitee.com", "personio.de",
    "bamboohr.com", "breezy.hr", "teamtailor.com", "join.com", "dayforce.com",
    "csod.com", "tal.net", "eploy.net", "ukg.com",
    # added 2026-10-07 (Stage 4)
    "hibob.com", "hirevue-app.eu", "cappassessments.com", "criteriacorp.com", "testgorilla.com",
}
# Matched by HOST suffix, because the registrable domain (oracle.com) may itself be an employer.
PLATFORM_HOST_SUFFIXES = ("cloud.oracle.com",)
# Workday domains share the address-level employer-in-local-part rule.
WORKDAY_DOMAINS = {"workday.com", "myworkday.com"}

# Local parts that never name an employer (no address-level link, never a candidate).
GENERIC_LOCALS = {
    "no-reply", "noreply", "donotreply", "do-not-reply", "notification",
    "notifications", "careers", "jobs", "hr",
}

# Names that are not employers (editable). Matched squashed.
NOT_EMPLOYER_NAMES = {
    "examplegradscheme", "samplecareersfair", "demorecruiting", "testtalentnetwork", "mockjobsboard",
}
# Sender domains that are recruiters / graduate services / non-employers (editable).
NOT_EMPLOYER_DOMAINS = {
    "gradjobs.example", "careersfair.example", "talentpool.example", "jobsboard.example",
}
# Substrings that mark a recruiter/agency. Applied to COMPANY NAMES and to sender
# REGISTRABLE DOMAINS - never to display names or local parts, because real employers
# use names like "Talent Team" and mailboxes like talent@acme.example.
RECRUITER_HINTS = ("recruit", "talent", "resourcing", "staffing", "headhunt", "search partners")

# --- stage 7 (v5) constants, used only when the matching rule is on (all editable) ---
# R5 prefix_attach: a single-word SHORTER name in this list never attaches by prefix ("Global" is not "Global Payments").
GENERIC_WORDS = ("global", "group", "capital", "digital", "international", "national", "partners",
                 "solutions", "services", "systems", "technologies", "technology", "energy",
                 "consulting", "consultants", "holdings", "limited", "the")
# R7 seed_quiet: names John has already judged. Stage 2 of the build will seed the discarded table from this list.
# Matched EXACTLY through discard_key() (alias applied, lower case, accents and punctuation removed, corporate
# endings removed, spaces removed). Never fuzzy.
SEED_DISCARDED_NAMES = ("Example Recruiting", "Sample Careers", "Demo Graduates", "Test Talent")
# R7: an email with one of these statuses still goes to review even for a seeded name (John wants to see invites).
VISIBLE_STATUSES = frozenset({"assessment_invite", "interview_invite", "interview_complete", "offer"})

# --- build stage 2 constants (R8 to R10; used only when the matching rule is on; all editable) ---
# R8: recruiting SOFTWARE that happens to contain the word "recruit" (not an agency).
WEBRECRUIT_DOMAINS = ("webrecruit.co.uk",)
# R9: one employer's own Teamtailor tenant is <label>.teamtailor-mail.com.
TEAMTAILOR_TENANT_DOMAIN = "teamtailor-mail.com"
# Aliases that are not in matcher.COMPANY_ALIASES (this was scratch/manual_aliases.csv): (alias, tracked company name).
MANUAL_ALIASES = (("A.C.M.E.", "Acme Widgets"), ("ACME", "Acme Widgets"))

# --- review categories -------------------------------------------------------
CAT_SHARED = "shared_parent_or_ambiguous"
CAT_DUP = "duplicate_name"
CAT_NOT_EMPLOYER = "not_employer_name"
CAT_RECRUITER = "recruiter_sender"
CAT_ADDRESS = "address_like"
CAT_SHORT = "short_name"
CAT_NAME_NOT_IN = "name_not_in_email"
CAT_NOT_ABOUT = "not_about_application"
CAT_ALERT = "job_alert"
CAT_OTHER_EMPLOYER = "sender_names_other_employer"
CAT_TEXT = "text_unavailable"
CAT_DISCARDED_REPLIED = "discarded_company_replied"      # R10
CAT_OTHER = "other"


def registrable_domain(host: str) -> str:
    host = (host or "").lower().strip().rstrip(".")
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    if ".".join(parts[-2:]) in _TWO_PART_SUFFIXES:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _platform_domains() -> set:
    """PLATFORM_DOMAINS, plus WEBRECRUIT_DOMAINS when R8 is on."""
    return PLATFORM_DOMAINS | set(WEBRECRUIT_DOMAINS) if rule_on("webrecruit_platform") else PLATFORM_DOMAINS


def _is_platform(domain: str, reg: str) -> bool:
    doms = _platform_domains()
    if domain in doms or reg in doms:
        return True
    return any(domain == s or domain.endswith("." + s) for s in PLATFORM_HOST_SUFFIXES)


def _squash(s: str) -> str:
    return squash(s)


def _key(name: str | None) -> str:
    """Identity of a would-create company: squashed, corporate suffixes removed (and, with R4, accent-free)."""
    return name_key(name)


def discard_key(name: str | None) -> str:
    """Exact-match key for the discarded list (R7): alias applied, lower case, accents and punctuation removed,
    corporate endings removed, spaces removed. Same key function as would-create names, always accent-free."""
    canon = _canonicalize(name) if name else name
    base = strip_accents(canon or "")
    return squash(_normalize(base)) or squash(base)


def is_not_employer(name: str) -> bool:
    n = squash(name)
    return any(squash(x) == n for x in NOT_EMPLOYER_NAMES)


def has_recruiter_hint(text: str) -> bool:
    t = (text or "").lower()
    s = squash(t)
    return any(h in t or squash(h) in s for h in RECRUITER_HINTS)


def is_recruiter_domain(reg: str, domain: str = "") -> bool:
    """Sender is a recruiter / grad service: a listed domain, or a RECRUITER_HINTS word in
    the registrable domain. ATS platforms (smartrecruiters.com) and job boards
    (ziprecruiter.com) are platforms, not recruiters, so they never count."""
    if _is_platform(domain or reg, reg) or reg in NEVER_LINK_DOMAINS:
        return False
    return reg in NOT_EMPLOYER_DOMAINS or has_recruiter_hint(reg)


def duplicate_of(name, tracker_names):
    """A tracker name this would-create name probably duplicates (squashed prefix or
    similarity >= DUPLICATE_SIM). For duplicate flagging only - never for sender resolution."""
    ok, dup = not_duplicate(name, tracker_names)
    return None if ok else dup


def _bad_brands() -> set:
    doms = set(_platform_domains()) | set(NEVER_LINK_DOMAINS) | set(NOT_EMPLOYER_DOMAINS)
    return {squash(d.split(".")[0]) for d in doms}


def _is_bad_name(name: str, skip_recruiter_hint: bool = False) -> bool:
    """Platform / job board / vendor / recruiter / known non-employer company name.
    ``skip_recruiter_hint`` (R6, LinkedIn senders only) leaves out the RECRUITER_HINTS test on the name."""
    return (_normalize(name) in PLATFORM_NAMES or is_not_employer(name)
            or (not skip_recruiter_hint and has_recruiter_hint(name)) or squash(name) in _bad_brands())


def _sender_reg(sender: str) -> str:
    """Registrable domain of the sender's address ('' if none)."""
    _d, addr = parseaddr(sender or "")
    return registrable_domain(addr.partition("@")[2].lower())


def sender_type(sender: str) -> str:
    _d, addr = parseaddr(sender or "")
    domain = addr.partition("@")[2].lower()
    reg = registrable_domain(domain)
    if reg == "linkedin.com":
        return "linkedin"
    if _is_platform(domain, reg) or (reg in NEVER_LINK_DOMAINS and reg not in FREE_MAIL_DOMAINS):
        return "platform_or_vendor"
    if is_recruiter_domain(reg, domain):
        return "recruiter_or_grad_service"
    return "employer_or_unknown"


def sender_candidates(sender: str) -> list[str]:
    """Employer-name candidates from the display name and the Workday local part."""
    display, address = parseaddr(sender or "")
    local, _, domain = address.partition("@")
    out = []
    cand = _display_candidate(display)
    if cand and _normalize(cand) not in PLATFORM_NAMES:
        out.append(cand)
    if domain.lower() in WORKDAY_DOMAINS and local.lower() not in GENERIC_LOCALS and local:
        out.append(local)
    return out


_TENANT_RE = re.compile(r"^([a-z0-9-]+)\." + re.escape(TEAMTAILOR_TENANT_DOMAIN) + r"$")


def tenant_label(domain: str) -> str:
    """R9: the label of a <label>.teamtailor-mail.com sender domain, or '' (exactly one label; the bare domain, two labels
    and lookalikes such as label.teamtailor-mail.com.example.com all give '')."""
    m = _TENANT_RE.match((domain or "").lower())
    return m.group(1) if m else ""


@dataclass
class Resolution:
    method: str                    # linked/text/tracked/attached/search/content/search_unavailable/review/skipped/quiet
    company: str | None = None
    company_id: int | None = None
    llama_disagrees: bool = False
    reason: str = ""
    review_reason: str = ""        # plain reason (review rows)
    review_category: str = ""
    sender_type: str = ""
    failed_checks: list = field(default_factory=list)
    passed_checks: list = field(default_factory=list)
    search_rank: int | None = None
    search_title: str | None = None
    would_create: bool = False
    would_create_confirmed: bool = False   # True: sender evidence; False: content checks only
    attached: bool = False                 # same new company as an earlier email in the run
    link_kind: str | None = None
    link_value: str | None = None
    note: str = ""
    checks: dict = field(default_factory=dict)     # the eight check results, when evaluated
    confirms: str | None = None    # key of an earlier unconfirmed would-create company THIS email confirms
    via_prefix: int = 0            # R5: 1 = attached/tracked because the name is a whole-word prefix of another
    prefix_with: str | None = None  # R5: the other name of that prefix pair (the company keeps ITS name)
    quiet_name: str | None = None  # R7: the SEED_DISCARDED_NAMES entry this email matched (method "quiet")


def _review(reason, category=CAT_OTHER, disagree=False, failed=None):
    return Resolution("review", None, None, llama_disagrees=disagree, reason=reason,
                      review_reason=reason, review_category=category, failed_checks=list(failed or []))


def _existing_or_new(llama_company, existing):
    name = _canonicalize(llama_company)
    m = match_company(name, existing)
    if m.company_id is not None and m.score >= NAME_MATCH_THRESHOLD:
        return m.company_id, m.name
    return None, name


def tracked_lookup(llama_company, existing, aliases):
    """Distinct tracked companies whose name or alias agrees with Llama's company
    (existing normalisation + NAME_MATCH_THRESHOLD; no containment). [(id, name)]."""
    nl = _canon_norm(llama_company) if llama_company else ""
    if not nl:
        return []
    hits = {}
    for cid, name in existing:
        if _agrees(nl, _canon_norm(name)):
            hits[cid] = name
    for alias, cid, cname in aliases:
        if cid is not None and _agrees(nl, _canon_norm(alias)):
            hits.setdefault(cid, cname)
    return sorted(hits.items())


def _eligible_for_tracked(name) -> bool:
    return bool(name and name.strip()) and _normalize(name) not in PLATFORM_NAMES and not is_not_employer(name)


def _name_variants(name: str):
    base = re.sub(r"\s+", " ", (name or "").strip())
    out = [base]
    stripped = re.sub(r"[\s,]+(inc\.?|llc|ltd\.?|limited|plc|corp\.?|corporation|"
                      r"co\.?|company|group|holdings)$", "", base, flags=re.I).strip()
    if stripped and stripped not in out:
        out.append(stripped)
    nopunct = re.sub(r"[^\w\s]", "", base).strip()
    if nopunct and nopunct not in out:
        out.append(nopunct)
    return out[:3]


def _search_confirm(search_client, company, sender_reg):
    """Up to 3 '<variant> official website' queries; strict confirmation. A cached-only
    (replay) client treats an uncached query as NO RESULT and moves on."""
    norm = _normalize(company)
    uncached = results_seen = 0
    for variant in _name_variants(company):
        out = search_client.search(f"{variant} official website")
        if out.status == UNAVAILABLE:
            if "not cached" in out.note:
                uncached += 1
                continue
            return ("unavailable", None, None, out.note)
        results_seen += 1
        for rank, r in enumerate(out.results[:TOP_N], 1):
            rdom = registrable_domain(urlparse(r.get("url", "")).netloc)
            if rdom == sender_reg and sender_reg not in NEVER_LINK_DOMAINS:
                blob = _normalize(f"{r.get('title','')} {r.get('content','')}")
                if norm and norm in blob:
                    return ("confirmed", rank, r.get("title", ""), "")
    if results_seen:
        return ("mismatch", None, None, "")
    return ("not_cached", None, None, "not cached (replay)")


def _labels_by_company(existing, aliases):
    d = {cid: (name, [name]) for cid, name in existing}
    for alias, cid, _cname in aliases:
        if cid in d:
            d[cid][1].append(alias)
    return d


@dataclass
class _PoolEntry:
    """One name a would-create name is compared against, plus the company it stands for (R5 needs the target)."""
    name: str                # the string that is compared: a tracked name, an alias, or a would-create name
    target: tuple            # ("t", company_id) tracked or alias, ("c", key) would-create
    cid: int | None          # tracked company id (None for a would-create company)
    canonical: str           # the company's own name (an alias stands for its canonical company)


def _dup_pool_entries(existing, aliases, created, own_name=None, own_key=None):
    """The pool of _dup_pool, with the company each name stands for. Same names, same order."""
    own = _canon_norm(own_name) if own_name else ""
    out = []
    for cid, name in existing:
        if own and _agrees(own, _canon_norm(name)):
            continue
        out.append(_PoolEntry(name, ("t", cid), cid, name))
    for alias, cid, cname in aliases:
        if own and cname and _agrees(own, _canon_norm(cname)):
            continue
        out.append(_PoolEntry(alias, ("t", cid), cid, cname))
    for key, ent in created.items():
        if key == own_key:
            continue
        out.append(_PoolEntry(ent["name"], ("c", key), None, ent["name"]))
    return out


def _dup_pool(existing, aliases, created, own_name=None, own_key=None):
    """Names a would-create name is compared against for duplicates: tracked names,
    aliases and other would-create names. ``own_name`` (calibration only) excludes the
    company the email already resolves to, so it is not flagged against itself."""
    return [e.name for e in _dup_pool_entries(existing, aliases, created, own_name, own_key)]


def evaluate_content_checks(sender, llama_company, llama_status, content, existing, aliases, created,
                            exclude_name=None):
    """All eight checks for one email -> (checks{name: bool}, shape_reason, dup_name, other_name).

    ``content`` is a content_check.TextChecks (text-derived results). ``exclude_name`` is
    used only to calibrate on emails that already resolve to a company.
    """
    c = content
    checks = {"status": llama_status in APP_STATUSES}
    checks["name_in_email"] = bool(c and c.name_in_email)
    # R6: on a LinkedIn sender RECRUITER_HINTS are not applied to Llama's company name (sender domains still are)
    skip_hint = rule_on("linkedin_recruiter_names") and _sender_reg(sender) == "linkedin.com"
    shape_ok, shape_reason = name_shape(llama_company, lambda n: _is_bad_name(n, skip_hint))
    checks["name_shape"] = shape_ok
    own_key = _key(exclude_name) if exclude_name else None
    pool = _dup_pool(existing, aliases, created, own_name=exclude_name, own_key=own_key or _key(llama_company))
    dup_ok, dup_name = not_duplicate(_canonicalize(llama_company) if llama_company else llama_company, pool)
    checks["not_duplicate"] = dup_ok
    checks["addressed_to_you"] = bool(c and c.addressed_to_you)
    checks["about_application"] = bool(c and c.about_application)
    checks["not_alert"] = bool(c and c.not_alert)
    other_ok, other_name = sender_not_other(sender_candidates(sender), _labels_by_company(existing, aliases),
                                            llama_company)
    checks["sender_not_other"] = other_ok
    return checks, shape_reason, dup_name, other_name


def _content_category(failed, shape_reason):
    """Primary category for a content failure (failed_checks still lists every failure)."""
    if "name_shape" in failed:
        return {"short_name": CAT_SHORT, "address_like": CAT_ADDRESS,
                "not_employer_name": CAT_NOT_EMPLOYER}.get(shape_reason, CAT_OTHER)
    if "not_duplicate" in failed:
        return CAT_DUP
    if "sender_not_other" in failed:
        return CAT_OTHER_EMPLOYER
    if "not_alert" in failed:
        return CAT_ALERT
    if "name_in_email" in failed:
        return CAT_NAME_NOT_IN
    if "about_application" in failed:
        return CAT_NOT_ABOUT
    return CAT_OTHER          # addressed_to_you / status


def _sender_names_other(sender, entries, target):
    """R5: the name of a company OTHER than ``target`` (tracked, alias or would-create) that a sender candidate
    (display name / Workday local part) agrees with, else None. Person names and unknown names are silent."""
    for cand in sender_candidates(sender):
        nc = _canon_norm(cand)
        if not nc:
            continue
        for e in entries:
            if e.target != target and _agrees(nc, _canon_norm(e.name)):
                return e.canonical
    return None


def _prefix_attach(llama_company, sender, existing, aliases, created):
    """R5. Called only when not_duplicate is the ONLY failed content check.

    Returns a Resolution that attaches the email to the one existing company whose name and Llama's name are a
    whole-word prefix pair (method "attached" for a would-create target, "tracked" for a tracked one; via_prefix=1;
    the company keeps the name it was created with), a review Resolution "ambiguous prefix match" when more than
    one company qualifies, or None to leave the v4 duplicate_name review as it was."""
    name = _canonicalize(llama_company) if llama_company else llama_company
    entries = _dup_pool_entries(existing, aliases, created, own_name=None, own_key=_key(llama_company))
    pool = [e.name for e in entries]
    hits = duplicate_hits(name, pool)
    pairs = [i for i, _kind in hits if is_word_prefix_pair(name, pool[i])]
    if not pairs:
        return None                                    # the clash is a similarity / character-prefix one: still review
    targets = {}
    for i in pairs:
        targets.setdefault(entries[i].target, entries[i])
    if len(targets) > 1:
        return _review("ambiguous prefix match", CAT_DUP, failed=["not_duplicate"])
    entry = next(iter(targets.values()))
    if any(entries[i].target != entry.target for i, _kind in hits):
        return None                                    # a different company also clashes (not as a prefix pair)
    if entry.target[0] == "t" and entry.cid is None:
        return None                                    # an alias with no tracked company behind it
    words_a, words_b = name_words(name), name_words(entry.name)
    shorter = words_a if len(words_a) < len(words_b) else words_b
    if len(shorter) == 1 and shorter[0] in GENERIC_WORDS:
        return None                                    # "Global" is not "Global Payments"
    if is_not_employer(name) or is_not_employer(entry.canonical):
        return None
    if _sender_names_other(sender, entries, entry.target):
        return None                                    # the sender names a different company
    if entry.target[0] == "t":
        return Resolution("tracked", entry.canonical, entry.cid, via_prefix=1, prefix_with=entry.name,
                          reason=f"whole-word prefix pair with tracked name {entry.name!r}")
    return Resolution("attached", entry.canonical, None, attached=True, via_prefix=1, prefix_with=entry.name,
                      reason=f"whole-word prefix pair with {entry.name!r}, created earlier in this run")


def discard_map(discarded=None) -> dict:
    """The discarded names as {key: name}. None = the four SEED_DISCARDED_NAMES (v5). A dict is used as it is (key -> name);
    any other collection is taken to be a set of keys (the key stands for its own name)."""
    if discarded is None:
        return {discard_key(n): n for n in SEED_DISCARDED_NAMES}
    if hasattr(discarded, "get"):
        return discarded
    return {k: k for k in discarded}


def _quiet(res, llama_company, llama_status, existing=(), aliases=(), discarded=None):
    """R7. A "quiet" Resolution if ``res`` would go to review or create a company, Llama's company key is a discarded
    key (SEED_DISCARDED_NAMES unless ``discarded`` is given), the status is not visible, and the name is not a tracked
    company (checked directly, because the resolver never files NOT_EMPLOYER_NAMES under a tracked company). Else None."""
    if not llama_company or llama_status in VISIBLE_STATUSES or res.company_id is not None:
        return None
    if not (res.method in ("review", "search_unavailable") or res.would_create):
        return None
    if tracked_lookup(llama_company, existing, aliases):
        return None                                    # it names a company in tracked_snapshot: never quiet
    key = discard_key(llama_company)
    seed = discard_map(discarded).get(key)
    if seed is None:
        return None
    q = Resolution("quiet", None, None, reason=f"seeded discarded name {seed!r}", quiet_name=seed,
                   failed_checks=list(res.failed_checks),
                   note=f"would have been {res.method}: {res.review_reason or res.reason}")
    q.sender_type = res.sender_type
    return q


def _discard_visible(res, llama_company, llama_status, discarded=None):
    """R10. A discarded name that would create a new company while its status is visible goes to review instead
    (R7 sends the other statuses quiet). Tracked companies and emails that already fail a check are left alone."""
    if not llama_company or llama_status not in VISIBLE_STATUSES or res.company_id is not None or not res.would_create:
        return None
    name = discard_map(discarded).get(discard_key(llama_company))
    if name is None:
        return None
    r = _review("discarded company replied", CAT_DISCARDED_REPLIED)
    r.sender_type = res.sender_type
    r.note = f"{name!r} is on the discarded list; it would have created a company ({res.method})"
    return r


def _resolve_core(sender, llama_company, llama_status, *, links, conflicts, existing, tracker_names=None,
            search_client=None, enable_search=True, aliases=(), created=None, content=None,
            tracked_match_enabled=None, content_checks_enabled=None) -> Resolution:
    tracked_on = TRACKED_MATCH_ENABLED if tracked_match_enabled is None else tracked_match_enabled
    content_on = CONTENT_CHECKS_ENABLED if content_checks_enabled is None else content_checks_enabled
    created = {} if created is None else created
    tracker_names = [n for _i, n in existing] if tracker_names is None else tracker_names
    stype = sender_type(sender)

    def done(res):
        res.sender_type = stype
        return res

    _display, address = parseaddr(sender or "")
    local, _, domain = address.partition("@")
    local, domain, addr = local.lower(), domain.lower(), address.lower()
    reg = registrable_domain(domain)
    linkedin_sender = rule_on("linkedin_recruiter_names") and reg == "linkedin.com"     # R6

    if llama_status == "unrelated":
        return done(Resolution("skipped", reason="unrelated"))
    if llama_status not in APP_STATUSES:
        return done(_review(f"no application status ({llama_status})"))

    # (a) conflicted bootstrap sender -> review (do not guess the company)
    for k in (addr, reg):
        if k in conflicts:
            return done(_review(conflicts[k], CAT_SHARED))

    # (b) already-linked sender
    link = links["address"].get(addr) or links["domain"].get(reg)
    if link:
        cid, name, method = link
        if method == "user":
            return done(Resolution("linked", name, cid, reason="user link"))
        if llama_company and not _agrees(_canon_norm(name), _canon_norm(llama_company)):
            return done(_review("link disagrees with Llama", CAT_OTHER_EMPLOYER, disagree=True))
        return done(Resolution("linked", name, cid, reason="sender already linked"))

    def attach(name):
        ent = created.get(_key(name))
        if ent is None:
            return None
        return Resolution("attached", ent["name"], None, attached=True,
                          reason="same new company as an earlier email in this run")

    def decide_named(method, link_kind, link_value, rank=None, title=None):
        """Sender evidence (text/search) agrees with Llama: existing company, tracked
        alias, an earlier would-create entry, or a screened new company."""
        cid, name = _existing_or_new(llama_company, existing)
        base = dict(search_rank=rank, search_title=title, link_kind=link_kind, link_value=link_value)
        if cid is not None:
            return Resolution(method, name, cid, reason=f"{method} match -> existing", **base)
        if tracked_on and _eligible_for_tracked(llama_company):
            hits = tracked_lookup(llama_company, existing, aliases)
            if len(hits) == 1:
                return Resolution("tracked", hits[0][1], hits[0][0],
                                  reason=f"{method} agrees; name matches a tracked name/alias")
            if len(hits) > 1:
                return _review("ambiguous tracked match", CAT_SHARED)
        a = attach(name)
        if a is not None:
            a.method = method
            a.link_kind, a.link_value = link_kind, link_value
            a.search_rank, a.search_title = rank, title
            return a
        if is_not_employer(name):
            return _review("not an employer", CAT_NOT_EMPLOYER)
        if has_recruiter_hint(name) and not linkedin_sender:
            return _review("likely recruiter", CAT_NOT_EMPLOYER)
        if is_recruiter_domain(reg, domain):
            return _review("recruiter or grad-service sender", CAT_RECRUITER)
        dup = duplicate_of(name, tracker_names)
        if dup:
            return _review(f"possible duplicate of {dup}", CAT_DUP)
        return Resolution(method, name, None, would_create=True, would_create_confirmed=True,
                          reason=f"{method} match -> new", **base)

    # (c) sender text comparison
    chk = check_sender(sender, llama_company)
    display_local = chk.result == CONFIRMED
    brand = reg.split(".")[0] if reg else ""
    brand_ok = (not _is_platform(domain, reg) and reg not in FREE_MAIL_DOMAINS
                and reg not in NEVER_LINK_DOMAINS and brand
                and _agrees(_canon_norm(brand), _canon_norm(llama_company)))
    tenant = tenant_label(domain) if rule_on("teamtailor_tenant") else ""                      # R9
    tenant_ok = bool(tenant) and _agrees(_canon_norm(tenant), _canon_norm(llama_company))
    if display_local or brand_ok or tenant_ok:
        if brand_ok:
            link_kind, link_value = "domain", reg
        elif domain in WORKDAY_DOMAINS and local not in GENERIC_LOCALS \
                and _agrees(_canon_norm(local), _canon_norm(llama_company)):
            link_kind, link_value = "address", addr
        else:
            link_kind, link_value = None, None
        return done(decide_named("text", link_kind, link_value))

    # (d) tracked match: Llama's company vs every tracked name/alias; no link is created
    if tracked_on and _eligible_for_tracked(llama_company):
        hits = tracked_lookup(llama_company, existing, aliases)
        if len(hits) == 1:
            return done(Resolution("tracked", hits[0][1], hits[0][0], reason="tracked name/alias match"))
        if len(hits) > 1:
            return done(_review("ambiguous tracked match", CAT_SHARED))
        a = attach(_canonicalize(llama_company))
        if a is not None:
            return done(a)

    # recruiters / graduate services never reach search or the content checks
    if is_recruiter_domain(reg, domain):
        return done(_review("recruiter or grad-service sender", CAT_RECRUITER))

    # (e) search second check (guards: not platform / free mail / never-link)
    search_note = ""
    real_name = bool(llama_company) and _normalize(llama_company) not in PLATFORM_NAMES \
        and len(_normalize(llama_company)) >= 2
    if (enable_search and real_name and not _is_platform(domain, reg)
            and reg not in FREE_MAIL_DOMAINS and reg not in NEVER_LINK_DOMAINS
            and search_client is not None):
        state, rank, title, note = _search_confirm(search_client, llama_company, reg)
        if state == "confirmed":
            return done(decide_named("search", "domain", reg, rank, title))
        if state == "unavailable":
            r = Resolution("search_unavailable", reason=f"search unavailable: {note}",
                           review_reason="search unavailable", review_category=CAT_OTHER)
            return done(r)
        search_note = ("search not cached (replay)" if state == "not_cached"
                       else "search returned results but none confirmed")

    # (f) content checks for a NEW company the sender cannot name
    if content_on:
        name = _canonicalize(llama_company) if llama_company else llama_company
        a = attach(name) if name else None
        if a is not None:
            return done(a)
        if content is None or not content.text_available:
            return done(_review("email text unavailable", CAT_TEXT, failed=["text_unavailable"]))
        checks, shape_reason, dup_name, other_name = evaluate_content_checks(
            sender, llama_company, llama_status, content, existing, aliases, created)
        failed = [c for c in CHECKS if not checks[c]]
        if not failed:
            return done(Resolution("content", name, None, would_create=True, would_create_confirmed=False,
                                   passed_checks=list(CHECKS), reason="content checks passed", checks=checks,
                                   note="auto added by content checks (unconfirmed); passed: " + ", ".join(CHECKS)))
        if failed == ["not_duplicate"] and rule_on("prefix_attach"):          # R5
            pa = _prefix_attach(llama_company, sender, existing, aliases, created)
            if pa is not None:
                pa.checks = checks
                if pa.method == "review":
                    pa.failed_checks = list(failed)
                return done(pa)
        detail = []
        for c in failed:
            if c == "name_shape":
                detail.append(f"name_shape ({shape_reason})")
            elif c == "not_duplicate":
                detail.append(f"not_duplicate (possible duplicate of {dup_name})")
            elif c == "sender_not_other":
                detail.append(f"sender_not_other (sender names {other_name})")
            else:
                detail.append(c)
        r = _review("failed content checks: " + ", ".join(detail),
                    _content_category(failed, shape_reason), failed=failed)
        r.checks = checks
        return done(r)

    # (g) review
    return done(_review(search_note or "no link and no text match", CAT_OTHER))


def resolve(sender, llama_company, llama_status, *, auto_confirm_enabled=None, discarded=None, **kwargs) -> Resolution:
    """The resolution order described at the top of this module, plus the optional auto-confirm rule.

    With AUTO_CONFIRM_ENABLED off this returns exactly what _resolve_core returns. With it on, an email
    that resolves by SENDER evidence (linked / text / search), whose company agrees with Llama's company,
    and whose company is an earlier would-create entry still unconfirmed (added by the content checks),
    sets ``confirms`` to that entry's key. The caller applies it (it owns the ``created`` state and
    records the date and method of this confirming email). Emails resolved by content, tracked or
    attached never confirm, and a disagreement between sender evidence and Llama is a review, as before.
    """
    res = _resolve_core(sender, llama_company, llama_status, **kwargs)
    if rule_on("seed_quiet"):                                                  # R7
        q = _quiet(res, llama_company, llama_status, kwargs.get("existing") or (), kwargs.get("aliases") or (), discarded)
        if q is not None:
            return q
    if rule_on("discard_visible_review"):                                      # R10
        v = _discard_visible(res, llama_company, llama_status, discarded)
        if v is not None:
            return v
    on = AUTO_CONFIRM_ENABLED if auto_confirm_enabled is None else auto_confirm_enabled
    created = kwargs.get("created") or {}
    if (on and res.method in ("linked", "text", "search") and res.company and llama_company
            and not res.would_create):
        key = _key(res.company)
        ent = created.get(key)
        if (ent is not None and not ent.get("confirmed")
                and _agrees(_canon_norm(res.company), _canon_norm(llama_company))):
            res.confirms = key
    return res


def tracked_aliases(companies):
    """(alias, company_id, company name) for matcher.COMPANY_ALIASES and MANUAL_ALIASES, for the tracked companies that
    exist. ``companies`` = [(id, name)]. The replay reads the same pairs from tracked_snapshot and manual_aliases.csv."""
    from ingest.matcher import COMPANY_ALIASES
    by_name = {n: i for i, n in companies}
    return [(a, by_name[c], c) for a, c in list(COMPANY_ALIASES.items()) + list(MANUAL_ALIASES) if c in by_name]


def decide_email(state, mid, sender, date_, company, status, content=None):
    """The decision for ONE email and the state change it causes. Both the replay (scratch/replay_v6.py) and the live path
    (scheduler/resolver_run.py) call this, so the decision logic exists once.

    ``state`` is a dict: links {"address": {}, "domain": {}} of value -> (company_id, name, method), conflicts {value: reason},
    existing [(id, name)] and aliases [(alias, id, name)] (the tracked companies), created {key: {...}} (companies the
    resolver has added), discarded (see discard_map), search (a SearchClient or None). It is updated in place:
      created   a would-create company is recorded (key = _key(name))
      confirmed an earlier unconfirmed would-create company is confirmed by this email's sender evidence
      link      a link learned from a text/search match is added to ``links``
    Returns (Resolution, effects) where effects = {"created": key, "confirmed": key, "link": (kind, value)} for what happened."""
    res = resolve(sender, company, status, links=state["links"], conflicts=state["conflicts"], existing=state["existing"],
                  aliases=state["aliases"], created=state["created"], content=content, search_client=state.get("search"),
                  enable_search=True, tracked_match_enabled=True, content_checks_enabled=True,
                  discarded=state.get("discarded"))
    fx = {}
    if res.would_create:
        key = _key(res.company)
        state["created"][key] = {"name": res.company, "confirmed": res.would_create_confirmed, "first": mid,
                                 "first_date": date_, "first_method": res.method}
        fx["created"] = key
    if res.confirms:                                              # a later sender-evidence email confirms it
        state["created"][res.confirms].update(confirmed=True, confirmed_by=mid, confirmed_date=date_,
                                              confirmed_method=res.method)
        fx["confirmed"] = res.confirms
    if res.link_kind and res.method in ("text", "search") and res.link_value not in state["links"][res.link_kind]:
        state["links"][res.link_kind][res.link_value] = (res.company_id, res.company, res.method)
        fx["link"] = (res.link_kind, res.link_value)
    return res, fx
