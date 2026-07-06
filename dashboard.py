"""
Mail reader + database dashboard — LAN-accessible.

Run:  python dashboard.py   (or double-click run_dashboard.bat)
Open:  http://<this-pc-ip>:5000  from any machine on the network.
"""
import os
import re
import socket

os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

from datetime import datetime

import pandas as pd
from flask import Flask, jsonify, redirect, render_template_string, request, url_for

import config
from db import init_db
from mail.jobs_db import (
    add_job, delete_job, get_job, list_jobs, seed_from_config, update_job,
)
from parse_to_db import ingest_download, parse_file
from sqlalchemy import create_engine, inspect, text

app = Flask(__name__)
init_db()
engine = create_engine(
    config.DB_URL,
    connect_args={"check_same_thread": False},
)

FORBIDDEN_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|ATTACH|DETACH|PRAGMA|TRUNCATE)\b",
    re.IGNORECASE,
)

BASE_STYLE = """
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
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 14px; margin-bottom: 24px; }
.card { background: #161b26; border: 1px solid #232a38; border-radius: 12px; padding: 18px; }
.card .label { color: #8a94a6; font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }
.card .value { font-size: 24px; font-weight: 600; margin-top: 6px; }
.panel { background: #161b26; border: 1px solid #232a38; border-radius: 12px;
  padding: 20px; margin-bottom: 20px; }
.panel h2 { font-size: 13px; margin: 0 0 14px; color: #b7c0d0;
  text-transform: uppercase; letter-spacing: .04em; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #232a38; }
th { color: #8a94a6; font-weight: 500; white-space: nowrap; }
tr:hover td { background: #1b2130; }
.ok { color: #4ec98a; } .err { color: #ef6a6a; }
.muted { color: #8a94a6; font-size: 13px; }
.scroll { overflow-x: auto; }
.pill { font-size: 11px; padding: 2px 8px; border-radius: 999px;
  background: #232a38; color: #b7c0d0; }
textarea { width: 100%; min-height: 100px; background: #0d1017; color: #e6e9ef;
  border: 1px solid #232a38; border-radius: 8px; padding: 12px; font-family: monospace; }
button, select, input[type=text], input[type=number] { background: #232a38; color: #e6e9ef; border: 1px solid #3a4458;
  border-radius: 8px; padding: 8px 14px; cursor: pointer; }
input[type=text], input[type=number] { cursor: text; width: 100%; max-width: 400px; }
.btn-sm { font-size: 12px; padding: 4px 10px; }
.btn-run { background: #1a4d2e; border-color: #2d6b42; }
.btn-danger { background: #4d1a1a; border-color: #6b2d2d; }
.form-row { margin-bottom: 14px; }
.form-row label { display: block; font-size: 12px; color: #8a94a6; margin-bottom: 4px; }
.flash { padding: 12px; border-radius: 8px; margin-bottom: 16px; }
.flash.ok { background: #1a3d2a; color: #4ec98a; }
.flash.err { background: #3d1a1a; color: #ef6a6a; }
"""


def _list_excel_files():
    if not os.path.isdir(config.DOWNLOAD_DIR):
        return []
    files = [
        f for f in os.listdir(config.DOWNLOAD_DIR)
        if f.lower().endswith((".xlsx", ".xls"))
    ]
    files.sort(
        key=lambda f: os.path.getmtime(os.path.join(config.DOWNLOAD_DIR, f)),
        reverse=True,
    )
    return files


def _local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _tables():
    try:
        return sorted(inspect(engine).get_table_names())
    except Exception:
        return []


def _read_sql(query, params=None):
    try:
        return pd.read_sql(text(query), engine, params=params or {}), None
    except Exception as e:
        return pd.DataFrame(), str(e)


def _safe_select(sql):
    sql = sql.strip().rstrip(";")
    if not sql.upper().startswith("SELECT"):
        raise ValueError("Only SELECT queries are allowed.")
    if FORBIDDEN_SQL.search(sql):
        raise ValueError("Query contains forbidden keywords.")
    return sql


def _layout(title, active, body, **ctx):
    template = f"""
<!doctype html><html><head><meta charset="utf-8">
<title>{{{{ title }}}} — Samsung Mail DB</title>
<meta http-equiv="refresh" content="60">
<style>{BASE_STYLE}</style></head><body>
<div class="wrap">
  <nav>
    <a href="/" class="{{{{ 'active' if active=='home' else '' }}}}">Overview</a>
    <a href="/jobs" class="{{{{ 'active' if active=='jobs' else '' }}}}">Mail Jobs</a>
    <a href="/data" class="{{{{ 'active' if active=='data' else '' }}}}">Data Explorer</a>
    <a href="/query" class="{{{{ 'active' if active=='query' else '' }}}}">SQL Query</a>
    <a href="/api/docs" class="{{{{ 'active' if active=='api' else '' }}}}">API</a>
  </nav>
  {body}
</div></body></html>
"""
    return render_template_string(template, title=title, active=active, **ctx)


@app.route("/")
def index():
    total_rows = 0
    total_files = 0
    last_run = "—"
    last_status = None
    log_rows = []
    table_stats = []

    log_df, _ = _read_sql(
        "SELECT loaded_at, source_file, row_count, status, batch_id, filter_id, message "
        "FROM ingestion_log ORDER BY id DESC LIMIT 50"
    )
    if not log_df.empty:
        log_rows = log_df.to_dict("records")
        total_files = int((log_df["status"] == "success").sum())
        last = log_df.iloc[0]
        last_run = str(last["loaded_at"])
        last_status = last["status"]

    seed_from_config()
    jobs = list_jobs()
    for j in jobs:
        tbl = j["target_table"]
        if tbl in _tables():
            cnt_df, _ = _read_sql(f"SELECT COUNT(*) AS c FROM {tbl}")
            rows = int(cnt_df.iloc[0]["c"]) if not cnt_df.empty else 0
            total_rows += rows
            table_stats.append({"id": j["job_id"], "table": tbl, "rows": rows})

    body = """
  <h1>Overview</h1>
  <div class="sub">LAN: http://{{ lan_ip }}:{{ port }} · Refreshes every 60s · {{ now }}</div>
  <div class="cards">
    <div class="card"><div class="label">Total rows</div><div class="value">{{ total_rows }}</div></div>
    <div class="card"><div class="label">Files ingested</div><div class="value">{{ total_files }}</div></div>
    <div class="card"><div class="label">Last ingestion</div><div class="value" style="font-size:15px">{{ last_run }}</div></div>
    <div class="card"><div class="label">Active jobs</div><div class="value" style="font-size:18px">{{ job_count }}</div></div>
  </div>
  <div class="panel"><h2>Tables by mail filter</h2>
    <table><tr><th>Filter</th><th>Table</th><th>Rows</th></tr>
    {% for t in table_stats %}<tr><td>{{ t.id }}</td><td><span class="pill">{{ t.table }}</span></td><td>{{ t.rows }}</td></tr>{% endfor %}
    </table>
  </div>
  <div class="panel"><h2>Recent ingestions</h2><div class="scroll">
    {% if log_rows %}<table>
    <tr><th>Time</th><th>Filter</th><th>File</th><th>Rows</th><th>Status</th></tr>
    {% for r in log_rows %}<tr>
      <td>{{ r.loaded_at }}</td><td>{{ r.filter_id or '—' }}</td><td>{{ r.source_file }}</td>
      <td>{{ r.row_count }}</td><td class="{{ 'ok' if r.status=='success' else 'err' }}">{{ r.status }}</td>
    </tr>{% endfor %}</table>{% else %}<p class="muted">No data yet — start the mail monitor.</p>{% endif %}
  </div></div>
"""
    return _layout(
        "Overview", "home", body,
        lan_ip=_local_ip(), port=config.DASHBOARD_PORT,
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        total_rows=total_rows, total_files=total_files, last_run=last_run,
        job_count=len(jobs), table_stats=table_stats, log_rows=log_rows,
    )


@app.route("/jobs")
def jobs_list():
    seed_from_config()
    jobs = list_jobs()
    msg = request.args.get("msg")
    err = request.args.get("err")
    runs_df, _ = _read_sql(
        "SELECT checked_at, downloads, errors, status, error_detail "
        "FROM monitor_runs ORDER BY id DESC LIMIT 20"
    )
    runs = runs_df.to_dict("records") if not runs_df.empty else []

    body = """
  <h1>Mail Jobs</h1>
  <div class="sub">Cron jobs run automatically when due — nothing runs on startup. Scheduler checks every {{ tick }}s.</div>
  {% if msg %}<div class="flash ok">{{ msg }}</div>{% endif %}
  {% if err %}<div class="flash err">{{ err }}</div>{% endif %}
  <div style="margin-bottom:16px">
    <a href="/jobs/new"><button>+ New mail job</button></a>
    <a href="/jobs/parse"><button>Manual parse to SQL</button></a>
  </div>
  <div class="panel"><h2>Scheduled jobs</h2><div class="scroll">
    {% if jobs %}<table>
    <tr><th>Name</th><th>Mailbox</th><th>Subject</th><th>Table</th><th>Mode</th><th>Every</th><th>Next run</th><th>Last</th><th>Status</th><th>Actions</th></tr>
    {% for j in jobs %}<tr>
      <td><span class="pill">{{ j.job_id }}</span><br><span class="muted">{{ j.name }}</span></td>
      <td>{{ j.mailbox }}</td><td><code>{{ j.subject_pattern }}</code></td>
      <td>{{ j.target_table }}</td>
      <td><span class="pill">{{ j.ingest_mode }}</span></td>
      <td>{{ j.interval_hours }}h</td>
      <td>{{ j.next_run or '—' }}</td>
      <td>{{ j.last_run or '—' }}</td>
      <td class="{{ 'ok' if j.last_status=='ok' else 'err' if j.last_status else '' }}">{{ j.last_status or '—' }}</td>
      <td style="white-space:nowrap">
        <form method="post" action="/jobs/{{ j.job_id }}/run" style="display:inline"><button class="btn-sm btn-run">Run now</button></form>
        <a href="/jobs/{{ j.job_id }}/edit"><button type="button" class="btn-sm">Edit</button></a>
        <form method="post" action="/jobs/{{ j.job_id }}/toggle" style="display:inline"><button class="btn-sm">{{ 'Disable' if j.enabled else 'Enable' }}</button></form>
      </td>
    </tr>{% endfor %}</table>
    {% else %}<p class="muted">No jobs yet — click <b>New mail job</b> to add one.</p>{% endif %}
  </div></div>
  <div class="panel"><h2>Recording a new mail type</h2>
    <p class="muted">Use Playwright codegen to find mailbox/subject selectors, then add a job here:</p>
    <pre style="background:#0d1017;padding:12px;border-radius:8px;font-size:12px">python -m playwright codegen --channel chrome http://w1.samsung.net</pre>
    <p class="muted">Note the <b>mailbox</b> button name and email <b>subject</b> text, then create a job with those values.</p>
  </div>
  <div class="panel"><h2>Recent runs</h2><div class="scroll">
    {% if runs %}<table>
    <tr><th>Time</th><th>Downloads</th><th>Errors</th><th>Status</th><th>Detail</th></tr>
    {% for r in runs %}<tr>
      <td>{{ r.checked_at }}</td><td>{{ r.downloads }}</td><td>{{ r.errors }}</td>
      <td class="{{ 'ok' if r.status=='ok' else 'err' }}">{{ r.status }}</td>
      <td class="muted">{{ r.error_detail or '' }}</td>
    </tr>{% endfor %}</table>{% else %}<p class="muted">No runs yet.</p>{% endif %}
  </div></div>
"""
    return _layout("Mail Jobs", "jobs", body, jobs=jobs, runs=runs, tick=config.SCHEDULER_TICK_SECONDS, msg=msg, err=err)


@app.route("/jobs/new", methods=["GET", "POST"])
def jobs_new():
    error = None
    if request.method == "POST":
        try:
            add_job(
                job_id=request.form["job_id"].strip().lower(),
                name=request.form["name"].strip(),
                mailbox=request.form["mailbox"].strip(),
                subject_pattern=request.form["subject_pattern"].strip(),
                target_table=request.form["target_table"].strip().lower(),
                interval_hours=int(request.form.get("interval_hours", 2)),
                enabled=request.form.get("enabled") == "on",
                ingest_mode=request.form.get("ingest_mode", "replace"),
            )
            return redirect(url_for("jobs_list", msg=f"Job '{request.form['job_id']}' created"))
        except Exception as e:
            error = str(e)

    body = """
  <h1>New mail job</h1>
  {% if error %}<div class="flash err">{{ error }}</div>{% endif %}
  <form method="post" class="panel">
    <div class="form-row"><label>Job ID (slug, e.g. order_extract)</label>
      <input type="text" name="job_id" required pattern="[a-z][a-z0-9_]+"></div>
    <div class="form-row"><label>Display name</label>
      <input type="text" name="name" required placeholder="Order Extract"></div>
    <div class="form-row"><label>W1 mailbox button name</label>
      <input type="text" name="mailbox" required value="Extract"></div>
    <div class="form-row"><label>Subject pattern (regex)</label>
      <input type="text" name="subject_pattern" required placeholder="Order Extract - AE/GCC"></div>
    <div class="form-row"><label>SQL table name</label>
      <input type="text" name="target_table" required value="orders"></div>
    <div class="form-row"><label>Check every (hours)</label>
      <input type="number" name="interval_hours" value="2" min="1" required></div>
    <div class="form-row"><label>When a newer file arrives</label>
      <select name="ingest_mode">
        <option value="replace" selected>Replace — swap old rows for this job (recommended)</option>
        <option value="append">Append — keep all historical rows</option>
      </select></div>
    <div class="form-row"><label><input type="checkbox" name="enabled" checked> Enabled</label></div>
    <button type="submit">Create job</button>
    <a href="/jobs"><button type="button">Cancel</button></a>
  </form>
"""
    return _layout("New Job", "jobs", body, error=error)


@app.route("/jobs/parse", methods=["GET", "POST"])
def jobs_parse():
    seed_from_config()
    jobs = list_jobs()
    files = _list_excel_files()
    msg = request.args.get("msg")
    error = request.args.get("err")

    if request.method == "POST":
        filename = request.form.get("filename", "")
        table = request.form.get("target_table", "")
        filter_id = request.form.get("filter_id", "manual")
        path = os.path.join(config.DOWNLOAD_DIR, filename)
        try:
            if not os.path.isfile(path):
                raise FileNotFoundError(f"File not found: {filename}")
            force = request.form.get("force") == "on"
            ingest_download(
                path, table=table, filter_id=filter_id,
                ingest_mode=request.form.get("ingest_mode", "replace"),
                force=force,
            )
            return redirect(url_for("jobs_parse", msg=f"Parsed {filename} → {table}"))
        except Exception as e:
            error = str(e)

    body = """
  <h1>Manual parse to SQL</h1>
  <div class="sub">Decrypt + load an Excel file from the download folder without running a mail check.</div>
  {% if msg %}<div class="flash ok">{{ msg }}</div>{% endif %}
  {% if error %}<div class="flash err">{{ error }}</div>{% endif %}
  <form method="post" class="panel">
    <div class="form-row"><label>Excel file in {{ download_dir }}</label>
      <select name="filename" required>
        {% for f in files %}<option value="{{ f }}">{{ f }}</option>{% endfor %}
      </select>
      {% if not files %}<p class="muted">No Excel files found — run a mail job first.</p>{% endif %}
    </div>
    <div class="form-row"><label>Target SQL table</label>
      <input type="text" name="target_table" list="tables" required value="{{ default_table }}">
      <datalist id="tables">{% for j in jobs %}<option value="{{ j.target_table }}">{% endfor %}</datalist>
    </div>
    <div class="form-row"><label>Filter / job ID (for logging)</label>
      <select name="filter_id">
        <option value="manual">manual</option>
        {% for j in jobs %}<option value="{{ j.job_id }}">{{ j.job_id }}</option>{% endfor %}
      </select>
    </div>
    <div class="form-row"><label>When loading</label>
      <select name="ingest_mode">
        <option value="replace">Replace old rows for this job</option>
        <option value="append">Append rows</option>
      </select></div>
    <div class="form-row"><label><input type="checkbox" name="force"> Force re-parse (even if file unchanged)</label></div>
    <button type="submit" {{ 'disabled' if not files else '' }}>Parse to SQL</button>
    <a href="/jobs"><button type="button">Back</button></a>
  </form>
"""
    default_table = jobs[0]["target_table"] if jobs else "orders"
    return _layout(
        "Manual Parse", "jobs", body,
        files=files, jobs=jobs, download_dir=config.DOWNLOAD_DIR,
        default_table=default_table, msg=msg, error=error,
    )


@app.route("/jobs/<job_id>/edit", methods=["GET", "POST"])
def jobs_edit(job_id):
    job = get_job(job_id)
    if not job:
        return redirect(url_for("jobs_list", err="Job not found"))
    error = None
    if request.method == "POST":
        try:
            update_job(
                job_id,
                name=request.form["name"].strip(),
                mailbox=request.form["mailbox"].strip(),
                subject_pattern=request.form["subject_pattern"].strip(),
                target_table=request.form["target_table"].strip().lower(),
                interval_hours=int(request.form.get("interval_hours", 2)),
                enabled=request.form.get("enabled") == "on",
                ingest_mode=request.form.get("ingest_mode", "replace"),
            )
            return redirect(url_for("jobs_list", msg=f"Job '{job_id}' updated"))
        except Exception as e:
            error = str(e)

    body = """
  <h1>Edit job: {{ job.job_id }}</h1>
  {% if error %}<div class="flash err">{{ error }}</div>{% endif %}
  <form method="post" class="panel">
    <div class="form-row"><label>Display name</label>
      <input type="text" name="name" value="{{ job.name }}" required></div>
    <div class="form-row"><label>Mailbox</label>
      <input type="text" name="mailbox" value="{{ job.mailbox }}" required></div>
    <div class="form-row"><label>Subject pattern</label>
      <input type="text" name="subject_pattern" value="{{ job.subject_pattern }}" required></div>
    <div class="form-row"><label>SQL table</label>
      <input type="text" name="target_table" value="{{ job.target_table }}" required></div>
    <div class="form-row"><label>Every (hours)</label>
      <input type="number" name="interval_hours" value="{{ job.interval_hours }}" min="1" required></div>
    <div class="form-row"><label>When a newer file arrives</label>
      <select name="ingest_mode">
        <option value="replace" {{ 'selected' if job.ingest_mode=='replace' else '' }}>Replace old rows</option>
        <option value="append" {{ 'selected' if job.ingest_mode=='append' else '' }}>Append rows</option>
      </select></div>
    <div class="form-row"><label><input type="checkbox" name="enabled" {{ 'checked' if job.enabled else '' }}> Enabled</label></div>
    <button type="submit">Save</button>
    <a href="/jobs"><button type="button">Cancel</button></a>
  </form>
  <form method="post" action="/jobs/{{ job.job_id }}/delete" class="panel" onsubmit="return confirm('Delete this job?')">
    <button type="submit" class="btn-danger">Delete job</button>
  </form>
"""
    return _layout("Edit Job", "jobs", body, job=job, error=error)


@app.route("/jobs/<job_id>/run", methods=["POST"])
def jobs_run(job_id):
    import threading
    from mail.cron import run_job

    def _bg():
        try:
            run_job(job_id)
        except Exception as e:
            print(f"Manual run failed: {e}")

    if not get_job(job_id):
        return redirect(url_for("jobs_list", err="Job not found"))
    threading.Thread(target=_bg, daemon=True, name=f"run-{job_id}").start()
    return redirect(url_for("jobs_list", msg=f"Started '{job_id}' — check back in a minute"))


@app.route("/jobs/<job_id>/toggle", methods=["POST"])
def jobs_toggle(job_id):
    job = get_job(job_id)
    if not job:
        return redirect(url_for("jobs_list", err="Job not found"))
    update_job(job_id, enabled=not job["enabled"])
    state = "disabled" if job["enabled"] else "enabled"
    return redirect(url_for("jobs_list", msg=f"Job '{job_id}' {state}"))


@app.route("/jobs/<job_id>/delete", methods=["POST"])
def jobs_delete(job_id):
    delete_job(job_id)
    return redirect(url_for("jobs_list", msg=f"Job '{job_id}' deleted"))


@app.route("/mail")
def mail_redirect():
    return redirect(url_for("jobs_list"))


@app.route("/data")
def data_explorer():
    table = request.args.get("table", config.ORDERS_TABLE)
    tables = _tables()
    if table not in tables:
        table = tables[0] if tables else config.ORDERS_TABLE

    preview = pd.DataFrame()
    total = 0
    if table in tables:
        cnt, _ = _read_sql(f"SELECT COUNT(*) AS c FROM {table}")
        total = int(cnt.iloc[0]["c"]) if not cnt.empty else 0
        preview, _ = _read_sql(f"SELECT * FROM {table} LIMIT 100")

    cols = list(preview.columns)[:15] if not preview.empty else []
    rows = preview[cols].to_dict("records") if cols else []

    body = """
  <h1>Data Explorer</h1>
  <div class="sub">{{ total }} rows in selected table</div>
  <form method="get" class="panel" style="display:flex;gap:12px;align-items:center">
    <label>Table</label>
    <select name="table" onchange="this.form.submit()">
      {% for t in tables %}<option value="{{ t }}" {{ 'selected' if t==table else '' }}>{{ t }}</option>{% endfor %}
    </select>
  </form>
  <div class="panel"><h2>Latest 100 rows</h2><div class="scroll">
    {% if cols %}<table><tr>{% for c in cols %}<th>{{ c }}</th>{% endfor %}</tr>
    {% for row in rows %}<tr>{% for c in cols %}<td>{{ row[c] }}</td>{% endfor %}</tr>{% endfor %}
    </table>{% else %}<p class="muted">No rows in this table yet.</p>{% endif %}
  </div></div>
"""
    return _layout(
        "Data Explorer", "data", body,
        table=table, tables=tables, total=total, cols=cols, rows=rows,
    )


@app.route("/query", methods=["GET", "POST"])
def sql_query():
    sql = request.form.get("sql", "SELECT * FROM ingestion_log ORDER BY id DESC LIMIT 20")
    error = None
    result_cols, result_rows = [], []

    if request.method == "POST":
        try:
            safe = _safe_select(sql)
            df, err = _read_sql(safe)
            if err:
                error = err
            elif not df.empty:
                result_cols = list(df.columns)
                result_rows = df.head(500).to_dict("records")
            else:
                result_cols, result_rows = [], []
        except Exception as e:
            error = str(e)

    body = """
  <h1>SQL Query</h1>
  <div class="sub">Read-only SELECT · Max 500 rows · For LAN colleagues</div>
  <form method="post" class="panel">
    <textarea name="sql">{{ sql }}</textarea><br><br>
    <button type="submit">Run query</button>
  </form>
  {% if error %}<div class="panel err">{{ error }}</div>{% endif %}
  <div class="panel"><h2>Results ({{ result_rows|length }} rows)</h2><div class="scroll">
    {% if result_cols %}<table><tr>{% for c in result_cols %}<th>{{ c }}</th>{% endfor %}</tr>
    {% for row in result_rows %}<tr>{% for c in result_cols %}<td>{{ row[c] }}</td>{% endfor %}</tr>{% endfor %}
    </table>{% else %}<p class="muted">No results.</p>{% endif %}
  </div></div>
"""
    return _layout(
        "SQL Query", "query", body,
        sql=sql, error=error, result_cols=result_cols, result_rows=result_rows,
    )


# ── JSON API for network integrations ──────────────────────────

def _check_api_key():
    if not config.API_KEY:
        return None
    provided = request.headers.get("X-API-Key") or request.args.get("api_key")
    if provided != config.API_KEY:
        return jsonify({"error": "unauthorized — set X-API-Key header or ?api_key="}), 401
    return None


def _api_guard():
    err = _check_api_key()
    if err:
        return err


@app.route("/api/docs")
def api_docs():
    ip = _local_ip()
    port = config.DASHBOARD_PORT
    base = f"http://{ip}:{port}"
    key_note = (
        "All requests need header <code>X-API-Key: YOUR_KEY</code> "
        "(set <code>API_KEY</code> in config.py or env)."
        if config.API_KEY else
        "No API key configured — open access on your LAN."
    )
    body = f"""
  <h1>API docs</h1>
  <div class="sub">Base URL: <code>{base}</code> · {key_note}</div>
  <div class="panel"><h2>Endpoints</h2>
  <table>
    <tr><th>Method</th><th>URL</th><th>Description</th></tr>
    <tr><td>GET</td><td><code>/api/health</code></td><td>Server status</td></tr>
    <tr><td>GET</td><td><code>/api/tables</code></td><td>List SQL tables</td></tr>
    <tr><td>GET</td><td><code>/api/jobs</code></td><td>List mail cron jobs</td></tr>
    <tr><td>GET</td><td><code>/api/table/orders?limit=100&amp;offset=0</code></td><td>Rows from a table (paginated)</td></tr>
    <tr><td>GET</td><td><code>/api/table/orders/latest?filter_id=order_extract</code></td><td>Latest snapshot for one job</td></tr>
    <tr><td>GET</td><td><code>/api/ingestions</code></td><td>Ingestion history</td></tr>
    <tr><td>POST</td><td><code>/api/query</code></td><td>Read-only SQL (JSON body)</td></tr>
  </table></div>
  <div class="panel"><h2>Examples (curl)</h2>
  <pre style="background:#0d1017;padding:12px;border-radius:8px;font-size:12px;overflow-x:auto">
# List tables
curl "{base}/api/tables"

# Get 500 order rows
curl "{base}/api/table/orders?limit=500"

# Latest data for one mail job
curl "{base}/api/table/orders/latest?filter_id=order_extract"

# Read-only SQL
curl -X POST "{base}/api/query" -H "Content-Type: application/json" \\
  -d '{{"sql": "SELECT * FROM orders LIMIT 10"}}'

# Python
import requests
r = requests.get("{base}/api/table/orders", params={{"limit": 100}})
print(r.json()["rows"])
</pre></div>
  <div class="panel"><h2>From Excel / Power Query</h2>
  <p class="muted">Data → Get Data → From Web → paste:</p>
  <pre style="background:#0d1017;padding:12px;border-radius:8px">{base}/api/table/orders?limit=10000</pre>
  </div>
"""
    return _layout("API", "api", body)


@app.route("/api/health")
def api_health():
    if err := _api_guard():
        return err
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})


@app.route("/api/tables")
def api_tables():
    if err := _api_guard():
        return err
    return jsonify({"tables": _tables()})


@app.route("/api/jobs")
def api_jobs():
    if err := _api_guard():
        return err
    seed_from_config()
    jobs = list_jobs()
    return jsonify({"jobs": jobs})


@app.route("/api/table/<name>")
def api_table(name):
    if err := _api_guard():
        return err
    if name not in _tables():
        return jsonify({"error": "table not found"}), 404
    limit = min(int(request.args.get("limit", 100)), 10000)
    offset = int(request.args.get("offset", 0))
    filter_id = request.args.get("filter_id")
    if filter_id:
        df, err = _read_sql(
            f"SELECT * FROM {name} WHERE filter_id = :fid LIMIT :lim OFFSET :off",
            {"fid": filter_id, "lim": limit, "off": offset},
        )
    else:
        df, err = _read_sql(
            f"SELECT * FROM {name} LIMIT :lim OFFSET :off",
            {"lim": limit, "off": offset},
        )
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"table": name, "rows": df.to_dict("records"), "count": len(df)})


@app.route("/api/table/<name>/latest")
def api_table_latest(name):
    """Latest snapshot — rows from the most recent successful ingest for a filter."""
    if err := _api_guard():
        return err
    if name not in _tables():
        return jsonify({"error": "table not found"}), 404
    filter_id = request.args.get("filter_id", "")
    if filter_id:
        df, err = _read_sql(
            f"SELECT * FROM {name} WHERE filter_id = :fid",
            {"fid": filter_id},
        )
    else:
        batch_df, err = _read_sql(
            f"SELECT batch_id FROM {name} ORDER BY loaded_at DESC LIMIT 1"
        )
        if err or batch_df.empty:
            return jsonify({"table": name, "rows": [], "count": 0})
        bid = batch_df.iloc[0]["batch_id"]
        df, err = _read_sql(
            f"SELECT * FROM {name} WHERE batch_id = :bid",
            {"bid": bid},
        )
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"table": name, "filter_id": filter_id or None,
                    "rows": df.to_dict("records"), "count": len(df)})


@app.route("/api/query", methods=["POST"])
def api_query():
    if err := _api_guard():
        return err
    data = request.get_json(silent=True) or {}
    sql = data.get("sql", "")
    try:
        safe = _safe_select(sql)
        df, err = _read_sql(safe)
        if err:
            return jsonify({"error": err}), 400
        return jsonify({"rows": df.head(5000).to_dict("records"), "count": len(df)})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/ingestions")
def api_ingestions():
    if err := _api_guard():
        return err
    df, _ = _read_sql("SELECT * FROM ingestion_log ORDER BY id DESC LIMIT 100")
    return jsonify({"ingestions": df.to_dict("records")})


if __name__ == "__main__":
    seed_from_config()
    if config.DASHBOARD_RUNS_SCHEDULER:
        from mail.cron import start_background
        start_background()
        print("Mail scheduler running in background (no instant run on startup).")
    ip = _local_ip()
    print(f"Dashboard: http://{ip}:{config.DASHBOARD_PORT}  (LAN)")
    print(f"           http://127.0.0.1:{config.DASHBOARD_PORT}  (local)")
    print("Manage mail cron jobs at /jobs")
    app.run(host=config.DASHBOARD_HOST, port=config.DASHBOARD_PORT, debug=False, threaded=True)
