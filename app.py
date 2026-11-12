"""
Bellhaven CRM Review App — local Flask server.

Routes
------
GET  /                   → dashboard (counts by match type)
GET  /proposals          → paginated proposal list (filterable by match_type)
GET  /proposals/<id>     → single proposal detail + evidence
POST /proposals/<id>/approve   → write change to CRM, record decision
POST /proposals/<id>/reject    → record rejection, no CRM write
GET  /crm/<account_id>   → pass-through fetch of a CRM account (for live diff)

Approved writes
---------------
The app handles three write paths automatically:

1. CONFIDENT_MATCH   → no write needed; just record the decision
2. NEEDS_FIX_* (direct re-parent, no CHOW)
   → PATCH existing account with proposed_changes
3. NEEDS_FIX_* with _chow=True in proposed_changes
   → POST new account under correct parent
   → PATCH old account: set chow_current_account = new_id, leave parent unchanged
4. NEW_ACCOUNT
   → POST new account
5. ORPHANED
   → PATCH status = "Needs Review" (or whatever reviewer specifies)
6. DUPLICATE
   → PATCH duplicate_of_account = survivor id, status = "Inactive"
"""

import json
import logging
import os
from functools import lru_cache
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template_string, request, url_for

import db as database
from crm_client import CRMClient
from matcher import CONFIDENT_MATCH, NEW_ACCOUNT, ORPHANED, Matcher

logger = logging.getLogger(__name__)

app = Flask(__name__)
# flash() messages live in the session, which Flask refuses to use without a key.
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(32).hex()
crm = CRMClient()


@lru_cache(maxsize=1)
def _bellhaven_parent_id():
    """Proposals don't all carry the parent id, so resolve it once from the CRM."""
    parent = Matcher(crm.get_all_accounts()).find_bellhaven_parent()
    return parent.get("id") if parent else None


def _account_payload(scraped: dict, fallback: dict) -> dict:
    """Build a new account in the CRM's own field names (billing_*), preferring
    the website's values and falling back to the existing CRM record."""
    def pick(scraped_key, crm_key):
        return scraped.get(scraped_key) or fallback.get(crm_key, "")

    return {
        "name": pick("name", "name"),
        "billing_street": pick("address", "billing_street"),
        "billing_city": pick("city", "billing_city"),
        "billing_state": pick("state", "billing_state"),
        "billing_zip": pick("zip_code", "billing_zip"),
        "phone": pick("phone", "phone"),
        "care_type": ", ".join(scraped.get("care_types") or []) or fallback.get("care_type", ""),
        "status": "Active",
    }


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

MATCH_TYPE_BADGE = {
    "CONFIDENT_MATCH": ("✅", "#22c55e", "Confident Match"),
    "NEEDS_FIX_NAME": ("✏️", "#f59e0b", "Name Fix"),
    "NEEDS_FIX_PARENT": ("🔗", "#f59e0b", "Parent Fix"),
    "NEEDS_FIX_BOTH": ("⚠️", "#ef4444", "Name + Parent Fix"),
    "NEW_ACCOUNT": ("🆕", "#3b82f6", "New Account"),
    "ORPHANED": ("👻", "#6b7280", "Orphaned"),
    "DUPLICATE": ("♊", "#a855f7", "Duplicate"),
}


def _badge(match_type):
    icon, color, label = MATCH_TYPE_BADGE.get(
        match_type, ("?", "#6b7280", match_type)
    )
    return icon, color, label


def _perform_crm_write(proposal: dict) -> dict:
    """
    Execute the appropriate CRM write for an approved proposal.
    Returns a dict describing what was done (stored in crm_result_json).
    """
    match_type = proposal.get("match_type")
    scraped = proposal.get("scraped_json") or {}
    crm_account = proposal.get("crm_json") or {}
    proposed_changes = proposal.get("proposed_changes_json") or {}
    bellhaven_parent_id = proposal.get("evidence_json", {}).get(
        "bellhaven_parent_id"
    ) if isinstance(proposal.get("evidence_json"), dict) else None

    # Remove internal flags
    changes = {k: v for k, v in proposed_changes.items() if not k.startswith("_")}
    is_chow = proposed_changes.get("_chow", False)
    crm_id = crm_account.get("id")

    result = {"actions": [], "errors": []}

    # ---- 1. Confident match — no write needed ----------------------
    if match_type == CONFIDENT_MATCH:
        result["actions"].append("No CRM change needed (confident match)")
        return result

    # ---- 2. New account — create it --------------------------------
    if match_type == NEW_ACCOUNT:
        new_data = _account_payload(scraped, {})
        parent_id = (proposal.get("evidence_json") or {}).get("bellhaven_parent_id") or _bellhaven_parent_id()
        if parent_id:
            new_data["parent_id"] = parent_id
        try:
            created = crm.create_account(new_data)
            result["actions"].append(f"Created new account: {created.get('id')} — {created.get('name')}")
            result["new_account_id"] = created.get("id")
        except Exception as e:
            result["errors"].append(f"Failed to create account: {e}")
        return result

    # ---- 3. Needs fix — reparent or rename -------------------------
    if match_type in ("NEEDS_FIX_NAME", "NEEDS_FIX_PARENT", "NEEDS_FIX_BOTH"):
        if is_chow:
            # CHOW path: preserve old account, create new, link with chow_current_account
            new_data = _account_payload(scraped, crm_account)
            parent_id = changes.get("parent_id")
            if parent_id:
                new_data["parent_id"] = parent_id
            try:
                created = crm.create_account(new_data)
                new_id = created.get("id")
                result["actions"].append(
                    f"CHOW: Created new account {new_id} — {created.get('name')}"
                )
                # Set chow_current_account on old account
                crm.update_account(crm_id, {"chow_current_account": new_id})
                result["actions"].append(
                    f"CHOW: Set chow_current_account={new_id} on old account {crm_id}"
                )
                result["new_account_id"] = new_id
            except Exception as e:
                result["errors"].append(f"CHOW write failed: {e}")
        else:
            # Direct update path
            if changes:
                try:
                    crm.update_account(crm_id, changes)
                    result["actions"].append(
                        f"Updated account {crm_id}: {list(changes.keys())}"
                    )
                except Exception as e:
                    result["errors"].append(f"Update failed: {e}")
            else:
                result["actions"].append("No field changes to write")
        return result

    # ---- 4. Orphaned — mark Needs Review ---------------------------
    if match_type == ORPHANED:
        orphan_changes = changes or {"status": "Needs Review"}
        try:
            crm.update_account(crm_id, orphan_changes)
            result["actions"].append(
                f"Orphan: Updated account {crm_id}: {list(orphan_changes.keys())}"
            )
        except Exception as e:
            result["errors"].append(f"Orphan update failed: {e}")
        return result

    # ---- 5. Duplicate — point at the survivor and deactivate ------
    if match_type == "DUPLICATE":
        try:
            crm.update_account(crm_id, changes)
            result["actions"].append(f"Duplicate: Updated account {crm_id}: {list(changes.keys())}")
        except Exception as e:
            result["errors"].append(f"Duplicate update failed: {e}")
        return result

    result["actions"].append(f"Unknown match type {match_type!r} — no write performed")
    return result


# ------------------------------------------------------------------ #
# HTML templates (single-file; no external dependencies)
# ------------------------------------------------------------------ #

BASE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bellhaven CRM Review</title>
<style>
  :root {
    --bg: #0f172a; --surface: #1e293b; --border: #334155;
    --text: #e2e8f0; --muted: #94a3b8; --accent: #38bdf8;
    --green: #22c55e; --yellow: #f59e0b; --red: #ef4444; --blue: #3b82f6;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: system-ui, sans-serif;
         font-size: 14px; line-height: 1.6; }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  header { background: var(--surface); border-bottom: 1px solid var(--border);
           padding: 12px 24px; display: flex; align-items: center; gap: 16px; }
  header h1 { font-size: 16px; font-weight: 700; }
  header nav a { color: var(--muted); margin-left: 16px; font-size: 13px; }
  .container { max-width: 1100px; margin: 0 auto; padding: 24px; }
  .card { background: var(--surface); border: 1px solid var(--border);
          border-radius: 8px; padding: 16px; margin-bottom: 16px; }
  .badge { display: inline-flex; align-items: center; gap: 4px;
           padding: 2px 8px; border-radius: 999px; font-size: 12px;
           font-weight: 600; border: 1px solid currentColor; }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .grid3 { display: grid; grid-template-columns: repeat(3,1fr); gap: 12px; }
  .stat-box { background: var(--surface); border: 1px solid var(--border);
              border-radius: 8px; padding: 16px; text-align: center; }
  .stat-box .num { font-size: 32px; font-weight: 800; }
  .stat-box .lbl { font-size: 12px; color: var(--muted); margin-top: 4px; }
  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; padding: 8px 12px; color: var(--muted);
       font-size: 11px; text-transform: uppercase; letter-spacing: .05em;
       border-bottom: 1px solid var(--border); }
  td { padding: 10px 12px; border-bottom: 1px solid var(--border); vertical-align: top; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(255,255,255,.02); }
  .btn { display: inline-flex; align-items: center; gap: 6px; padding: 7px 16px;
         border-radius: 6px; font-size: 13px; font-weight: 600; cursor: pointer;
         border: none; transition: opacity .15s; }
  .btn:hover { opacity: .85; }
  .btn-green { background: var(--green); color: #000; }
  .btn-red { background: var(--red); color: #fff; }
  .btn-ghost { background: transparent; border: 1px solid var(--border);
               color: var(--text); }
  .issue { background: rgba(239,68,68,.1); border-left: 3px solid var(--red);
           padding: 8px 12px; border-radius: 0 4px 4px 0; margin-bottom: 8px;
           font-size: 13px; }
  .kv-row { display: flex; gap: 12px; margin-bottom: 6px; font-size: 13px; }
  .kv-row .kv-key { color: var(--muted); min-width: 160px; flex-shrink: 0; }
  .change-row { background: rgba(59,130,246,.1); border-left: 3px solid var(--blue);
                padding: 8px 12px; border-radius: 0 4px 4px 0; margin-bottom: 6px;
                font-size: 13px; }
  .decided-approved { color: var(--green); font-weight: 600; }
  .decided-rejected  { color: var(--red); font-weight: 600; }
  .filter-bar { display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }
  .filter-btn { padding: 4px 12px; border-radius: 999px; font-size: 12px;
                font-weight: 600; cursor: pointer; border: 1px solid var(--border);
                background: transparent; color: var(--muted); }
  .filter-btn.active { background: var(--accent); color: #000; border-color: var(--accent); }
  textarea { width: 100%; background: var(--bg); border: 1px solid var(--border);
             color: var(--text); border-radius: 6px; padding: 8px; font-size: 13px;
             font-family: inherit; resize: vertical; }
  .flash { padding: 10px 16px; border-radius: 6px; margin-bottom: 16px; font-size: 13px; }
  .flash-ok  { background: rgba(34,197,94,.15); border: 1px solid var(--green); }
  .flash-err { background: rgba(239,68,68,.15); border: 1px solid var(--red); }
  pre { background: var(--bg); border: 1px solid var(--border); padding: 12px;
        border-radius: 6px; font-size: 12px; overflow-x: auto; white-space: pre-wrap; }
</style>
</head>
<body>
<header>
  <span>🏥</span>
  <h1>Bellhaven CRM Review</h1>
  <nav>
    <a href="/">Dashboard</a>
    <a href="/proposals">All Proposals</a>
    <a href="/proposals?status=pending">Pending</a>
  </nav>
</header>
<div class="container">
  {% with messages = get_flashed_messages(with_categories=true) %}
    {% for cat, msg in messages %}
      <div class="flash flash-{{ cat }}">{{ msg }}</div>
    {% endfor %}
  {% endwith %}
  {{ content | safe }}
</div>
</body>
</html>
"""


def render(content: str, **kwargs):
    from flask import render_template_string
    return render_template_string(BASE_HTML, content=content, **kwargs)


# ------------------------------------------------------------------ #
# Routes
# ------------------------------------------------------------------ #

@app.route("/")
def dashboard():
    all_p = database.get_all_proposals()
    total = len(all_p)
    pending = sum(1 for p in all_p if not p.get("decision"))
    approved = sum(1 for p in all_p if p.get("decision") == "approved")
    rejected = sum(1 for p in all_p if p.get("decision") == "rejected")

    by_type: dict = {}
    for p in all_p:
        mt = p.get("match_type", "?")
        by_type[mt] = by_type.get(mt, 0) + 1

    rows = ""
    for mt, count in sorted(by_type.items()):
        icon, color, label = _badge(mt)
        rows += f"""
        <tr>
          <td><span class="badge" style="color:{color}">{icon} {label}</span></td>
          <td style="text-align:right; font-weight:600">{count}</td>
          <td><a href="/proposals?match_type={mt}">View →</a></td>
        </tr>"""

    content = f"""
    <h2 style="margin-bottom:20px; font-size:20px">Pipeline Summary</h2>
    <div class="grid3" style="margin-bottom:24px">
      <div class="stat-box"><div class="num">{total}</div><div class="lbl">Total Proposals</div></div>
      <div class="stat-box"><div class="num" style="color:var(--yellow)">{pending}</div><div class="lbl">Pending Review</div></div>
      <div class="stat-box"><div class="num" style="color:var(--green)">{approved}</div><div class="lbl">Approved</div></div>
    </div>
    <div class="card">
      <h3 style="margin-bottom:12px; font-size:14px; color:var(--muted)">BREAKDOWN BY MATCH TYPE</h3>
      <table>{rows}</table>
    </div>
    <p style="margin-top:16px">
      <a href="/proposals?status=pending" class="btn btn-green">Review Pending ({pending})</a>
    </p>
    """
    return render(content)


@app.route("/proposals")
def proposal_list():
    match_type_filter = request.args.get("match_type", "")
    status_filter = request.args.get("status", "")

    all_p = database.get_all_proposals()

    if match_type_filter:
        all_p = [p for p in all_p if p.get("match_type") == match_type_filter]
    if status_filter == "pending":
        all_p = [p for p in all_p if not p.get("decision")]
    elif status_filter == "decided":
        all_p = [p for p in all_p if p.get("decision")]

    rows = ""
    for p in all_p:
        pid = p.get("id")
        mt = p.get("match_type", "?")
        icon, color, label = _badge(mt)
        scraped = p.get("scraped_json") or {}
        crm_acc = p.get("crm_json") or {}
        decision = p.get("decision")

        scraped_name = scraped.get("name", "—")
        crm_name = crm_acc.get("name", "—")
        scraped_city = f"{scraped.get('city','')}, {scraped.get('state','')}"

        dec_html = ""
        if decision == "approved":
            dec_html = '<span class="decided-approved">✅ Approved</span>'
        elif decision == "rejected":
            dec_html = '<span class="decided-rejected">❌ Rejected</span>'
        else:
            dec_html = '<span style="color:var(--yellow)">⏳ Pending</span>'

        issues = p.get("issues_json") or []
        issue_count = len(issues)

        rows += f"""
        <tr>
          <td><span class="badge" style="color:{color}">{icon} {label}</span></td>
          <td><a href="/proposals/{pid}"><strong>{scraped_name}</strong></a>
              <div style="color:var(--muted); font-size:12px">{scraped_city}</div></td>
          <td style="color:var(--muted)">{crm_name}</td>
          <td style="color:{'var(--red)' if issue_count else 'var(--green)'}">
            {issue_count} issue{'s' if issue_count != 1 else ''}
          </td>
          <td>{dec_html}</td>
          <td><a href="/proposals/{pid}">View →</a></td>
        </tr>"""

    # Filter bar
    types = list({p.get("match_type") for p in database.get_all_proposals()})
    filter_btns = ""
    for t in sorted(types):
        icon, color, label = _badge(t)
        active = "active" if match_type_filter == t else ""
        filter_btns += (
            f'<a href="/proposals?match_type={t}" class="filter-btn {active}">'
            f'<span style="color:{color}">{icon}</span> {label}</a>'
        )
    filter_btns += (
        f'<a href="/proposals?status=pending" class="filter-btn '
        f'{"active" if status_filter=="pending" else ""}">⏳ Pending</a>'
    )
    filter_btns += f'<a href="/proposals" class="filter-btn {"active" if not match_type_filter and not status_filter else ""}">All</a>'

    content = f"""
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:16px">
      <h2 style="font-size:20px">Proposals ({len(all_p)})</h2>
    </div>
    <div class="filter-bar">{filter_btns}</div>
    <div class="card" style="padding:0; overflow:hidden">
      <table>
        <thead><tr>
          <th>Type</th><th>Scraped Location</th><th>CRM Account</th>
          <th>Issues</th><th>Status</th><th></th>
        </tr></thead>
        <tbody>{rows if rows else '<tr><td colspan="6" style="text-align:center;color:var(--muted);padding:32px">No proposals found</td></tr>'}</tbody>
      </table>
    </div>
    """
    return render(content)


@app.route("/proposals/<pid>")
def proposal_detail(pid):
    p = database.get_proposal(pid)
    if not p:
        return render("<p>Proposal not found.</p>"), 404

    mt = p.get("match_type", "?")
    icon, color, label = _badge(mt)
    scraped = p.get("scraped_json") or {}
    crm_acc = p.get("crm_json") or {}
    evidence = p.get("evidence_json") or {}
    issues = p.get("issues_json") or []
    proposed_changes = p.get("proposed_changes_json") or {}
    decision = p.get("decision")

    # Issues section
    issues_html = "".join(
        f'<div class="issue">⚠️ {iss}</div>' for iss in issues
    ) or '<p style="color:var(--green)">✅ No issues detected</p>'

    # Evidence section — scraped vs CRM
    def kv(key, scraped_val, crm_val):
        same = str(scraped_val).lower().strip() == str(crm_val).lower().strip()
        style = "" if same or (not scraped_val and not crm_val) else "color:var(--yellow)"
        return f"""
        <tr>
          <td style="color:var(--muted); font-size:12px">{key}</td>
          <td>{scraped_val or "—"}</td>
          <td style="{style}">{crm_val or "—"}</td>
        </tr>"""

    evidence_rows = (
        kv("Name", scraped.get("name", ""), crm_acc.get("name", ""))
        + kv("Address", scraped.get("address", ""), crm_acc.get("address", "") or crm_acc.get("billing_street", ""))
        + kv("City", scraped.get("city", ""), crm_acc.get("city", "") or crm_acc.get("billing_city", ""))
        + kv("State", scraped.get("state", ""), crm_acc.get("state", "") or crm_acc.get("billing_state", ""))
        + kv("ZIP", scraped.get("zip_code", ""), crm_acc.get("zip", "") or crm_acc.get("zip_code", ""))
        + kv("Parent ID", evidence.get("bellhaven_parent_id", ""), crm_acc.get("parent_id", ""))
        + kv("Status", "Active (on website)", crm_acc.get("status", ""))
    )

    # Proposed changes section
    changes_html = ""
    for field_name, new_val in proposed_changes.items():
        if field_name.startswith("_"):
            continue
        old_val = crm_acc.get(field_name, "—")
        changes_html += f"""
        <div class="change-row">
          <strong>{field_name}</strong>:
          <span style="color:var(--red)"> "{old_val}"</span>
          → <span style="color:var(--green)">"{new_val}"</span>
        </div>"""
    if not changes_html and mt not in (NEW_ACCOUNT,):
        changes_html = '<p style="color:var(--muted); font-size:13px">No field changes</p>'
    if mt == NEW_ACCOUNT:
        changes_html = '<p style="color:var(--blue)">🆕 A new CRM account will be created</p>'

    # CHOW note
    chow_note = evidence.get("chow_note", "")
    chow_html = f'<div class="issue" style="border-color:var(--yellow)">{chow_note}</div>' if chow_note and chow_note != "N/A" else ""

    # Action form
    action_html = ""
    if not decision:
        action_html = f"""
        <div class="card" style="margin-top:16px">
          <h3 style="margin-bottom:12px; font-size:14px">Review Action</h3>
          <div style="display:flex; gap:12px; flex-wrap:wrap; align-items:flex-start">
            <form method="POST" action="/proposals/{pid}/approve" style="flex:1">
              <div style="margin-bottom:8px">
                <textarea name="note" rows="2" placeholder="Optional note..."></textarea>
              </div>
              <button type="submit" class="btn btn-green">✅ Approve & Write to CRM</button>
            </form>
            <form method="POST" action="/proposals/{pid}/reject" style="flex:1">
              <div style="margin-bottom:8px">
                <textarea name="note" rows="2" placeholder="Reason for rejection..."></textarea>
              </div>
              <button type="submit" class="btn btn-red">❌ Reject</button>
            </form>
          </div>
        </div>"""
    else:
        crm_result = p.get("crm_result_json")
        crm_result_html = ""
        if crm_result:
            actions_done = crm_result.get("actions", [])
            errors = crm_result.get("errors", [])
            crm_result_html = "<br>".join(f"• {a}" for a in actions_done)
            if errors:
                crm_result_html += "<br>" + "<br>".join(
                    f'<span style="color:var(--red)">✗ {e}</span>' for e in errors
                )
        dec_class = "decided-approved" if decision == "approved" else "decided-rejected"
        note_text = p.get("decision_note") or ""
        action_html = f"""
        <div class="card" style="margin-top:16px">
          <div class="{dec_class}" style="font-size:16px; margin-bottom:8px">
            {'✅ Approved' if decision=='approved' else '❌ Rejected'}
            <span style="font-size:12px; color:var(--muted); font-weight:400; margin-left:8px">
              {p.get('decided_at','')[:19].replace('T',' ')} UTC
            </span>
          </div>
          {f'<p style="color:var(--muted); font-size:13px">{note_text}</p>' if note_text else ''}
          {f'<div style="margin-top:8px; font-size:13px">{crm_result_html}</div>' if crm_result_html else ''}
        </div>"""

    # Similarity scores
    name_sim = evidence.get("name_similarity", "")
    addr_sim = evidence.get("address_similarity", "")
    sim_html = ""
    if name_sim or addr_sim:
        sim_html = f"""
        <div style="font-size:12px; color:var(--muted); margin-top:8px">
          Name similarity: <strong>{name_sim}%</strong> &nbsp;|&nbsp;
          Address similarity: <strong>{addr_sim}%</strong>
        </div>"""

    # Care types
    care_types = scraped.get("care_types", [])
    care_html = (
        " ".join(f'<span class="badge" style="color:var(--accent)">{ct}</span>' for ct in care_types)
        or '<span style="color:var(--muted)">—</span>'
    )

    content = f"""
    <div style="margin-bottom:16px">
      <a href="/proposals" style="color:var(--muted); font-size:13px">← Back to proposals</a>
    </div>
    <div style="display:flex; align-items:center; gap:12px; margin-bottom:16px">
      <span class="badge" style="color:{color}; font-size:14px; padding:4px 12px">{icon} {label}</span>
      <h2 style="font-size:20px">{scraped.get('name','New Account')}</h2>
    </div>

    <div class="grid2">
      <div>
        <div class="card">
          <h3 style="margin-bottom:12px; color:var(--muted); font-size:12px; text-transform:uppercase">Issues</h3>
          {issues_html}
          {chow_html}
        </div>
        <div class="card">
          <h3 style="margin-bottom:12px; color:var(--muted); font-size:12px; text-transform:uppercase">Proposed Changes</h3>
          {changes_html}
        </div>
      </div>
      <div>
        <div class="card">
          <h3 style="margin-bottom:12px; color:var(--muted); font-size:12px; text-transform:uppercase">Evidence — Scraped vs CRM</h3>
          <table>
            <thead><tr><th>Field</th><th>Website</th><th>CRM</th></tr></thead>
            <tbody>{evidence_rows}</tbody>
          </table>
          {sim_html}
        </div>
        <div class="card">
          <h3 style="margin-bottom:8px; color:var(--muted); font-size:12px; text-transform:uppercase">Care Types (from website)</h3>
          {care_html}
        </div>
      </div>
    </div>

    {action_html}

    <details style="margin-top:16px">
      <summary style="cursor:pointer; color:var(--muted); font-size:12px">Raw data (debug)</summary>
      <div class="grid2" style="margin-top:8px">
        <div><h4 style="font-size:12px; margin-bottom:4px; color:var(--muted)">Scraped</h4>
          <pre>{json.dumps(scraped, indent=2)}</pre></div>
        <div><h4 style="font-size:12px; margin-bottom:4px; color:var(--muted)">CRM Account</h4>
          <pre>{json.dumps(crm_acc, indent=2)}</pre></div>
      </div>
    </details>
    """
    return render(content)


@app.route("/proposals/<pid>/approve", methods=["POST"])
def approve(pid):
    p = database.get_proposal(pid)
    if not p:
        return redirect(url_for("proposal_list"))
    if p.get("decision"):
        return redirect(url_for("proposal_detail", pid=pid))

    note = request.form.get("note", "")
    crm_result = {}
    errors = []

    try:
        crm_result = _perform_crm_write(p)
        if crm_result.get("errors"):
            errors = crm_result["errors"]
    except Exception as e:
        errors.append(str(e))

    if errors:
        # Still record as approved, but flag errors
        crm_result["errors"] = errors

    database.record_decision(pid, "approved", note=note, crm_result=crm_result)

    if errors:
        from flask import flash
        flash(f"Approved, but CRM write had errors: {'; '.join(errors)}", "err")
    else:
        actions = crm_result.get("actions", [])
        from flask import flash
        flash(f"Approved. {'; '.join(actions)}", "ok")

    return redirect(url_for("proposal_detail", pid=pid))


@app.route("/proposals/<pid>/reject", methods=["POST"])
def reject(pid):
    p = database.get_proposal(pid)
    if not p:
        return redirect(url_for("proposal_list"))
    if p.get("decision"):
        return redirect(url_for("proposal_detail", pid=pid))

    note = request.form.get("note", "")
    database.record_decision(pid, "rejected", note=note)

    from flask import flash
    flash("Proposal rejected.", "ok")
    return redirect(url_for("proposal_detail", pid=pid))


@app.route("/crm/<account_id>")
def crm_account(account_id):
    """Live fetch of a CRM account for quick comparison."""
    acc = crm.get_account(account_id)
    if not acc:
        return jsonify({"error": "not found"}), 404
    return jsonify(acc)


@app.route("/api/proposals")
def api_proposals():
    return jsonify(database.get_all_proposals())


if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    print(f"\n🏥  Bellhaven CRM Review App")
    print(f"   Open: http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=True)
