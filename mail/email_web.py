"""
Flask blueprint — Settings + Email Jobs UI.

Wire into dashboard.py with:
    from mail.email_web import email_bp
    app.register_blueprint(email_bp)
"""
from flask import Blueprint, redirect, render_template_string, request, url_for

from mail.email_jobs_db import (
    delete_email_job, get_email_job_for_rpa, list_email_jobs,
    mark_send_finished, upsert_email_job,
)
from mail.sender import SendError, send_for_rpa
from mail.settings_db import AGENT_KEYS, get_agent_config, is_agent_configured, set_setting

email_bp = Blueprint("email_bp", __name__)


_STYLE = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; background: #0d1017; color: #e6e9ef;
  font-family: -apple-system, Segoe UI, Roboto, sans-serif; }
.wrap { max-width: 1200px; margin: 0 auto; padding: 24px 24px 64px; }
nav { display: flex; gap: 12px; margin-bottom: 24px; flex-wrap: wrap; }
nav a { color: #8ab4ff; text-decoration: none; font-size: 14px; padding: 6px 12px;
  border-radius: 8px; background: #161b26; border: 1px solid #232a38; }
nav a.active { background: #232a38; color: #fff; }
h1 { font-size: 22px; margin: 0 0 4px; }
.sub { color: #8a94a6; font-size: 13px; margin-bottom: 24px; }
.panel { background: #161b26; border: 1px solid #232a38; border-radius: 12px;
  padding: 20px; margin-bottom: 20px; }
.panel h2 { font-size: 13px; margin: 0 0 14px; color: #b7c0d0;
  text-transform: uppercase; letter-spacing: .04em; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #232a38; vertical-align: top; }
th { color: #8a94a6; font-weight: 500; white-space: nowrap; }
tr:hover td { background: #1b2130; }
.ok { color: #4ec98a; } .err { color: #ef6a6a; }
.muted { color: #8a94a6; font-size: 13px; }
textarea, input[type=text], input[type=number], input[type=password], select {
  background: #0d1017; color: #e6e9ef; border: 1px solid #232a38;
  border-radius: 8px; padding: 10px; font-family: inherit; font-size: 13px; width: 100%; }
textarea { min-height: 120px; font-family: monospace; }
button { background: #232a38; color: #e6e9ef; border: 1px solid #3a4458;
  border-radius: 8px; padding: 8px 14px; cursor: pointer; }
.btn-run { background: #1a4d2e; border-color: #2d6b42; }
.btn-danger { background: #4d1a1a; border-color: #6b2d2d; }
.btn-sm { font-size: 12px; padding: 4px 10px; }
.form-row { margin-bottom: 14px; }
.form-row label { display: block; font-size: 12px; color: #8a94a6; margin-bottom: 4px; }
.flash { padding: 12px; border-radius: 8px; margin-bottom: 16px; }
.flash.ok { background: #1a3d2a; color: #4ec98a; }
.flash.err { background: #3d1a1a; color: #ef6a6a; }
.pill { font-size: 11px; padding: 2px 8px; border-radius: 999px;
  background: #232a38; color: #b7c0d0; }
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
@media (max-width: 720px) { .grid-2 { grid-template-columns: 1fr; } }
"""


def _shell(title: str, active: str, body: str, **ctx) -> str:
    template = f"""
<!doctype html><html><head><meta charset="utf-8">
<title>{{{{ title }}}} — Samsung Mail DB</title>
<style>{_STYLE}</style></head><body>
<div class="wrap">
  <nav>
    <a href="/">Overview</a>
    <a href="/jobs">Mail Jobs</a>
    <a href="/rpa">RPA Tools</a>
    <a href="/email-jobs" class="{{{{ 'active' if active=='email' else '' }}}}">Email Jobs</a>
    <a href="/settings" class="{{{{ 'active' if active=='settings' else '' }}}}">Settings</a>
    <a href="/data">Data Explorer</a>
  </nav>
  {body}
</div></body></html>
"""
    return render_template_string(template, title=title, active=active, **ctx)


def _flash(msg: str, ok: bool = True) -> str:
    if not msg:
        return ""
    cls = "ok" if ok else "err"
    return f'<div class="flash {cls}">{msg}</div>'


# ── Settings ──────────────────────────────────────────────────────────────

_AGENT_LABELS = {
    "agent_api_url": (
        "Agent API URL",
        "e.g. https://agent.sec.samsung.net/api/v1/run/&lt;flow-id&gt;?stream=false",
    ),
    "agent_api_key": (
        "API key",
        "Sent as the x-api-key header (starts with sk-)",
    ),
    "agent_mail_component_id": (
        "Mail component ID",
        "Langflow component id inside the flow — default: knox_portal_mail-1irUi",
    ),
}


@email_bp.route("/settings", methods=["GET", "POST"])
def settings_page():
    msg = ""
    ok = True
    if request.method == "POST":
        try:
            for k in AGENT_KEYS:
                set_setting(k, request.form.get(k, ""))
            msg = "Saved."
        except Exception as e:
            ok = False
            msg = f"Failed to save: {e}"

    cfg = get_agent_config()
    rows = []
    for key in AGENT_KEYS:
        label, hint = _AGENT_LABELS[key]
        is_secret = "key" in key
        input_type = "password" if is_secret else "text"
        rows.append(f"""
        <div class="form-row">
          <label for="{key}">{label}</label>
          <input type="{input_type}" id="{key}" name="{key}"
                 value="{cfg.get(key, '') or ''}" autocomplete="off">
          <div class="muted" style="margin-top:4px">{hint}</div>
        </div>
        """)

    status = "Configured ✓" if is_agent_configured() else "Not configured yet."
    status_cls = "ok" if is_agent_configured() else "err"

    body = f"""
    <h1>Email API settings</h1>
    <div class="sub">Credentials for the Samsung Agent API used by Email Jobs.</div>
    {_flash(msg, ok)}
    <div class="panel">
      <h2>Agent (mail) API</h2>
      <p class="{status_cls}">{status}</p>
      <form method="post">
        {''.join(rows)}
        <button type="submit" class="btn-run">Save settings</button>
      </form>
    </div>
    """
    return _shell("Settings", "settings", body)


# ── Email jobs ────────────────────────────────────────────────────────────

def _list_rpa_ids() -> list[dict]:
    try:
        from rpa.jobs_db import list_rpa_jobs
        return list_rpa_jobs()
    except Exception:
        return []


@email_bp.route("/email-jobs")
def email_jobs_page():
    jobs = list_email_jobs()
    msg = request.args.get("msg", "")
    ok = request.args.get("err", "0") != "1"

    if not jobs:
        table_html = '<p class="muted">No email jobs yet. Add one below.</p>'
    else:
        rows_html = []
        for j in jobs:
            enabled_pill = ('<span class="pill ok">on</span>'
                           if j["enabled"] else '<span class="pill">off</span>')
            last_status = j.get("last_status") or "—"
            last_status_cls = "ok" if last_status == "ok" else ("err" if last_status == "error" else "muted")
            last_when = j["last_sent_at"].strftime("%Y-%m-%d %H:%M") if j.get("last_sent_at") else "—"
            rows_html.append(f"""
            <tr>
              <td><b>{j['rpa_id']}</b> {enabled_pill}</td>
              <td>{j['to_emails']}<br><span class="muted">cc: {j['cc_emails'] or '—'}</span></td>
              <td>{j['subject'] or '—'}</td>
              <td>{j['attach_folder'] or '<span class="muted">auto (RPA folder)</span>'}<br>
                  <span class="muted">latest {j['attach_count']}</span></td>
              <td><span class="{last_status_cls}">{last_status}</span><br>
                  <span class="muted">{last_when}</span></td>
              <td>
                <form method="post" action="/email-jobs/{j['rpa_id']}/send-now" style="display:inline">
                  <button type="submit" class="btn-run btn-sm">Send now</button>
                </form>
                <a href="/email-jobs/{j['rpa_id']}/edit"><button class="btn-sm">Edit</button></a>
                <form method="post" action="/email-jobs/{j['rpa_id']}/delete" style="display:inline"
                      onsubmit="return confirm('Delete email job for {j['rpa_id']}?');">
                  <button type="submit" class="btn-danger btn-sm">Delete</button>
                </form>
              </td>
            </tr>
            """)
        table_html = f"""
        <table>
          <thead><tr>
            <th>RPA</th><th>Recipients</th><th>Subject</th>
            <th>Attach folder</th><th>Last send</th><th></th>
          </tr></thead>
          <tbody>{''.join(rows_html)}</tbody>
        </table>
        """

    warn = ""
    if not is_agent_configured():
        warn = _flash(
            'Agent API is not configured. <a href="/settings" style="color:inherit;text-decoration:underline">Open Settings</a> to set credentials.',
            ok=False,
        )

    body = f"""
    <h1>Email jobs</h1>
    <div class="sub">Send an email with attachments — manually, or from any RPA script via
      <code>send_for_rpa("rpa_id")</code>.</div>
    {warn}
    {_flash(msg, ok)}
    <div class="panel">
      <h2>Configured jobs</h2>
      {table_html}
      <div style="margin-top:14px">
        <a href="/email-jobs/new"><button class="btn-run">+ New email job</button></a>
      </div>
    </div>
    """
    return _shell("Email Jobs", "email", body)


def _rpa_selector(selected: str = "") -> str:
    rpa_jobs = _list_rpa_ids()
    if not rpa_jobs:
        return f'<input type="text" name="rpa_id" value="{selected}" placeholder="rpa_id" required>'
    opts = ['<option value="">-- select an RPA --</option>']
    for r in rpa_jobs:
        rid = r.get("rpa_id", "")
        sel = " selected" if rid == selected else ""
        opts.append(f'<option value="{rid}"{sel}>{rid} — {r.get("name", "")}</option>')
    return f'<select name="rpa_id" required>{"".join(opts)}</select>'


def _render_form(job=None, is_edit=False) -> str:
    j = job or {}
    rpa_id = j.get("rpa_id", "")
    rpa_input = (f'<input type="text" name="rpa_id" value="{rpa_id}" readonly>'
                 if is_edit else _rpa_selector(rpa_id))
    checked = "checked" if j.get("enabled", True) else ""
    return f"""
    <div class="form-row">
      <label>Linked RPA job</label>
      {rpa_input}
    </div>
    <div class="grid-2">
      <div class="form-row">
        <label>To (comma or semicolon separated)</label>
        <input type="text" name="to_emails" value="{j.get('to_emails', '')}" required>
      </div>
      <div class="form-row">
        <label>Cc (optional)</label>
        <input type="text" name="cc_emails" value="{j.get('cc_emails', '')}">
      </div>
    </div>
    <div class="form-row">
      <label>Subject</label>
      <input type="text" name="subject" value="{j.get('subject', '')}">
    </div>
    <div class="form-row">
      <label>Body (plain text or HTML — detected automatically)</label>
      <textarea name="body">{j.get('body', '')}</textarea>
    </div>
    <div class="grid-2">
      <div class="form-row">
        <label>Attach folder (blank = auto-pick from RPA download folder)</label>
        <input type="text" name="attach_folder" value="{j.get('attach_folder', '')}"
               placeholder="C:/Users/you/Desktop/Reports">
      </div>
      <div class="form-row">
        <label>How many latest files to attach</label>
        <input type="number" name="attach_count" min="1" value="{j.get('attach_count', 1)}">
      </div>
    </div>
    <div class="form-row">
      <label><input type="checkbox" name="enabled" {checked}> Enabled</label>
    </div>
    """


@email_bp.route("/email-jobs/new", methods=["GET", "POST"])
def email_job_new():
    msg = ""
    ok = True
    form = {}
    if request.method == "POST":
        try:
            form = request.form.to_dict()
            upsert_email_job(
                rpa_id=form.get("rpa_id", "").strip(),
                to_emails=form.get("to_emails", ""),
                cc_emails=form.get("cc_emails", ""),
                subject=form.get("subject", ""),
                body=form.get("body", ""),
                attach_folder=form.get("attach_folder", ""),
                attach_count=int(form.get("attach_count") or 1),
                enabled=("enabled" in form),
            )
            return redirect(url_for("email_bp.email_jobs_page",
                                    msg=f"Saved email job for {form.get('rpa_id')}"))
        except Exception as e:
            ok = False
            msg = f"Failed to save: {e}"

    body = f"""
    <h1>New email job</h1>
    <div class="sub">Link a recipient list + template to an RPA. Trigger via the dashboard
      or from Python: <code>from mail.sender import send_for_rpa</code>.</div>
    {_flash(msg, ok)}
    <div class="panel">
      <form method="post">
        {_render_form(form)}
        <button type="submit" class="btn-run">Save</button>
        <a href="/email-jobs"><button type="button">Cancel</button></a>
      </form>
    </div>
    """
    return _shell("New email job", "email", body)


@email_bp.route("/email-jobs/<rpa_id>/edit", methods=["GET", "POST"])
def email_job_edit(rpa_id):
    job = get_email_job_for_rpa(rpa_id)
    if not job:
        return redirect(url_for("email_bp.email_jobs_page",
                                msg=f"No email job for {rpa_id}", err="1"))
    msg = ""
    ok = True
    if request.method == "POST":
        try:
            form = request.form
            upsert_email_job(
                rpa_id=rpa_id,
                to_emails=form.get("to_emails", ""),
                cc_emails=form.get("cc_emails", ""),
                subject=form.get("subject", ""),
                body=form.get("body", ""),
                attach_folder=form.get("attach_folder", ""),
                attach_count=int(form.get("attach_count") or 1),
                enabled=("enabled" in form),
            )
            return redirect(url_for("email_bp.email_jobs_page",
                                    msg=f"Updated {rpa_id}"))
        except Exception as e:
            ok = False
            msg = f"Failed to save: {e}"
            job = {**job, **request.form.to_dict()}

    body = f"""
    <h1>Edit email job — <code>{rpa_id}</code></h1>
    {_flash(msg, ok)}
    <div class="panel">
      <form method="post">
        {_render_form(job, is_edit=True)}
        <button type="submit" class="btn-run">Save</button>
        <a href="/email-jobs"><button type="button">Cancel</button></a>
      </form>
    </div>
    """
    return _shell("Edit email job", "email", body)


@email_bp.route("/email-jobs/<rpa_id>/send-now", methods=["POST"])
def email_job_send_now(rpa_id):
    try:
        send_for_rpa(rpa_id)
        mark_send_finished(rpa_id, "ok", "Sent via dashboard.")
        return redirect(url_for("email_bp.email_jobs_page",
                                msg=f"Sent email for {rpa_id}"))
    except SendError as e:
        mark_send_finished(rpa_id, "error", str(e))
        return redirect(url_for("email_bp.email_jobs_page",
                                msg=f"Send failed for {rpa_id}: {e}", err="1"))
    except Exception as e:
        mark_send_finished(rpa_id, "error", str(e))
        return redirect(url_for("email_bp.email_jobs_page",
                                msg=f"Unexpected error for {rpa_id}: {e}", err="1"))


@email_bp.route("/email-jobs/<rpa_id>/delete", methods=["POST"])
def email_job_delete(rpa_id):
    delete_email_job(rpa_id)
    return redirect(url_for("email_bp.email_jobs_page",
                            msg=f"Deleted email job for {rpa_id}"))
