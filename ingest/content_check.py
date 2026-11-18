"""Content checks that let a brand NEW company be added when the email text itself
shows it is a real application (dry run only; not wired into run_sync).

Used when a sender cannot name the employer (LinkedIn, ATS platforms, assessment
vendors): there is no sender evidence, so instead we require EVERY check below to
pass. A new company is viable only if all eight hold:

  status            Llama read an application status (not "unrelated")
  name_in_email     Llama's company (corporate suffixes removed) appears in the
                    subject or body as whole words, ignoring case and punctuation
  name_shape        long enough, not address-like, and not a platform / job board /
                    assessment vendor / recruiter / known non-employer name
  not_duplicate     not a possible duplicate (prefix, or similarity >= 0.80) of any
                    tracked name, alias, or other would-create name
  addressed_to_you  the text contains one of USER_NAME_TOKENS as a whole word
  about_application the text contains an APPLICATION_PHRASES entry
  not_alert         the text contains no ALERT_PHRASES entry. With RELAXED_ALERT_ENABLED the
                    check fails only if an alert phrase is in the SUBJECT, or is in the body
                    with no STRONG_APPLICATION_PHRASES entry anywhere (so a "similar jobs"
                    footer cannot fail a clear application confirmation)
  sender_not_other  no sender candidate names a DIFFERENT tracked company

PRIVACY: email text is handled in memory only. Nothing here stores, prints or logs
a subject, body or snippet; the only things returned are booleans and the integer
position of the matched user-name token.

Every phrase/word list below is a plain named constant so it can be edited.

STAGE 7 (v5) RULE SWITCH. Seven rule changes sit behind ONE shared set, ENABLED_RULES, which is
EMPTY by default. With it empty every function in this file and in ingest/sender_resolver.py returns
exactly what it returned in v4. A script turns rules on with set_enabled_rules([...]); rule_on(name)
is the only test the code uses. Rules in stage order (RULE_NAMES):
  ignore_consulting          R1  similarity test ignores a trailing "consulting"            (not_duplicate)
  address_last_word          R2  ADDRESS_HINTS apply to the last word of the name only       (name_shape)
  short_names                R3  3-letter names allowed; short names use whole-word prefixes (name_shape, not_duplicate)
  name_normalise             R4  accent/space-tolerant name_in_email; accent-free name keys   (text_checks, squash)
  prefix_attach              R5  whole-word prefix pairs attach instead of review            (ingest/sender_resolver.py)
  linkedin_recruiter_names   R6  RECRUITER_HINTS skip Llama's name when the sender is LinkedIn (ingest/sender_resolver.py)
  seed_quiet                 R7  seeded discarded names go quiet unless the status is visible (ingest/sender_resolver.py)

BUILD STAGE 2 adds three more rules to the same set (RULE_NAMES order, after seed_quiet):
  webrecruit_platform        R8  webrecruit.co.uk is treated as a platform domain, not a recruiter agency (ingest/sender_resolver.py)
  teamtailor_tenant          R9  <label>.teamtailor-mail.com counts as sender evidence when the label agrees with Llama's company
  discard_visible_review     R10 a discarded name that would create a company with a visible status goes to review instead
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from ingest.matcher import _normalize, _similarity  # import only; matcher.py is not modified
from ingest.sender_check import _agrees, _canon_norm

# --- rule switch (stage 7 / v5) ------------------------------------------------
RULE_NAMES = ("ignore_consulting", "address_last_word", "short_names", "name_normalise",
              "prefix_attach", "linkedin_recruiter_names", "seed_quiet",
              "webrecruit_platform", "teamtailor_tenant", "discard_visible_review")
ENABLED_RULES: set[str] = set()      # shared by content_check and sender_resolver; EMPTY = v4 behaviour


def rule_on(name: str) -> bool:
    """True if rule ``name`` is enabled. An unknown name raises, so a typo can never silently mean 'off'."""
    if name not in RULE_NAMES:
        raise ValueError(f"unknown rule {name!r}; valid rules: {RULE_NAMES}")
    return name in ENABLED_RULES


def set_enabled_rules(names) -> None:
    """Replace the enabled rules IN PLACE (the set object is shared with sender_resolver, so never rebind it)."""
    names = set(names)
    unknown = names - set(RULE_NAMES)
    if unknown:
        raise ValueError(f"unknown rule(s) {sorted(unknown)}; valid rules: {RULE_NAMES}")
    ENABLED_RULES.clear()
    ENABLED_RULES.update(names)


# --- editable constants -------------------------------------------------------
MIN_NAME_LENGTH = 4          # shortest acceptable company name (letters/digits only)
DUPLICATE_SIM = 0.80         # squashed-name similarity that flags a probable duplicate
ADDRESS_HINTS = ("house", "street", "road", "lane", "court", "building", "tower",
                 "plaza", "square", "avenue", "close", "drive")
UK_POSTCODE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b", re.IGNORECASE)
USER_NAME_TOKENS = ("YOUR_FIRST_NAME", "YOUR_LAST_NAME")   # the names emails use for you
APPLICATION_PHRASES = (
    "your application", "you applied", "thank you for applying", "applying for",
    "we received your application", "your interview", "your assessment",
    "we regret to inform you",
)
ALERT_PHRASES = (
    "jobs you may be interested in", "recommended jobs", "similar jobs",
    "job alert", "new jobs for you",
)
# not_alert, relaxed (Stage 5). False = the v3 rule: ANY alert phrase in subject+body fails.
# True  = an alert phrase in the subject fails; an alert phrase only in the body fails unless a
#         STRONG_APPLICATION_PHRASES entry appears in the subject or body.
# General phrases such as "applying for" are deliberately NOT strong.
RELAXED_ALERT_ENABLED = False
STRONG_APPLICATION_PHRASES = (
    "your application was sent", "application was sent to", "you applied to", "you applied for",
    "thank you for applying", "thanks for applying", "we received your application",
    "we have received your application", "your application has been received",
    "your application to", "your application for",
)

# --- stage 7 (v5) constants, used only when the matching rule is on ---------------
TRAILING_GENERIC_WORDS = ("consulting",)   # R1: dropped from the END of both names in the 0.80 similarity test only
MIN_NAME_LENGTH_SHORT_NAMES = 3            # R3: replaces MIN_NAME_LENGTH (4); names of 2 characters or fewer still fail
SHORT_NAME_LIMIT = 5                       # R3: names with fewer normalised characters use whole-word prefix tests
IGNORED_LEADING_NAME_WORDS = ("the",)      # R4: one leading word of Llama's name ignored by name_in_email
IGNORED_TRAILING_NAME_WORDS = ("uk",)      # R4: one trailing word of Llama's name ignored by name_in_email

CHECKS = ("status", "name_in_email", "name_shape", "not_duplicate",
          "addressed_to_you", "about_application", "not_alert", "sender_not_other")
TEXT_CHECKS = ("name_in_email", "addressed_to_you", "about_application", "not_alert")

_TOKEN_RE = re.compile(r"\b(?:" + "|".join(map(re.escape, USER_NAME_TOKENS)) + r")\b", re.IGNORECASE)


def strip_accents(s: str | None) -> str:
    """NFKD, then drop the combining marks: 'Crème Ltd' -> 'Creme Ltd'."""
    return "".join(ch for ch in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(ch))


def squash(s: str | None) -> str:
    s = s or ""
    if rule_on("name_normalise"):                      # R4(b): accent-free keys
        s = strip_accents(s)
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _words(text: str | None) -> str:
    """Lower-case, punctuation -> single spaces, padded so ' a b ' matches whole words."""
    return " " + re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip() + " "


def name_key(name: str | None) -> str:
    """Identity of a would-create company (and of a discard key): corporate endings removed, spaces
    removed, and with R4 also accent-free."""
    base = strip_accents(name) if rule_on("name_normalise") else name
    return squash(_normalize(base)) or squash(name)


def name_words(name: str | None) -> list[str]:
    """The words of a name for whole-word tests: lower case, brackets, punctuation and corporate endings
    removed (accents folded when R4 is on)."""
    base = strip_accents(name) if rule_on("name_normalise") else (name or "")
    return _normalize(base).split()


def is_word_prefix_pair(a: str | None, b: str | None) -> bool:
    """True if one name's words are exactly the first words of the other, and the other is longer."""
    wa, wb = name_words(a), name_words(b)
    if not wa or not wb or len(wa) == len(wb):
        return False
    short, long_ = (wa, wb) if len(wa) < len(wb) else (wb, wa)
    return long_[:len(short)] == short


def name_words_for_text(company: str | None) -> list[str]:
    """R4(a): the words of Llama's name that must appear in the text. Accents folded, brackets, punctuation
    and corporate endings removed, one leading 'The' and one trailing 'UK' ignored."""
    toks = _normalize(strip_accents(company or "")).split()
    if toks and toks[0] in IGNORED_LEADING_NAME_WORDS:
        toks = toks[1:]
    if toks and toks[-1] in IGNORED_TRAILING_NAME_WORDS:
        toks = toks[:-1]
    return toks


def name_in_text_normalised(text: str | None, company: str | None) -> bool:
    """R4(a). Whole-word match of Llama's name in the text, ignoring accents, allowing optional whitespace between
    the name's words, ignoring a leading 'The' and a trailing 'UK' in the name. No fuzzy matching. A superset of the
    v4 test, so an email that already passed it still passes."""
    toks = name_words_for_text(company)
    if not toks:
        return False
    folded = " " + re.sub(r"[^a-z0-9]+", " ", strip_accents(text or "").lower()).strip() + " "
    pattern = r"(?<![a-z0-9])" + r"\s*".join(re.escape(t) for t in toks) + r"(?![a-z0-9])"
    return re.search(pattern, folded) is not None


@dataclass
class TextChecks:
    """Result of the four checks that need the email text (all in-memory derived)."""
    text_available: bool
    name_in_email: bool | None = None
    addressed_to_you: bool | None = None
    about_application: bool | None = None
    not_alert: bool | None = None
    token_position: int | None = None   # char offset of the matched user-name token only
    alert_in_subject: bool | None = None          # an ALERT_PHRASES entry is in the subject
    alert_in_body: bool | None = None             # an ALERT_PHRASES entry is in the body
    strong_application_phrase: bool | None = None  # a STRONG_APPLICATION_PHRASES entry is in subject or body


def compute_not_alert(alert_in_subject, alert_in_body, strong_application_phrase) -> bool:
    """The not_alert result from the three stored booleans, under the CURRENT mode
    (RELAXED_ALERT_ENABLED is read at call time so a replay can compare both modes)."""
    if not RELAXED_ALERT_ENABLED:
        return not (alert_in_subject or alert_in_body)
    if alert_in_subject:
        return False
    if alert_in_body:
        return bool(strong_application_phrase)
    return True


def alert_flags(subject: str | None, body: str | None) -> tuple[bool, bool, bool]:
    """(alert_in_subject, alert_in_body, strong_application_phrase) - booleans only; the text is
    used in memory and discarded."""
    sw, bw = _words(subject), _words(body)
    a_sub = any(_words(p) in sw for p in ALERT_PHRASES)
    a_body = any(_words(p) in bw for p in ALERT_PHRASES)
    strong = any(_words(p) in sw or _words(p) in bw for p in STRONG_APPLICATION_PHRASES)
    return a_sub, a_body, strong


def text_checks(subject: str | None, body: str | None, company: str | None) -> TextChecks:
    """Run the text-dependent checks. The text is used and discarded; never stored."""
    text = f"{subject or ''}\n{body or ''}"
    if not text.strip():
        return TextChecks(False)
    words = _words(text)
    phrase = _normalize(company)                       # suffixes/parentheticals removed
    name_in = bool(phrase) and f" {phrase} " in words
    if not name_in and rule_on("name_normalise"):      # R4(a): never turns a pass into a fail
        name_in = name_in_text_normalised(text, company)
    m = _TOKEN_RE.search(text)
    a_sub, a_body, strong = alert_flags(subject, body)
    if RELAXED_ALERT_ENABLED:
        not_alert = compute_not_alert(a_sub, a_body, strong)
    else:                                              # v3: any alert phrase in subject+body as one text
        not_alert = not any(_words(p) in words for p in ALERT_PHRASES)
    return TextChecks(
        True,
        name_in_email=name_in,
        addressed_to_you=m is not None,
        about_application=any(_words(p) in words for p in APPLICATION_PHRASES),
        not_alert=not_alert,
        token_position=m.start() if m else None,
        alert_in_subject=a_sub,
        alert_in_body=a_body,
        strong_application_phrase=strong,
    )


def _address_like(company: str, words: str) -> bool:
    if rule_on("address_last_word"):                   # R2: only the LAST word of the name can be an address word
        last = name_words(company)[-1:]
        return bool(last) and last[0] in ADDRESS_HINTS
    return any(f" {h} " in words for h in ADDRESS_HINTS)


def name_shape(company: str | None, is_bad_name) -> tuple[bool, str]:
    """(ok, reason). reason is '' | 'no_name' | 'short_name' | 'address_like' | 'not_employer_name'.

    ``is_bad_name`` is supplied by the resolver (platform, job board, vendor,
    recruiter hint, known non-employer) so these lists live in one place.
    """
    if not company or not company.strip():
        return False, "no_name"
    sq = squash(company)                    # the name as given (letters/digits only): "Acme Co" = 6, "AB" = 2
    min_len = MIN_NAME_LENGTH_SHORT_NAMES if rule_on("short_names") else MIN_NAME_LENGTH   # R3
    if len(sq) < min_len:
        return False, "short_name"
    words = _words(company)
    if UK_POSTCODE.search(company) or _address_like(company, words):
        return False, "address_like"
    if is_bad_name(company):
        return False, "not_employer_name"
    return True, ""


def _without_trailing_generic(name: str | None) -> str:
    """R1: the squashed name with a trailing word from TRAILING_GENERIC_WORDS removed (at least one word is kept)."""
    base = strip_accents(name) if rule_on("name_normalise") else (name or "")
    toks = re.findall(r"[a-z0-9]+", base.lower())
    while len(toks) > 1 and toks[-1] in TRAILING_GENERIC_WORDS:
        toks.pop()
    return "".join(toks)


def _clash_kind(company: str | None, other: str | None, n: str, t: str) -> str | None:
    """Why two names clash: 'equal' | 'prefix' | 'similar' | None. ``n`` and ``t`` are their squashed forms."""
    if n == t:
        return "equal"
    if rule_on("short_names") and min(len(name_key(company)), len(name_key(other))) < SHORT_NAME_LIMIT:
        prefix = is_word_prefix_pair(company, other)           # R3: a short name only prefixes on whole words
    else:
        prefix = n.startswith(t) or t.startswith(n)
    if prefix:
        return "prefix"
    if rule_on("ignore_consulting"):                           # R1: compare without a trailing "consulting"
        sim = _similarity(_without_trailing_generic(company), _without_trailing_generic(other))
    else:
        sim = _similarity(n, t)
    return "similar" if sim >= DUPLICATE_SIM else None


def duplicate_hits(company: str | None, pool) -> list[tuple[int, str]]:
    """EVERY pool name that clashes with ``company``: [(index into pool, kind)]. The first entry is
    what not_duplicate reports."""
    n = squash(company)
    if not n:
        return []
    hits = []
    for i, p in enumerate(pool):
        t = squash(p)
        if not t:
            continue
        kind = _clash_kind(company, p, n, t)
        if kind:
            hits.append((i, kind))
    return hits


def not_duplicate(company: str | None, pool) -> tuple[bool, str | None]:
    """(ok, matching_name). ``pool`` = names to compare against (tracked names,
    aliases, other would-create names). Duplicate if one squashed name is a prefix of
    the other, or similarity >= DUPLICATE_SIM. For duplicate flagging only."""
    n = squash(company)
    if not n:
        return True, None
    for p in pool:
        t = squash(p)
        if not t:
            continue
        if _clash_kind(company, p, n, t):
            return False, p
    return True, None


def sender_not_other(candidates, labels_by_company, llama_company) -> tuple[bool, str | None]:
    """Fail only if a sender candidate names a tracked company that Llama's company
    is NOT. Person names and unknown names are silent; domain brands are never used.

    ``labels_by_company``: {company_id: (company_name, [name + alias labels])}.
    """
    nl = _canon_norm(llama_company) if llama_company else ""
    for cand in candidates:
        nc = _canon_norm(cand)
        if not nc:
            continue
        for _cid, (cname, labels) in labels_by_company.items():
            if any(_agrees(nc, _canon_norm(l)) for l in labels):          # candidate = this tracked company
                if not (nl and any(_agrees(nl, _canon_norm(l)) for l in labels)):  # ...but Llama says another
                    return False, cname
    return True, None
