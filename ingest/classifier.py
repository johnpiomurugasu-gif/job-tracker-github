"""LLM-based email classifier backed by a local Ollama model.

One email in, one structured classification out. The model is asked to read a
job-application email the way a person would and return strict JSON:
``{company, status, stage, confidence}``. ``status`` is constrained to the
closed set from the build brief, including the ``unrelated`` escape hatch for
inbox noise (newsletters, LinkedIn digests, job alerts).

Failure handling has two tiers:

* **Per-email failures** — the model returned something we can't trust (not
  JSON, missing fields, a status outside the allowed set). These come back as
  a ``ClassificationResult`` with ``needs_review=True``; the caller routes them
  to the manual review queue (``events.confirmed = 0``) instead of writing a
  possibly-wrong update. This is the behaviour the sync must never break on.
* **Systemic failures** — Ollama is unreachable, timed out, or the model is
  missing. These raise ``ClassifierUnavailable`` so the caller can stop the
  sync and surface the problem, rather than dumping every email into review.

This module classifies only. Applying the confidence threshold and deciding
auto-write vs. review is the matcher's job (see the build brief).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

import requests

# --- Ollama connection / model (env-overridable; can move to config.py later) ---
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")
NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "8192"))
REQUEST_TIMEOUT = float(os.environ.get("OLLAMA_TIMEOUT", "120"))
MAX_BODY_CHARS = 6000

# Closed set of statuses the classifier may emit (build brief). "unrelated" is
# the escape hatch for inbox noise that isn't about a specific application.
VALID_STATUSES = (
    "applied",
    "assessment_invite",
    "interview_invite",
    "interview_complete",
    "offer",
    "rejection",
    "withdrawn",
    "unrelated",
)
_REQUIRED_FIELDS = ("company", "status", "stage", "confidence")

SYSTEM_PROMPT = """\
You classify emails about job applications for a personal application tracker.

Read the email and decide what it says about a specific job the recipient
applied for. Respond with ONE JSON object and nothing else - no prose, no
markdown code fences.

The JSON object must have exactly these four keys:

- "company": the employer the application is with, as a short plain name
  (e.g. "Acme Widgets", "Globex"). The company is often in the sender
  address or the signature, not only the subject line. Use null if the email
  is unrelated or no employer can be identified.

- "status": exactly one of these values:
    "applied"            - application submitted / received / acknowledged
    "assessment_invite"  - invited to an online test, assessment, or challenge
    "interview_invite"   - invited to interview, or asked to book an interview
    "interview_complete" - an interview or assessment centre has taken place,
                           or a post-interview follow-up with no decision yet
    "offer"              - a job offer is being made
    "rejection"          - application unsuccessful / declined / not progressing
    "withdrawn"          - the applicant withdrew, or the role was cancelled
    "unrelated"          - NOT an update about a specific application the
                           recipient made

Interview vs. assessment - classify by CONTENT, not delivery format. The
platform or length of a meeting (phone call, Zoom, Microsoft Teams, video
call, in person; 15 minutes or a full day) never decides between
"interview_invite" and "assessment_invite", and neither does how warm or
formal the tone is:
  * Any live conversation with people - exploratory, introductory, screening,
    technical, or a final panel, on any platform - is "interview_invite".
  * A test, graded challenge, timed task, or scored assessment event - e.g.
    an online cognitive test, an Excel assignment, or an assessment centre -
    is "assessment_invite", even when a call is scheduled to send or discuss it.
  * If an email invites you to an interview AND attaches a task (e.g. a final
    interview that also asks for an Excel assignment), the primary invitation
    wins: it is "interview_invite", with the task as a component.

- "stage": the name of the stage if the email names one (e.g. "Assessment
  Centre", "First-round screen", "Final round", "Phone screen"). Use null if
  no stage is named.

- "confidence": a number from 0.0 to 1.0 - how sure you are of "status".

Use "unrelated" for anything that is not a real update on an application the
recipient submitted: newsletters, LinkedIn notifications and digests, "jobs you
may be interested in" recommendations, job alerts, marketing, event invites,
and networking blasts. These often look job-adjacent but concern no specific
application. When you are unsure whether an email is about a real application
the recipient actually made, choose "unrelated" with a low confidence.
"""


class ClassifierUnavailable(RuntimeError):
    """The Ollama backend could not be reached, timed out, or lacks the model.

    Systemic (it affects every email), so callers should stop the sync and
    surface this rather than routing emails to the review queue.
    """


@dataclass
class ClassificationResult:
    status: str | None
    company: str | None
    stage: str | None
    confidence: float
    needs_review: bool
    raw_response: str
    error: str | None = None


def classify_email(subject: str, body: str, sender: str = "") -> ClassificationResult:
    """Classify one email.

    Returns a ``ClassificationResult``. Bad model output never raises: it comes
    back with ``needs_review=True``. Only systemic backend problems raise
    ``ClassifierUnavailable``.
    """
    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0, "num_ctx": NUM_CTX},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_message(sender, subject, body)},
        ],
    }
    try:
        resp = requests.post(
            f"{OLLAMA_HOST}/api/chat", json=payload, timeout=REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        content = resp.json().get("message", {}).get("content", "")
    except requests.exceptions.RequestException as exc:
        raise ClassifierUnavailable(
            f"Could not reach Ollama at {OLLAMA_HOST} with model "
            f"'{OLLAMA_MODEL}': {exc}. Is Ollama running (`ollama serve`) and "
            f"has the model been pulled (`ollama pull {OLLAMA_MODEL}`)?"
        ) from exc

    return _parse(content or "")


def _build_user_message(sender: str, subject: str, body: str) -> str:
    body = (body or "").strip()
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n[... truncated ...]"
    return (
        f"From: {sender or '(unknown sender)'}\n"
        f"Subject: {subject or '(no subject)'}\n\n"
        f"{body or '(empty body)'}"
    )


def _parse(content: str) -> ClassificationResult:
    """Validate the model's output; downgrade anything untrustworthy to review."""
    data = _load_json(content)
    if data is None:
        return _review(content, "model did not return valid JSON")
    if not isinstance(data, dict):
        return _review(content, "model JSON was not an object")

    missing = [key for key in _REQUIRED_FIELDS if key not in data]
    if missing:
        return _review(content, f"missing field(s): {', '.join(missing)}")

    status = data["status"]
    if not isinstance(status, str) or status not in VALID_STATUSES:
        return _review(content, f"status not in the allowed set: {status!r}")

    confidence = _coerce_confidence(data["confidence"])
    if confidence is None:
        return _review(content, f"confidence not a number: {data['confidence']!r}")

    return ClassificationResult(
        status=status,
        company=_clean(data["company"]),
        stage=_clean(data["stage"]),
        confidence=confidence,
        needs_review=False,
        raw_response=content,
    )


def _load_json(content: str):
    """Parse JSON from the model. Returns the object, or None if unparseable.

    With ``format="json"`` Ollama should already return clean JSON; the fenced
    fallback is cheap insurance if that constraint is ever relaxed.
    """
    content = content.strip()
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        pass
    start, end = content.find("{"), content.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(content[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _coerce_confidence(value):
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return None


def _clean(value):
    """Normalise a string field; empty / null-ish placeholders become None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"null", "none", "n/a", "unknown"}:
        return None
    return text


def _review(raw: str, error: str) -> ClassificationResult:
    return ClassificationResult(
        status=None,
        company=None,
        stage=None,
        confidence=0.0,
        needs_review=True,
        raw_response=raw,
        error=error,
    )


if __name__ == "__main__":
    # Smoke test against the live local model: a rejection, an assessment
    # invite, and a LinkedIn digest that should classify as "unrelated".
    samples = [
        (
            "careers@acme.example",
            "Update on your application to Acme Widgets",
            "Hi Sam, thank you for interviewing for the Graduate Analyst "
            "role. After careful consideration we won't be moving "
            "forward with your application at this time. We wish you the best.",
        ),
        (
            "no-reply@globex.example",
            "Globex - next steps",
            "Congratulations on progressing! Please complete your online "
            "assessment within 5 days to move to the next stage of the Graduate "
            "Programme 2027 process.",
        ),
        (
            "jobalerts@jobboard.example",
            "8 new jobs for 'data analyst'",
            "See the latest jobs matching your search. Junior Data Analyst roles "
            "are being posted now near you. Apply today!",
        ),
    ]
    for sender, subject, body in samples:
        result = classify_email(subject, body, sender)
        print(f"\nFrom:    {sender}\nSubject: {subject}")
        print(
            f"  status={result.status!r} company={result.company!r} "
            f"stage={result.stage!r} confidence={result.confidence} "
            f"needs_review={result.needs_review}"
        )
        if result.error:
            print(f"  error: {result.error}")
