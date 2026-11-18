"""Verify that a company named by the classifier genuinely exists online.

Llama (in ``classifier.py``) reads the email and names a company, but it cannot
browse - it only sees the one email's text, so it will just as confidently name
a hallucinated or mistyped company as a real one. This module is the SEPARATE,
subsequent reality check: it runs a real web search for the extracted name and
asks whether a genuine company web presence (an official website, or a LinkedIn
company page) actually turns up.

It gates the AUTO_ADD path in run_sync: a new company only auto-adds when it
verifies as a single, clearly-real company; anything ambiguous (several
companies share the name) or unfindable falls back to review. Crucially it
NEVER raises, and it reports a *failed* search as its own ``error`` status,
never as ``unverified`` - so a search outage (throttling, a challenge page, a
changed layout) can be told apart from a search that genuinely found no match.
run_sync treats ``error`` as "verification unavailable" and adds the company as
it did before verification existed, rather than dumping real employers into the
review queue when the search endpoint is simply refusing us.

Four outcomes
-------------
  * VERIFIED   - the search worked and exactly one result domain's brand label
                 matches the name (acme.example for "Acme", globex.example for
                 "Globex"): one clearly-real company.
  * AMBIGUOUS  - the search worked and two or more distinct matching brands
                 appear (apexcos vs apexgroup vs apexservice): different
                 companies sharing a word. Also a name too short to search.
  * UNVERIFIED - the search worked and found NO result domain matching the name
                 (only registries/news/directories), or an explicit "no
                 results" page. A genuine "we looked, nothing matched".
  * ERROR      - the search did NOT work, so we cannot tell: a request
                 exception or timeout, a non-200 response (e.g. DuckDuckGo's
                 202 throttle), a challenge/anomaly page, or any 200 whose body
                 has no parseable results and no explicit "no results" marker
                 (a layout change counts here).

# Note: brand-prefix heuristic over scraped DuckDuckGo HTML. Ceiling: a
# review site like "reviews.example" can register as a spurious extra brand and
# push a real single-company name to AMBIGUOUS (-> review, the safe direction).
# Volume control (dedupe, delay, error-latch) lives in run_sync, not here, since
# it is per-run state. Upgrade path if scraping stays throttled: swap
# _ddg_search for a keyed search/entity API (SerpAPI, Bing) behind the same
# `searcher` seam - verify_company's interpretation and the tests stay put.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field

import requests

VERIFIED = "verified"
AMBIGUOUS = "ambiguous"
UNVERIFIED = "unverified"
ERROR = "error"

_SEARCH_URL = "https://html.duckduckgo.com/html/"
_UA = "Mozilla/5.0 (compatible; jobtracker-verify/1.0)"
_TIMEOUT = 12.0
_MIN_SLUG = 3          # names shorter than this are too generic to verify
_MIN_PREFIX = 4        # brand/slug prefix overlap must be at least this long


@dataclass
class VerificationResult:
    status: str                       # VERIFIED / AMBIGUOUS / UNVERIFIED / ERROR
    brands: list[str] = field(default_factory=list)   # distinct matching site brands
    evidence: list[str] = field(default_factory=list) # result domains that matched
    linkedin: bool = False            # a linkedin.com/company page appeared
    error: str | None = None          # why the search failed (ERROR only)

    @property
    def brief(self) -> str:
        if self.status == ERROR:
            return f"error ({self.error or 'search failed'})"
        if self.status == UNVERIFIED:
            return "unverified: no company site found"
        bits = ", ".join(self.evidence[:3]) or "no site"
        if self.linkedin:
            bits += "; linkedin company page"
        return f"{self.status}: {bits}"

    def notes(self) -> str:
        """Short provenance string for companies.notes (VERIFIED adds)."""
        return f"auto-verified via web search — {self.brief}"


def verify_company(name: str | None, searcher=None) -> VerificationResult:
    """Reality-check an extracted company name against real web presence.

    ``searcher`` is an injection seam for tests: a callable ``query -> response``
    returning an object with ``.status_code`` and ``.text`` (a ``requests``
    Response satisfies it), or raising on a transport failure. Defaults to a live
    DuckDuckGo HTML search. Never raises; a failed search returns ``ERROR``.
    """
    slug = _slug(name)
    if len(slug) < _MIN_SLUG:
        return VerificationResult(AMBIGUOUS, error="name too short/generic to verify")

    search = searcher or _ddg_search
    try:
        resp = search(f"{name} company")
    except Exception as exc:                      # timeout, connection reset, etc.
        return VerificationResult(ERROR, error=f"request failed: {type(exc).__name__}")

    status = getattr(resp, "status_code", None)
    text = getattr(resp, "text", "") or ""
    if status != 200:                             # 202 throttle, 4xx, 5xx, ...
        return VerificationResult(ERROR, error=f"http {status}")

    urls = _parse_results(text)
    if not urls:
        # A working search almost always returns *some* result for a company
        # name. No parseable results means either an explicit "no results" page
        # (the search worked -> unverified) or a challenge/anomaly/changed-layout
        # page we cannot read (-> error, do not guess "unverified").
        if _explicit_no_results(text):
            return VerificationResult(UNVERIFIED)
        return VerificationResult(
            ERROR, error="no parseable results (challenge/anomaly or layout change)"
        )

    # The search worked and returned results: judge by matching brand domains.
    brands: dict[str, str] = {}
    linkedin = False
    for u in urls:
        host = urllib.parse.urlparse(u if "://" in u else "http://" + u).netloc.lower()
        host = host.removeprefix("www.")
        if not host:
            continue
        if "linkedin.com/company" in u.lower():
            linkedin = True
            continue
        label = host.split(".")[0]
        if _matches(label, slug):
            brands.setdefault(label, host)

    distinct = list(brands)
    evidence = [brands[b] for b in distinct]
    if len(distinct) == 1:
        return VerificationResult(VERIFIED, distinct, evidence, linkedin)
    if len(distinct) >= 2:
        return VerificationResult(AMBIGUOUS, distinct, evidence, linkedin)
    return VerificationResult(UNVERIFIED, [], [], linkedin)   # results, but none matched


def _slug(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _matches(label: str, slug: str) -> bool:
    """A site's brand label belongs to the searched company name."""
    label = re.sub(r"[^a-z0-9]", "", label)
    if not label:
        return False
    if label == slug:
        return True
    n = min(len(label), len(slug))
    return n >= _MIN_PREFIX and (label.startswith(slug) or slug.startswith(label))


def _parse_results(text: str) -> list[str]:
    """Extract result URLs from a DuckDuckGo HTML results page (de-wrapped)."""
    urls = []
    for href in re.findall(r'result__a"[^>]*href="([^"]+)"', text):
        m = re.search(r"uddg=([^&]+)", href)
        urls.append(urllib.parse.unquote(m.group(1)) if m else href)
    return urls


def _explicit_no_results(text: str) -> bool:
    """True if the page is DuckDuckGo's genuine 'no results' result page."""
    t = text.lower()
    return "no-results" in t or "no results." in t or "no results found" in t


def _ddg_search(query: str):
    """Live DuckDuckGo HTML search -> the raw Response (status + body).

    Deliberately does NOT raise_for_status: verify_company inspects the status
    itself so a 202 throttle becomes ERROR, not a silent empty result.
    """
    return requests.post(
        _SEARCH_URL, data={"q": query},
        headers={"User-Agent": _UA}, timeout=_TIMEOUT,
    )


def demo() -> None:
    """Offline self-check: the decision logic on canned responses (no network).

    Exercises all four outcomes, including every ERROR trigger, so a regression
    in telling a failed search from a real miss fails loudly.
    """
    @dataclass
    class Resp:
        status_code: int
        text: str

    def page(urls):
        rows = "".join(
            f'<a class="result__a" href="//duckduckgo.com/l/?uddg='
            f'{urllib.parse.quote(u, safe="")}">x</a>' for u in urls
        )
        return Resp(200, f'<div class="results">{rows}</div>')

    ok = lambda urls: (lambda q: page(urls))

    # working searches
    assert verify_company("Acme", ok(["https://www.acme.example", "https://en.wikipedia.org/wiki/Acme"])).status == VERIFIED
    r = verify_company("Globex", ok(["https://globex.example", "https://www.linkedin.com/company/globex", "https://globex.test"]))
    assert r.status == VERIFIED and r.linkedin, r
    assert verify_company("Apex", ok(["https://apexcos.com", "https://apexgroup.com", "https://apexservice.com"])).status == AMBIGUOUS
    # fake: search worked, results are only registries -> unverified
    assert verify_company("Zxqwlfoodynamics Global Ltd", ok(["https://opencorporates.com/x", "https://namescan.io/y"])).status == UNVERIFIED
    # explicit no-results page -> unverified
    assert verify_company("Zxqwlfoo Ltd", lambda q: Resp(200, '<div class="no-results">No results.</div>')).status == UNVERIFIED

    # FAILED searches must be ERROR, never unverified
    assert verify_company("Acme", lambda q: Resp(202, "<html>DuckDuckGo</html>")).status == ERROR   # throttle
    assert verify_company("Acme", lambda q: Resp(200, "")).status == ERROR                          # empty page
    assert verify_company("Acme", lambda q: Resp(200, "<html>captcha challenge</html>")).status == ERROR  # challenge, no results
    def boom(q):
        raise TimeoutError("timed out")
    assert verify_company("Acme", boom).status == ERROR                                             # timeout
    assert verify_company("X", ok(["https://x.com"])).status == AMBIGUOUS                            # too short

    print("verifier.demo: all routing assertions passed (verified/ambiguous/unverified/error)")


if __name__ == "__main__":
    demo()
