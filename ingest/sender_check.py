"""Confirm a brand-new company by matching Llama's extracted name to the sender.

The sender of an application email nearly always names the employer - in the
display name ("Acme", "Globex Industries Hiring Team", "Umbrella Group
Workday (Do Not Reply)"), or, for myworkday.com only, in the local part before
the @ (acme@myworkday.com, umbrella@myworkday.com). Llama can misread or hallucinate
a company name, but it cannot invent a matching sender. So when the sync is
about to AUTO_ADD a new company, we compare the extracted name against the
employer name(s) in the sender: agreement is strong enough to add the company
as confirmed; disagreement, or a sender that names no employer, goes to review.

Comparison reuses matcher.py's canonicalisation (aliases) and normalisation
(corporate suffixes), so "Acmeco" folds onto "Acme Fashion Co." and "Foo Ltd" onto
"Foo". Names agree ONLY if equal once normalised (spaces removed) or difflib-
similar past NAME_MATCH_THRESHOLD - no containment matching, no initialism
guessing (so "abc" does not match "Alpha Beta Corporation").

This module only reads and compares strings; it never touches the database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from email.utils import parseaddr

from ingest.matcher import (  # import only; matcher.py is not modified
    NAME_MATCH_THRESHOLD,
    PLATFORM_NAMES,
    _canonicalize,
    _normalize,
    _similarity,
)

CONFIRMED = "confirmed"
MISMATCH = "mismatch"
NO_EMPLOYER = "no_employer_in_sender"

# Only myworkday.com puts the employer in the local part (acme@myworkday.com);
# elsewhere the local part is generic (no-reply, notification, ...).
LOCALPART_DOMAINS = {"myworkday.com"}

# Generic words/phrases in a sender DISPLAY name that are not the employer.
# Same plain-constant style as matcher.PLATFORM_NAMES. Matched case- and
# space-insensitively, longest first, so "Workday at" is stripped before
# "Workday", and "Do Not Reply" also catches the run-together "DoNotReply".
# "anything after an @" (iCIMS style, "Company @ icims") is handled separately.
# Note: "System" is stripped as a whole substring per spec (for the Workday
# "WorkdaySystemDoNotReply" form); a real name like "Foo Systems" therefore
# loses "System" and routes to review, not a wrong confirm - the safe direction.
GENERIC_DISPLAY_PHRASES = (
    "Talent Acquisition Team",
    "Recruitment Team",
    "Hiring Team",
    "Talent Team",
    "Workday at",
    "Do Not Reply",
    "No Reply",
    "Notifications",
    "Careers",
    "Workday",
    "System",
)

# Local parts (before the @) that never name an employer.
GENERIC_LOCAL_PARTS = {
    "no-reply", "noreply", "notification", "notifications", "careers", "jobs", "hr",
}


@dataclass
class SenderCheck:
    result: str                  # CONFIRMED / MISMATCH / NO_EMPLOYER
    matched: str | None = None   # the sender candidate that agreed (CONFIRMED)
    candidates: list[str] = field(default_factory=list)
    address: str = ""            # email address parsed from the sender

    @property
    def primary(self) -> str | None:
        """The employer name the sender appears to give (for the mismatch reason)."""
        return self.candidates[0] if self.candidates else None


def check_sender(sender: str, extracted_company: str | None) -> SenderCheck:
    """Compare ``extracted_company`` against the employer name(s) in ``sender``."""
    display, address = parseaddr(sender or "")
    candidates = _candidates(display, address)
    if not candidates:
        return SenderCheck(NO_EMPLOYER, candidates=[], address=address)

    target = _canon_norm(extracted_company)
    for cand in candidates:
        if target and _agrees(_canon_norm(cand), target):
            return SenderCheck(CONFIRMED, matched=cand, candidates=candidates, address=address)
    return SenderCheck(MISMATCH, candidates=candidates, address=address)


def _candidates(display: str, address: str) -> list[str]:
    """Employer-name candidates from the display name and the myworkday local part."""
    out: list[str] = []
    disp = _display_candidate(display)
    if disp and not _is_platform(disp):
        out.append(disp)
    local = _localpart_candidate(address)
    if local and not _is_platform(local):
        out.append(local)
    return out


def _display_candidate(display: str) -> str:
    if not display:
        return ""
    name = display.split("@")[0]              # iCIMS: drop "... @ icims"
    name = re.sub(r"\(.*?\)", " ", name)       # drop "(Do Not Reply)" etc.
    for phrase in sorted(GENERIC_DISPLAY_PHRASES, key=len, reverse=True):
        pattern = r"\s*".join(re.escape(w) for w in phrase.split())
        name = re.sub(pattern, " ", name, flags=re.IGNORECASE)
    return name.strip(" ,-").strip()


def _localpart_candidate(address: str) -> str:
    local, _, domain = address.partition("@")
    if not domain or domain.lower() not in LOCALPART_DOMAINS:
        return ""
    if local.lower() in GENERIC_LOCAL_PARTS:
        return ""
    return local


def _canon_norm(name: str | None) -> str:
    """Alias-resolve then normalise (matcher helpers) for comparison."""
    return _normalize(_canonicalize(name)) if name else ""


def _agrees(a: str, b: str) -> bool:
    """Agree if equal ignoring spaces, or difflib-similar past the threshold."""
    if not a or not b:
        return False
    if a.replace(" ", "") == b.replace(" ", ""):
        return True
    return _similarity(a, b) >= NAME_MATCH_THRESHOLD


def _is_platform(name: str) -> bool:
    return _normalize(name) in PLATFORM_NAMES


def demo() -> None:
    """Offline self-check on the real sender strings (no network, no DB)."""
    def r(sender, extracted):
        return check_sender(sender, extracted)

    assert r("Acme <notification@smartrecruiters.com>", "Acme").result == CONFIRMED
    c = r("acme@myworkday.com", "Acme")
    assert c.result == CONFIRMED and c.matched == "acme", c
    assert r("Globex Industries Hiring Team <notifications@smartrecruiters.com>",
             "Globex Industries").result == CONFIRMED
    assert r('"Initech Partners @ icims" <initech+autoreply@talent.icims.com>',
             "Initech Partners").result == CONFIRMED
    assert r('"Umbrella Group Workday (Do Not Reply)" <umbrellagroup@myworkday.com>',
             "Umbrella Group").result == CONFIRMED
    # alias table: sender says Acmeco, classifier says Acme Fashion Co.
    assert r("Acmeco <talent@acmeco.example>", "Acme Fashion Co.").result == CONFIRMED
    # mismatch: myworkday tenant 'zxq' vs an unrelated extracted name
    assert r("zxq@myworkday.com", "Quorvale Hall").result == MISMATCH
    # platform name catch stays a mismatch (Acme sender, Llama read 'Ashby')
    assert r("Acme Talent Acquisition Team <no-reply@ashbyhq.com>", "Ashby").result == MISMATCH
    # generic sender -> no employer name
    assert r("Notifications <notifications@smartrecruiters.com>", "Whoever").result == NO_EMPLOYER
    assert r("WorkdaySystemDoNotReply <umbrella@myworkday.com>", "Umbrella").result == CONFIRMED
    print("sender_check.demo: all assertions passed")


if __name__ == "__main__":
    demo()
