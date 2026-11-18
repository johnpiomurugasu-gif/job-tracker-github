"""Job-tracker dashboard (Streamlit) — reads tracker.db, six sections.

Adapts dashboard-mock-v13-spacing.html to Streamlit primitives. Every number
is queried from tracker.db; nothing is hardcoded from the mock's sample data.
Run:  streamlit run dashboard/app.py
"""
from __future__ import annotations

import html
import sqlite3
from datetime import date
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

DB_PATH = Path(__file__).resolve().parent.parent / "db" / "tracker.db"

STATUS_LABEL = {
    "applied": "Applied", "assessment_invite": "Assessment",
    "interview_invite": "Interview", "interview_complete": "Interview",
    "offer": "Offer", "rejection": "Rejected", "withdrawn": "Withdrawn",
}
STATUS_ORDER = ["applied", "assessment_invite", "interview_invite", "offer",
                "rejection", "withdrawn"]
REACH = {"assessment_invite", "interview_invite", "interview_complete", "offer"}
INTERVIEW = {"interview_invite", "interview_complete", "offer"}
TERMINAL = {"rejection", "withdrawn", "offer"}
STALE_DAYS = 21  # "3+ weeks" (mock: gone quiet threshold)


# --------------------------------------------------------------------------- #
# data access                                                                 #
# --------------------------------------------------------------------------- #
def conn() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def apps_df() -> pd.DataFrame:
    with conn() as c:
        return pd.read_sql(
            """SELECT a.id, a.role_title, a.current_status, a.current_stage,
                      a.date_applied, a.source, a.last_updated, a.confirmed_by_user,
                      c.name AS company, c.industry
                 FROM applications a JOIN companies c ON c.id = a.company_id
                ORDER BY a.id""", c)


def events_df() -> pd.DataFrame:
    with conn() as c:
        return pd.read_sql(
            """SELECT e.application_id, e.event_type, e.event_date, e.detail_text,
                      a.role_title, c.name AS company, c.industry
                 FROM events e
                 JOIN applications a ON a.id = e.application_id
                 JOIN companies c ON c.id = a.company_id
                ORDER BY e.event_date, e.id""", c)


def review_df() -> pd.DataFrame:
    with conn() as c:
        return pd.read_sql(
            "SELECT * FROM review_queue ORDER BY COALESCE(received_date, created_at) DESC", c)


def confirm_review(rid, company, role, industry, status, stage) -> None:
    """Create a confirmed company + application from a review row, drop the row."""
    with conn() as c:
        cur = c.execute(
            "INSERT INTO companies (name, industry, notes, confirmed_by_user) "
            "VALUES (?, ?, NULL, 1)", (company, industry or None))
        c.execute(
            "INSERT INTO applications (company_id, role_title, current_status, "
            "current_stage, last_updated, confirmed_by_user) VALUES (?, ?, ?, ?, ?, 1)",
            (cur.lastrowid, role or None, status, stage or None, date.today().isoformat()))
        c.execute("DELETE FROM review_queue WHERE id = ?", (rid,))
        c.commit()


def discard_review(rid) -> None:
    with conn() as c:
        c.execute("DELETE FROM review_queue WHERE id = ?", (rid,))
        c.commit()


# --------------------------------------------------------------------------- #
# pure helpers (unit-tested in dashboard/_selfcheck.py)                        #
# --------------------------------------------------------------------------- #
def _txt(v) -> str:
    return "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v)


def _h(v) -> str:
    """v19: _txt, escaped for HTML, so text from emails or the database never becomes page code. _txt itself is unchanged (the server uses it)."""
    return html.escape(_txt(v))


class _Safe(str):
    """v19: markup built here from escaped parts; html_table passes it through and escapes every other cell."""


def _or(v, default: str) -> str:
    return _txt(v) or default


def role_type(role: str | None) -> str:
    r = _txt(role).lower()
    for kw, label in (("analyst", "Analyst"), ("consultant", "Consultant"),
                      ("engineer", "Engineer"), ("operations", "Operations"),
                      ("ops", "Operations")):
        if kw in r:
            return label
    return "Other"


def rejection_bucket(stage: str | None, types: set[str]) -> str:
    s = _txt(stage).lower()
    if "interview" in s or types & {"interview_invite", "interview_complete"}:
        return "After interview"
    if "assessment" in s or "assessment_invite" in types:
        return "After assessment"
    return "No response, then rejected"


def _d(s):
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def compute_metrics(apps: pd.DataFrame, events: pd.DataFrame, today: date) -> dict:
    ev_by_app: dict = {}
    for _, e in events.iterrows():
        ev_by_app.setdefault(e["application_id"], []).append(e)

    n = len(apps)
    reached = interviewed = offers = rejected = gone_quiet = 0
    gaps, applied_dates, outcome, industry, source, rej_stage = [], [], {}, {}, {}, {}

    for _, a in apps.iterrows():
        evs = ev_by_app.get(a["id"], [])
        types = {e["event_type"] for e in evs}
        status, stage = a["current_status"], _txt(a["current_stage"]).lower()

        if types & REACH or status in REACH or "assessment" in stage or "interview" in stage:
            reached += 1
        if types & INTERVIEW or status in INTERVIEW or "interview" in stage:
            interviewed += 1
        if status == "offer" or "offer" in types:
            offers += 1
        if status == "rejection" or "rejection" in types:
            rejected += 1
            bucket = rejection_bucket(a["current_stage"], types)
            rej_stage[bucket] = rej_stage.get(bucket, 0) + 1

        lbl = STATUS_LABEL.get(status, status)
        outcome[lbl] = outcome.get(lbl, 0) + 1
        ind, src = _or(a["industry"], "Not recorded"), _or(a["source"], "Not recorded")
        industry[ind] = industry.get(ind, 0) + 1
        source[src] = source.get(src, 0) + 1

        dates = [_d(e["event_date"]) for e in evs] + [_d(a["last_updated"])]
        dates = [d for d in dates if d]
        if dates and status not in TERMINAL and (today - max(dates)).days >= STALE_DAYS:
            gone_quiet += 1

        applied_ev = [_d(e["event_date"]) for e in evs if e["event_type"] == "applied"]
        resp_ev = [_d(e["event_date"]) for e in evs if e["event_type"] not in ("applied", "unrelated")]
        applied_ev = [d for d in applied_ev if d]
        resp_ev = [d for d in resp_ev if d]
        if applied_ev and resp_ev and min(resp_ev) >= min(applied_ev):
            gaps.append((min(resp_ev) - min(applied_ev)).days)
        applied_dates += applied_ev or ([_d(a["date_applied"])] if _d(a["date_applied"]) else [])

    # applications/week average over the span we have dates for
    apps_per_week = None
    if len(applied_dates) >= 2:
        weeks = max((max(applied_dates) - min(applied_dates)).days / 7, 1)
        apps_per_week = round(len(applied_dates) / weeks, 1)

    # weekly applied vs rejected (from events)
    weekly = pd.DataFrame()
    if not events.empty:
        ev = events.copy()
        ev["week"] = pd.to_datetime(ev["event_date"], errors="coerce").dt.to_period("W").dt.start_time
        ev = ev.dropna(subset=["week"])
        if not ev.empty:
            weekly = (ev.assign(kind=ev["event_type"].map(
                          {"applied": "Applied", "rejection": "Rejected"}))
                        .dropna(subset=["kind"])
                        .groupby(["week", "kind"]).size().unstack(fill_value=0)
                        .reindex(columns=["Applied", "Rejected"], fill_value=0))

    return dict(
        n=n, apps_per_week=apps_per_week,
        reach=reached, reach_pct=pct(reached, n),
        interviewed=interviewed, offers=offers, i2o_pct=pct(offers, interviewed),
        rejected=rejected, rej_pct=pct(rejected, n),
        avg_response=round(sum(gaps) / len(gaps)) if gaps else None,
        gone_quiet=gone_quiet,
        outcome=outcome, industry=industry, source=source, rej_stage=rej_stage,
        weekly=weekly,
    )


def pct(a, b):
    return round(100 * a / b) if b else 0


# --------------------------------------------------------------------------- #
# styling + custom-HTML render helpers (see dashboard/style.css)              #
# --------------------------------------------------------------------------- #
PILL_CLASS = {"applied": "applied", "assessment_invite": "assessment",
              "interview_invite": "interview", "interview_complete": "interview",
              "offer": "offer", "rejection": "rejected", "withdrawn": "withdrawn"}
OUTCOME_COLOR = {"Applied": "#9DB0AF", "Assessment": "#d9a441", "Interview": "#5CC8CD",
                 "Offer": "#5CC8CD", "Rejected": "#d67a6c", "Withdrawn": "#9EA7BA"}


def load_css():
    st.markdown(f"<style>{(Path(__file__).parent / 'style.css').read_text()}</style>",
                unsafe_allow_html=True)


def pill(status) -> str:
    s = _txt(status)
    return _Safe(f'<span class="pill pill-{PILL_CLASS.get(s, "applied")}">{html.escape(STATUS_LABEL.get(s, s or "—"))}</span>')


def company_cell(name, role) -> str:
    return _Safe(f'<span class="jt-name">{_h(name) or "—"}</span><span class="jt-role">{_h(role)}</span>')


def kpi_row(cells: list[dict]):
    html = '<div class="kpi-row">'
    for c in cells:
        delta = f'<div class="kpi-delta">{c["delta"]}</div>' if c.get("delta") else ""
        html += (f'<div class="kpi-cell"><div class="kpi-label">{c["label"]}</div>'
                 f'<div class="kpi-num {c.get("cls", "")}">{c["value"]}</div>{delta}</div>')
    st.markdown(html + "</div>", unsafe_allow_html=True)


def html_table(headers: list[str], rows: list[list[str]]):
    head = "".join(f"<th>{_h(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c if isinstance(c, _Safe) else _h(c)}</td>" for c in r) + "</tr>" for r in rows)
    st.markdown(f'<table class="jt-table"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>',
                unsafe_allow_html=True)


def bar_breakdown(mapping: dict):
    if not mapping:
        st.caption("No data yet.")
        return
    mx = max(mapping.values()) or 1
    html = ""
    for label, count in sorted(mapping.items(), key=lambda x: -x[1]):
        html += (f'<div class="bar-row"><div class="bar-label">{_h(label)}</div>'
                 f'<div class="bar-track"><div class="bar-fill" style="width:{round(100 * count / mx)}%">'
                 f'</div></div><div class="bar-count">{count}</div></div>')
    st.markdown(html, unsafe_allow_html=True)


def _alt(chart):
    return (chart.configure(background="transparent").configure_view(stroke=None)
            .configure_axis(labelColor="#9DB0AF", titleColor="#9DB0AF",
                            gridColor="#161a1b", domainColor="#2e3a3a")
            .configure_legend(labelColor="#9DB0AF", titleColor="#9DB0AF"))


def donut(mapping: dict):
    if not mapping:
        st.caption("No data yet.")
        return
    dom = list(mapping)
    df = pd.DataFrame({"outcome": dom, "count": list(mapping.values())})
    chart = alt.Chart(df).mark_arc(innerRadius=55).encode(
        theta="count:Q",
        color=alt.Color("outcome:N", title=None, scale=alt.Scale(
            domain=dom, range=[OUTCOME_COLOR.get(o, "#9DB0AF") for o in dom])),
        tooltip=["outcome", "count"])
    st.altair_chart(_alt(chart), use_container_width=True, theme=None)


# --------------------------------------------------------------------------- #
# confirm dialog                                                              #
# --------------------------------------------------------------------------- #
@st.dialog("Confirm & create application")
def confirm_dialog(row):
    st.caption("Edit before creating a company + application, or discard.")
    company = st.text_input("Company", _txt(row.get("extracted_company")))
    role = st.text_input("Role", "")
    industry = st.text_input("Industry", "")
    opts = STATUS_ORDER
    cur = row.get("status") if row.get("status") in opts else "applied"
    status = st.selectbox("Status", opts, index=opts.index(cur),
                          format_func=lambda s: STATUS_LABEL.get(s, s))
    c1, c2 = st.columns(2)
    if c1.button("Confirm & create", type="primary", use_container_width=True):
        confirm_review(row["id"], company, role, industry, status,
                       _txt(row.get("stage")) or None)
        st.rerun()
    if c2.button("Discard", type="secondary", use_container_width=True):
        discard_review(row["id"])
        st.rerun()


# --------------------------------------------------------------------------- #
# sections                                                                    #
# --------------------------------------------------------------------------- #
def render_priority():
    apps = apps_df()
    st.title("Priority")
    counts = apps["current_status"].value_counts()
    with conn() as c:
        unconfirmed = c.execute(
            "SELECT COUNT(*) FROM applications WHERE confirmed_by_user = 0").fetchone()[0]
    total = len(apps)
    rejected = int(counts.get("rejection", 0))
    kpi_row([
        {"label": "Total applications", "value": total},
        {"label": "Awaiting response", "value": int(counts.get("applied", 0)),
         "delta": "First responses can take 1–3 weeks"},
        {"label": "Interview / assessment", "cls": "accent",
         "value": int(counts.get("interview_invite", 0) + counts.get("interview_complete", 0)
                      + counts.get("assessment_invite", 0))},
        {"label": "Rejected", "cls": "red", "value": rejected,
         "delta": f"{pct(rejected, total)}% of total applications"},
        {"label": "Unconfirmed, new", "cls": "amber", "value": unconfirmed,
         "delta": "Auto-added from inbox"},
    ])

    st.divider()
    st.markdown("### Needs your review")
    rq = review_df()
    st.markdown(f'<div class="jt-sub">{len(rq)} item(s) detected from your inbox · '
                f'not yet confirmed</div>', unsafe_allow_html=True)
    if rq.empty:
        st.info("Nothing to review. New companies from `run_sync` land here.")
    else:
        for _, r in rq.iterrows():
            row = r.to_dict()
            cols = st.columns([3, 4, 1.6, 1.6], vertical_alignment="center")
            cols[0].markdown(company_cell(row.get("extracted_company"), row.get("sender")),
                             unsafe_allow_html=True)
            cols[1].markdown(f'{pill(row.get("status"))} <span style="color:#9DB0AF">'
                             f'{_h(row.get("subject")) or _h(row.get("reason"))}</span>',
                             unsafe_allow_html=True)
            if cols[2].button("Confirm", key=f"cf{row['id']}", type="primary",
                              use_container_width=True):
                confirm_dialog(row)
            if cols[3].button("Discard", key=f"ds{row['id']}", type="secondary",
                              use_container_width=True):
                discard_review(row["id"])
                st.rerun()

    st.divider()
    st.markdown("### Just moved · status changed in the last sync")
    ev = events_df().tail(8).iloc[::-1]
    if ev.empty:
        st.caption("No events yet — this fills in after your first sync.")
    else:
        html_table(["Company", "Status", "Date"],
                   [[company_cell(e["company"], e["role_title"]), pill(e["event_type"]),
                     _txt(e["event_date"])] for _, e in ev.iterrows()])


def render_pipeline():
    apps = apps_df()
    st.title("Pipeline")
    apps["Industry"] = apps["industry"].fillna("Not recorded")
    apps["Status"] = apps["current_status"].map(lambda s: STATUS_LABEL.get(s, s))
    q = st.text_input("Search company or role", "")
    f = st.columns(2)
    statuses = ["All statuses"] + sorted(apps["Status"].unique())
    inds = ["All industries"] + sorted(apps["Industry"].unique())
    sf = f[0].selectbox("Status", statuses)
    inf = f[1].selectbox("Industry", inds)

    view = apps
    if q:
        m = view["company"].str.contains(q, case=False, na=False) | \
            view["role_title"].str.contains(q, case=False, na=False)
        view = view[m]
    if sf != "All statuses":
        view = view[view["Status"] == sf]
    if inf != "All industries":
        view = view[view["Industry"] == inf]

    st.markdown(f'<div class="jt-sub">showing {len(view)} of {len(apps)}</div>',
                unsafe_allow_html=True)
    if view.empty:
        st.info("No applications match these filters.")
        return
    html_table(
        ["Company", "Industry", "Status", "Stage", "Last activity"],
        [[company_cell(a["company"], a["role_title"]), _txt(a["Industry"]),
          pill(a["current_status"]), _txt(a["current_stage"]), _txt(a["last_updated"])]
         for _, a in view.iterrows()])


def render_timeline():
    st.title("Timeline")
    st.caption("each point is one real email event — hover to preview, click for detail below")
    ev = events_df()
    if ev.empty:
        st.info("No events yet. Points appear here once `run_sync` records email events.")
        return
    ev = ev.copy().reset_index(drop=True)
    ev["date"] = pd.to_datetime(ev["event_date"], errors="coerce")
    ev["Status"] = ev["event_type"].map(lambda s: STATUS_LABEL.get(s, s))
    ev["idx"] = ev.index
    dom = sorted(ev["Status"].unique())
    picked = alt.selection_point(name="pt", fields=["idx"], on="click", empty=False)
    chart = alt.Chart(ev).mark_circle(size=140).encode(
        x=alt.X("date:T", title=None),
        y=alt.Y("Status:N", title=None),
        color=alt.Color("Status:N", legend=None, scale=alt.Scale(
            domain=dom, range=[OUTCOME_COLOR.get(s, "#9DB0AF") for s in dom])),
        tooltip=["company", "role_title", "Status", "event_date"],
    ).add_params(picked).properties(height=260)
    sel = st.altair_chart(_alt(chart), use_container_width=True, on_select="rerun", theme=None)
    rows = (sel or {}).get("selection", {}).get("pt", [])
    st.markdown("### Event detail")
    if rows and rows[0].get("idx") is not None and int(rows[0]["idx"]) < len(ev):
        r = ev.iloc[int(rows[0]["idx"])]
        st.markdown(company_cell(r["company"], r["role_title"]), unsafe_allow_html=True)
        st.markdown(f'<div class="jt-sub">Industry: {_or(r["industry"], "Not recorded")} · '
                    f'Event: {r["Status"]} · Date: {_txt(r["event_date"])}</div>',
                    unsafe_allow_html=True)
        if _txt(r["detail_text"]):
            st.caption(_txt(r["detail_text"]))
    else:
        st.caption("Click a point on the timeline above to see its detail here.")


def render_metrics():
    apps, ev = apps_df(), events_df()
    m = compute_metrics(apps, ev, date.today())
    st.title("Metrics")
    st.markdown(f'<div class="jt-sub">based on {m["n"]} tracked applications</div>',
                unsafe_allow_html=True)
    kpi_row([
        {"label": "Applications / week (avg)",
         "value": "—" if m["apps_per_week"] is None else m["apps_per_week"]},
        {"label": "Reach assessment or interview", "cls": "accent", "value": f"{m['reach_pct']}%",
         "delta": f"{m['reach']} of {m['n']} applications"},
        {"label": "Interview → offer", "value": f"{m['i2o_pct']}%",
         "delta": f"{m['offers']} of {m['interviewed']} interviews"},
        {"label": "Rejection rate", "cls": "red", "value": f"{m['rej_pct']}%",
         "delta": f"{m['rejected']} of {m['n']}"},
        {"label": "Avg. time to first response",
         "value": "—" if m["avg_response"] is None else f"{m['avg_response']} days"},
        {"label": "Gone quiet, 3+ weeks", "cls": "amber", "value": m["gone_quiet"],
         "delta": "see Gone quiet tab"},
    ])

    st.divider()
    st.markdown("### Applications by week")
    st.markdown('<div class="jt-sub">applied vs. rejections landing, same week</div>',
                unsafe_allow_html=True)
    if m["weekly"].empty:
        st.caption("No dated events yet — this chart fills in after your first sync.")
    else:
        wl = m["weekly"].reset_index().melt("week", var_name="series", value_name="count")
        line = alt.Chart(wl).mark_line(point=True).encode(
            x=alt.X("week:T", title=None), y=alt.Y("count:Q", title=None),
            color=alt.Color("series:N", title=None, scale=alt.Scale(
                domain=["Applied", "Rejected"], range=["#5CC8CD", "#d67a6c"])))
        st.altair_chart(_alt(line), use_container_width=True, theme=None)

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("### Outcome mix")
        st.markdown('<div class="jt-sub">where all applications currently stand</div>',
                    unsafe_allow_html=True)
        donut(m["outcome"])
        st.markdown("### By industry")
        st.markdown('<div class="jt-sub">application count per sector</div>', unsafe_allow_html=True)
        bar_breakdown(m["industry"])
    with c2:
        st.markdown("### Where rejections land")
        st.markdown('<div class="jt-sub">stage reached before rejection</div>', unsafe_allow_html=True)
        bar_breakdown(m["rej_stage"])
        st.markdown("### By source")
        st.markdown('<div class="jt-sub">how each application was submitted</div>',
                    unsafe_allow_html=True)
        bar_breakdown(m["source"])


def render_stale():
    st.title("Gone quiet")
    st.markdown('<div class="jt-sub">no activity in 3+ weeks since the last real signal</div>',
                unsafe_allow_html=True)
    apps, ev = apps_df(), events_df()
    last = ev.groupby("application_id")["event_date"].max().to_dict() if not ev.empty else {}
    today = date.today()
    stale = []
    for _, a in apps.iterrows():
        if a["current_status"] in TERMINAL:
            continue
        last_d = _d(last.get(a["id"])) or _d(a["last_updated"])
        if last_d and (today - last_d).days >= STALE_DAYS:
            stale.append((a, last_d))

    if not stale:
        st.info("Nothing has gone quiet — every open application has recent activity.")
    else:
        for a, last_d in stale:
            c = st.columns([4, 2], vertical_alignment="center")
            c[0].markdown(
                f'<div class="jt-card">{company_cell(a["company"], a["role_title"])}'
                f'<div class="jt-note">{_txt(a["current_stage"])} · last signal {last_d}</div></div>',
                unsafe_allow_html=True)
            c[1].date_input("Suggested follow-up", today, key=f"fu{a['id']}")

    st.divider()
    st.markdown('<div class="jt-sub">Follow-up planning — pick a nudge date above; native date '
                "picker replaces the mock's month grid.</div>", unsafe_allow_html=True)


def render_filter():
    st.title("By industry / role")
    st.markdown('<div class="jt-sub">combine both to narrow the pipeline</div>',
                unsafe_allow_html=True)
    apps = apps_df()
    apps["Industry"] = apps["industry"].fillna("Not recorded")
    apps["Role type"] = apps["role_title"].map(role_type)
    c = st.columns(2)
    ind = c[0].selectbox("Industry", ["All"] + sorted(apps["Industry"].unique()))
    rt = c[1].selectbox("Role type", ["All"] + sorted(apps["Role type"].unique()))
    view = apps
    if ind != "All":
        view = view[view["Industry"] == ind]
    if rt != "All":
        view = view[view["Role type"] == rt]
    if view.empty:
        st.info("No applications match these filters.")
        return
    html_table(
        ["Company", "Industry", "Role type", "Status"],
        [[company_cell(a["company"], a["role_title"]), _txt(a["Industry"]),
          a["Role type"], pill(a["current_status"])] for _, a in view.iterrows()])


SECTIONS = {
    "Priority": render_priority, "Pipeline": render_pipeline,
    "Timeline": render_timeline, "Metrics": render_metrics,
    "Gone quiet": render_stale, "By industry / role": render_filter,
}


def main():
    st.set_page_config(page_title="Job Tracker", layout="wide")
    load_css()
    if not DB_PATH.exists():
        st.error(f"No database at {DB_PATH}. Run db/seed.py first.")
        return
    choice = st.sidebar.radio("Job Tracker", list(SECTIONS), label_visibility="visible")
    st.sidebar.caption("Theme: use the ☰ menu → Settings. In-app toggle from the "
                       "mock is not carried over.")
    SECTIONS[choice]()


if __name__ == "__main__":
    main()
