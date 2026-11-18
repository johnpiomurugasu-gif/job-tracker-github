"""Match an extracted company name to the companies table and gate auto-writes.

This module turns a ``ClassificationResult`` (from ``classifier.py``) into a
``MatchDecision`` with one of four outcomes, which the sync orchestrator
persists:

  * AUTO_WRITE - high confidence AND a clean match to an EXISTING company:
    append a confirmed event to that company's application (the trusted path).
  * AUTO_ADD   - high confidence AND a clean company extraction that is NOT in
    the table yet: create the company + application, flagged unconfirmed, so a
    correctly-identified new company appears automatically instead of waiting
    on manual approval.
  * REVIEW     - genuine classifier uncertainty ONLY: parse failure, low
    confidence, an ambiguous match between two similar existing companies, or
    no company could be extracted at all.
  * SKIP       - the classifier marked the email "unrelated" (inbox noise).

WHY CONFIDENCE ALONE IS NOT THE GATE
------------------------------------
Live testing of the local llama3.1:8b classifier returned ``confidence: 1.0``
on every sample, so the self-reported score cannot, on its own, decide what is
trustworthy. Confidence is therefore necessary but not sufficient: it gates
whether we act at all, while the company match decides HOW we act - write to a
known company (AUTO_WRITE), create a new one (AUTO_ADD), or, only for genuine
uncertainty, defer to a human (REVIEW). Reserving REVIEW for real failure is
deliberate: a correctly-identified new company should not gate on manual
approval, only be visibly marked unconfirmed until reviewed.

The thresholds are named module constants so they can be tuned once real
output is seen, without re-deriving the reasoning above.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from ingest.classifier import ClassificationResult

# --- Tunable gate thresholds (see module docstring for the reasoning) ---
CONFIDENCE_THRESHOLD = 0.75  # brief's starting point; necessary, not sufficient
NAME_MATCH_THRESHOLD = 0.90  # difflib ratio counted as an exact/near-exact match
AMBIGUITY_MARGIN = 0.05      # if the 2nd-best match is within this of the best
                             # (and also clears NAME_MATCH_THRESHOLD), the match
                             # is treated as ambiguous -> review, not auto-write

# --- Decision outcomes ---
AUTO_WRITE = "auto_write"  # high confidence + clean match to an EXISTING company
AUTO_ADD = "auto_add"      # high confidence + clean NEW company -> create it, unconfirmed
REVIEW = "review"          # genuine uncertainty only (parse fail / low conf / ambiguous / no company)
SKIP = "skip"              # classifier marked the email "unrelated"

# Corporate suffixes stripped during normalisation so "Foo" matches "Foo Ltd".
_CORP_SUFFIXES = {
    "inc", "incorporated", "llc", "ltd", "limited", "plc", "corp",
    "corporation", "co", "company", "group", "holdings",
}

# Platform / ATS names that are NOT employers. If the classifier extracts one of
# these as the company, the real employer is unidentifiable, so route to REVIEW
# rather than auto-adding a bogus company. Compared after normalisation.
# (User-maintained.)
PLATFORM_NAMES = {
    "myworkday", "workday", "greenhouse", "lever", "icims",
    "smartrecruiters", "ashby", "ashbyhq", "hirevue", "workable",
}

# Alternate extracted names -> the canonical company they belong to, so variant
# spellings merge into one company and known aliases fold onto a seeded company
# instead of creating a near-duplicate. Keys are matched after normalisation.
# (User-maintained.)
COMPANY_ALIASES = {
    "acmeco": "Acme Fashion Co.",
    "globex consulting": "Globex",
    "initech": "Initech Partners",
}


@dataclass
class CompanyMatch:
    company_id: int | None
    name: str | None
    score: float
    is_clean_match: bool          # clears the threshold AND is unambiguous
    is_ambiguous: bool
    runner_up_name: str | None = None
    runner_up_score: float = 0.0


@dataclass
class MatchDecision:
    outcome: str            # AUTO_WRITE / AUTO_ADD / REVIEW / SKIP
    confirmed: int | None   # 1 (auto-write/add), 0 (review), None (skip: no event row)
    auto_write: bool
    company_id: int | None
    company_name: str | None
    match_score: float
    reason: str


def match_and_decide(
    classification: ClassificationResult,
    companies: list[tuple[int, str]],
) -> MatchDecision:
    """Match the classified company against ``companies`` and apply the gate.

    ``companies`` is a list of ``(id, name)`` rows from the companies table
    (see ``load_companies``). The extracted name is first canonicalised through
    COMPANY_ALIASES so variant spellings and known aliases resolve to one
    company. Returns a ``MatchDecision``; never writes to the database and never
    raises on classifier output.
    """
    canonical = _canonicalize(classification.company)
    company_match = match_company(canonical, companies)
    return decide(classification, company_match, canonical)


def decide(
    classification: ClassificationResult,
    company_match: CompanyMatch,
    canonical_company: str | None = None,
) -> MatchDecision:
    """Route a classification to one of four outcomes (see module docstring).

    Order matters: genuine-uncertainty checks come first, so REVIEW is reserved
    for real classifier failure; a confident, cleanly-identified company then
    either matches an existing row (AUTO_WRITE) or is auto-added (AUTO_ADD).
    ``canonical_company`` is the alias-resolved name (defaults to the raw one).
    """
    if canonical_company is None:
        canonical_company = classification.company

    # 1. Parse failure -> genuine uncertainty.
    if classification.needs_review:
        return _decision(
            REVIEW, company_match,
            f"classifier flagged for review: {classification.error or 'parse failure'}",
        )

    # 2. Not a status update about a specific application -> filter it out.
    if classification.status == "unrelated":
        return _decision(SKIP, company_match, "classifier marked email 'unrelated'")

    # 3. Low confidence -> genuine uncertainty (we don't act on a shaky read).
    if classification.confidence < CONFIDENCE_THRESHOLD:
        return _decision(
            REVIEW, company_match,
            f"low confidence {classification.confidence:.2f} < {CONFIDENCE_THRESHOLD}",
        )

    # --- Confidence is sufficient from here; the company match decides how. ---

    # 4. Clean match to an EXISTING company -> trusted auto-write.
    if company_match.is_clean_match:
        return _decision(
            AUTO_WRITE, company_match,
            f"confidence {classification.confidence:.2f} >= {CONFIDENCE_THRESHOLD} "
            f"and company '{company_match.name}' matched "
            f"(score {company_match.score:.2f})",
        )

    # 5. Ambiguous between two similar EXISTING companies -> genuine uncertainty.
    if company_match.is_ambiguous:
        return _decision(
            REVIEW, company_match,
            f"ambiguous match: '{company_match.name}' ({company_match.score:.2f}) "
            f"vs '{company_match.runner_up_name}' ({company_match.runner_up_score:.2f})",
        )

    # 6. No real company to identify -> can't act. Either nothing was extracted,
    #    or the model returned a platform/ATS name that is not an employer.
    if not _normalize(canonical_company):
        return _decision(
            REVIEW, company_match, "no company could be extracted from the email"
        )
    if _is_platform(canonical_company):
        return _decision(
            REVIEW, company_match,
            f"extracted a platform/ATS name {canonical_company!r}, not an employer",
        )

    # 7. Confident, cleanly-identified company that is NOT in the table yet ->
    #    auto-add it (unconfirmed) rather than gating on manual approval.
    return _decision(
        AUTO_ADD, company_match,
        f"new company {canonical_company!r} not in the table "
        f"(closest existing '{company_match.name}' {company_match.score:.2f}); "
        f"auto-adding, unconfirmed",
        new_company_name=canonical_company,
    )


def match_company(
    name: str | None,
    companies: list[tuple[int, str]],
) -> CompanyMatch:
    """Best exact/near-exact match of ``name`` against the companies table.

    Uses stdlib ``difflib`` similarity on normalised names (no external fuzzy
    dependency). A match is "clean" only if it clears ``NAME_MATCH_THRESHOLD``
    and no runner-up is within ``AMBIGUITY_MARGIN`` of it.
    """
    query = _normalize(name)
    if not query or not companies:
        return CompanyMatch(None, None, 0.0, is_clean_match=False, is_ambiguous=False)

    scored = sorted(
        ((_similarity(query, _normalize(cname)), cid, cname) for cid, cname in companies),
        key=lambda t: t[0],
        reverse=True,
    )
    best_score, best_id, best_name = scored[0]
    runner_score, _, runner_name = scored[1] if len(scored) > 1 else (0.0, None, None)

    clears = best_score >= NAME_MATCH_THRESHOLD
    ambiguous = (
        clears
        and runner_score >= NAME_MATCH_THRESHOLD
        and (best_score - runner_score) < AMBIGUITY_MARGIN
    )
    return CompanyMatch(
        company_id=best_id,
        name=best_name,
        score=best_score,
        is_clean_match=clears and not ambiguous,
        is_ambiguous=ambiguous,
        runner_up_name=runner_name,
        runner_up_score=runner_score,
    )


def load_companies(conn) -> list[tuple[int, str]]:
    """Read ``(id, name)`` rows from the companies table via a sqlite3 conn.

    Convenience for the sync orchestrator; unused by the matching logic itself,
    which takes the list directly so it can be tested without a database.
    """
    return [(row[0], row[1]) for row in conn.execute("SELECT id, name FROM companies")]


def _decision(
    outcome: str,
    company_match: CompanyMatch,
    reason: str,
    new_company_name: str | None = None,
) -> MatchDecision:
    confirmed = {AUTO_WRITE: 1, AUTO_ADD: 1, REVIEW: 0, SKIP: None}[outcome]
    # A matched company id is only meaningful on a clean match to an existing
    # row; AUTO_ADD reports the new name to create instead.
    company_id = company_match.company_id if outcome == AUTO_WRITE else None
    company_name = new_company_name if outcome == AUTO_ADD else company_match.name
    return MatchDecision(
        outcome=outcome,
        confirmed=confirmed,
        auto_write=(outcome == AUTO_WRITE),
        company_id=company_id,
        company_name=company_name,
        match_score=company_match.score,
        reason=reason,
    )


def _canonicalize(company_name: str | None) -> str | None:
    """Map an extracted name to its canonical form via COMPANY_ALIASES."""
    if not company_name:
        return company_name
    return COMPANY_ALIASES.get(_normalize(company_name), company_name)


def _is_platform(company_name: str | None) -> bool:
    """True if the name is a known ATS/platform, not a real employer."""
    return _normalize(company_name) in PLATFORM_NAMES


def _normalize(name: str | None) -> str:
    """Lowercase, drop parentheticals/punctuation and corporate suffixes."""
    if not name:
        return ""
    text = name.lower()
    text = re.sub(r"\(.*?\)", " ", text)      # e.g. "Acme Widgets (UK)" -> "acme widgets"
    text = re.sub(r"[^a-z0-9\s]", " ", text)  # e.g. "Foo.Bar" -> "foo bar"
    tokens = [t for t in text.split() if t and t not in _CORP_SUFFIXES]
    return " ".join(tokens)


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()
