"""Asserts for the pure dashboard logic. Run: python dashboard/_selfcheck.py"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pandas as pd
from dashboard.app import compute_metrics, rejection_bucket, role_type

assert role_type("Graduate Analyst") == "Analyst"
assert role_type("Junior Consultant, C&I") == "Consultant"
assert role_type("Brokerage Operations Associate") == "Operations"
assert role_type("Digital Development Programme") == "Other"
assert rejection_bucket("Rejected post-interview", set()) == "After interview"
assert rejection_bucket("after final round", {"interview_invite"}) == "After interview"
assert rejection_bucket("Rejected after Assessment Centre", set()) == "After assessment"
assert rejection_bucket("", set()) == "No response, then rejected"

apps = pd.DataFrame([
    dict(id=1, role_title="Analyst", current_status="applied", current_stage=None,
         date_applied="2026-08-01", source="LinkedIn", last_updated="2026-08-01",
         confirmed_by_user=1, company="X", industry="Fintech"),
    dict(id=2, role_title="Consultant", current_status="rejection",
         current_stage="Rejected post-interview", date_applied="2026-07-01", source=None,
         last_updated="2026-08-20", confirmed_by_user=1, company="Y", industry="Finance"),
    dict(id=3, role_title="Analyst", current_status="offer", current_stage=None,
         date_applied="2026-06-01", source=None, last_updated="2026-09-10",
         confirmed_by_user=1, company="Z", industry="Fintech"),
])
events = pd.DataFrame([
    dict(application_id=1, event_type="applied", event_date="2026-08-01", detail_text="",
         role_title="Analyst", company="X", industry="Fintech"),
    dict(application_id=2, event_type="applied", event_date="2026-07-01", detail_text="",
         role_title="Consultant", company="Y", industry="Finance"),
    dict(application_id=2, event_type="interview_invite", event_date="2026-07-20",
         detail_text="", role_title="Consultant", company="Y", industry="Finance"),
    dict(application_id=2, event_type="rejection", event_date="2026-08-20", detail_text="",
         role_title="Consultant", company="Y", industry="Finance"),
    dict(application_id=3, event_type="applied", event_date="2026-06-01", detail_text="",
         role_title="Analyst", company="Z", industry="Fintech"),
    dict(application_id=3, event_type="interview_invite", event_date="2026-06-15",
         detail_text="", role_title="Analyst", company="Z", industry="Fintech"),
    dict(application_id=3, event_type="offer", event_date="2026-07-01", detail_text="",
         role_title="Analyst", company="Z", industry="Fintech"),
])
m = compute_metrics(apps, events, date(2026, 9, 15))
assert m["n"] == 3
assert m["reach"] == 2 and m["reach_pct"] == 67, m
assert m["interviewed"] == 2 and m["offers"] == 1 and m["i2o_pct"] == 50, m
assert m["rejected"] == 1 and m["rej_pct"] == 33, m
assert m["avg_response"] == 16, m          # gaps 19 & 14 -> mean 16.5 -> 16
assert m["gone_quiet"] == 1, m             # app1 only (non-terminal, 45d stale)
assert m["rej_stage"] == {"After interview": 1}, m
assert m["industry"] == {"Fintech": 2, "Finance": 1}, m
assert m["source"] == {"LinkedIn": 1, "Not recorded": 2}, m
assert m["outcome"] == {"Applied": 1, "Rejected": 1, "Offer": 1}, m
assert not m["weekly"].empty and list(m["weekly"].columns) == ["Applied", "Rejected"], m
assert abs(m["apps_per_week"] - 0.3) < 0.05, m

print("selfcheck OK:", {k: m[k] for k in
      ["n", "reach_pct", "i2o_pct", "rej_pct", "avg_response", "gone_quiet", "apps_per_week"]})
