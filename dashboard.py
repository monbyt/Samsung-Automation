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
from rpa.jobs_db import (
    add_rpa_job, delete_rpa_job, get_rpa_job, list_rpa_jobs, rpa_by_mail_job,
    seed_from_config as seed_rpa, update_rpa_job,
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
    """Excel files across all job download folders on the Desktop."""
    entries = []
    try:
        seed_from_config()
        dirs = {j.get("download_dir") for j in list_jobs() if j.get("download_dir")}
    except Exception:
        dirs = set()
    dirs.add(config.DOWNLOAD_DIR)
    for folder in dirs:
        if not folder or not os.path.isdir(folder):
            continue
        label = os.path.basename(folder)
        for f in os.listdir(folder):
            if not f.lower().endswith((".xlsx", ".xls", ".zip")):
                continue
            path = os.path.join(folder, f)
            entries.append({"label": f"{label}/{f}", "path": path})
    entries.sort(key=lambda e: os.path.getmtime(e["path"]), reverse=True)
    return entries


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
    <a href="/rpa" class="{{{{ 'active' if active=='rpa' else '' }}}}">RPA Tools</a>
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
    seed_rpa()
    jobs = list_jobs()
    rpa_map = rpa_by_mail_job()
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
    <tr><th>Name</th><th>Mailbox</th><th>Subject</th><th>Table</th><th>Folder</th><th>RPA</th><th>Every</th><th>Next run</th><th>Last</th><th>Status</th><th>Actions</th></tr>
    {% for j in jobs %}<tr>
      <td><span class="pill">{{ j.job_id }}</span><br><span class="muted">{{ j.name }}</span></td>
      <td>{{ j.mailbox }}</td><td><code>{{ j.subject_pattern }}</code></td>
      <td>{{ j.target_table }}</td>
      <td><span class="pill">{{ j.download_folder or '—' }}</span></td>
      <td>{% if rpa_map.get(j.job_id) %}{% for n in rpa_map[j.job_id] %}<span class="pill">{{ n }}</span> {% endfor %}{% else %}<span class="muted">—</span>{% endif %}</td>
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
  <div class="panel"><h2>How a job runs</h2>
    <p class="muted" style="line-height:1.6">
      1. Scheduler checks if <b>next run</b> time has passed<br>
      2. Chrome opens W1 → clicks your <b>mailbox</b> button<br>
      3. Finds the newest email matching your <b>subject</b> regex<br>
      4. Downloads the Excel attachment<br>
      5. Decrypts via Excel COM → loads into your <b>SQL table</b><br>
      Use <b>Run now</b> to test without waiting for the schedule.
    </p>
  </div>
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
    return _layout("Mail Jobs", "jobs", body, jobs=jobs, runs=runs, rpa_map=rpa_map, tick=config.SCHEDULER_TICK_SECONDS, msg=msg, err=err)


@app.route("/jobs/new", methods=["GET", "POST"])
def jobs_new():
    error = None
    form = {
        "job_id": "",
        "name": "",
        "mailbox": "Extract",
        "subject_pattern": "",
        "target_table": "orders",
        "download_folder": "Order-Extract",
        "interval_hours": "2",
        "ingest_mode": "replace",
        "enabled": True,
        "extract_zip": False,
    }

    if request.method == "POST":
        form = {
            "job_id": request.form.get("job_id", ""),
            "name": request.form.get("name", ""),
            "mailbox": request.form.get("mailbox", ""),
            "subject_pattern": request.form.get("subject_pattern", ""),
            "target_table": request.form.get("target_table", ""),
            "download_folder": request.form.get("download_folder", ""),
            "interval_hours": request.form.get("interval_hours", "2"),
            "ingest_mode": request.form.get("ingest_mode", "replace"),
            "enabled": request.form.get("enabled") == "on",
            "extract_zip": request.form.get("extract_zip") == "on",
        }
        try:
            if not form["name"].strip():
                raise ValueError("Display name is required.")
            if not form["mailbox"].strip():
                raise ValueError("Mailbox is required.")
            if not form["subject_pattern"].strip():
                raise ValueError("Subject pattern is required.")
            add_job(
                job_id=form["job_id"],
                name=form["name"].strip(),
                mailbox=form["mailbox"].strip(),
                subject_pattern=form["subject_pattern"].strip(),
                target_table=form["target_table"],
                download_folder=form["download_folder"].strip(),
                interval_hours=int(form["interval_hours"] or 2),
                enabled=form["enabled"],
                ingest_mode=form["ingest_mode"],
                extract_zip=form["extract_zip"],
            )
            from mail.jobs_db import _normalize_job_id
            slug = _normalize_job_id(form["job_id"])
            return redirect(url_for("jobs_list", msg=f"Job '{slug}' created"))
        except Exception as e:
            error = str(e)

    body = """
  <h1>New mail job</h1>
  <div class="sub">Job ID is auto-lowercased (spaces → underscores). Subject is a regex matched against email titles.</div>
  {% if error %}<div class="flash err">{{ error }}</div>{% endif %}
  <form method="post" class="panel" novalidate>
    <div class="form-row"><label>Job ID (e.g. order_extract)</label>
      <input type="text" name="job_id" required value="{{ form.job_id }}"
        placeholder="order_extract"></div>
    <div class="form-row"><label>Display name</label>
      <input type="text" name="name" required value="{{ form.name }}"
        placeholder="Order Extract"></div>
    <div class="form-row"><label>W1 mailbox button name</label>
      <input type="text" name="mailbox" required value="{{ form.mailbox }}"></div>
    <div class="form-row"><label>Subject pattern (regex)</label>
      <input type="text" name="subject_pattern" required value="{{ form.subject_pattern }}"
        placeholder="Order Extract - AE/GCC"></div>
    <div class="form-row"><label>SQL table name</label>
      <input type="text" name="target_table" required value="{{ form.target_table }}"></div>
    <div class="form-row"><label>Desktop folder name</label>
      <input type="text" name="download_folder" required value="{{ form.download_folder }}"
        placeholder="Product-Extract"></div>
    <p class="muted">Files save to Desktop/&lt;folder&gt; — e.g. C:/Users/you/Desktop/Product-Extract</p>
    <div class="form-row"><label>Check every (hours)</label>
      <input type="number" name="interval_hours" value="{{ form.interval_hours }}" min="1" required></div>
    <div class="form-row"><label>When a newer file arrives</label>
      <select name="ingest_mode">
        <option value="replace" {{ 'selected' if form.ingest_mode=='replace' else '' }}>Replace — swap old rows (recommended)</option>
        <option value="append" {{ 'selected' if form.ingest_mode=='append' else '' }}>Append — keep history</option>
      </select></div>
    <div class="form-row"><label><input type="checkbox" name="extract_zip" {{ 'checked' if form.extract_zip else '' }}> Extract Excel from .zip before parsing</label></div>
    <div class="form-row"><label><input type="checkbox" name="enabled" {{ 'checked' if form.enabled else '' }}> Enabled</label></div>
    <button type="submit">Create job</button>
    <a href="/jobs"><button type="button">Cancel</button></a>
  </form>
"""
    return _layout("New Job", "jobs", body, error=error, form=form)


@app.route("/jobs/parse", methods=["GET", "POST"])
def jobs_parse():
    seed_from_config()
    jobs = list_jobs()
    files = _list_excel_files()
    msg = request.args.get("msg")
    error = request.args.get("err")

    if request.method == "POST":
        filepath = request.form.get("filepath", "")
        table = request.form.get("target_table", "")
        filter_id = request.form.get("filter_id", "manual")
        path = filepath
        try:
            if not path or not os.path.isfile(path):
                raise FileNotFoundError(f"File not found: {path}")
            force = request.form.get("force") == "on"
            ingest_download(
                path, table=table, filter_id=filter_id,
                ingest_mode=request.form.get("ingest_mode", "replace"),
                force=force,
                extract_zip=request.form.get("extract_zip") == "on",
            )
            return redirect(url_for("jobs_parse", msg=f"Parsed {os.path.basename(path)} → {table}"))
        except Exception as e:
            error = str(e)

    body = """
  <h1>Manual parse to SQL</h1>
  <div class="sub">Decrypt + load an Excel file from the download folder without running a mail check.</div>
  {% if msg %}<div class="flash ok">{{ msg }}</div>{% endif %}
  {% if error %}<div class="flash err">{{ error }}</div>{% endif %}
  <form method="post" class="panel">
    <div class="form-row"><label>Excel file (all job folders on Desktop)</label>
      <select name="filepath" required>
        {% for f in files %}<option value="{{ f.path }}">{{ f.label }}</option>{% endfor %}
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
    <div class="form-row"><label><input type="checkbox" name="extract_zip"> Extract Excel from .zip before parsing</label></div>
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
                download_folder=request.form.get("download_folder", "").strip(),
                interval_hours=int(request.form.get("interval_hours", 2)),
                enabled=request.form.get("enabled") == "on",
                ingest_mode=request.form.get("ingest_mode", "replace"),
                extract_zip=request.form.get("extract_zip") == "on",
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
    <div class="form-row"><label>Desktop folder</label>
      <input type="text" name="download_folder" value="{{ job.download_folder }}" required></div>
    <div class="form-row"><label>Every (hours)</label>
      <input type="number" name="interval_hours" value="{{ job.interval_hours }}" min="1" required></div>
    <div class="form-row"><label>When a newer file arrives</label>
      <select name="ingest_mode">
        <option value="replace" {{ 'selected' if job.ingest_mode=='replace' else '' }}>Replace old rows</option>
        <option value="append" {{ 'selected' if job.ingest_mode=='append' else '' }}>Append rows</option>
      </select></div>
    <div class="form-row"><label><input type="checkbox" name="extract_zip" {{ 'checked' if job.extract_zip else '' }}> Extract Excel from .zip before parsing</label></div>
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


@app.route("/rpa")
def rpa_list():
    seed_rpa()
    from rpa.codegen import has_script

    jobs = list_rpa_jobs()
    for j in jobs:
        j["has_script"] = has_script(j["rpa_id"]) if j["tool"] == "codegen" else True
    mail_jobs = list_jobs()
    msg = request.args.get("msg")
    err = request.args.get("err")
    runs_df, _ = _read_sql(
        "SELECT ran_at, rpa_id, upload_file, status, message "
        "FROM rpa_runs ORDER BY id DESC LIMIT 20"
    )
    runs = runs_df.to_dict("records") if not runs_df.empty else []

    body = """
  <h1>RPA Tools</h1>
  <div class="sub">Record Playwright codegen scripts with a start URL, or use the built-in NERP flow.</div>
  <p><a href="/rpa/new"><button type="button">+ New recorded script</button></a></p>
  {% if msg %}<div class="flash ok">{{ msg }}</div>{% endif %}
  {% if err %}<div class="flash err">{{ err }}</div>{% endif %}
  <div class="panel"><h2>Registered tools</h2><div class="scroll">
    {% if jobs %}<table>
    <tr><th>Name</th><th>Type</th><th>Start URL</th><th>Trigger</th><th>Script</th><th>Last run</th><th>Status</th><th>Actions</th></tr>
    {% for r in jobs %}<tr>
      <td><span class="pill">{{ r.rpa_id }}</span><br><span class="muted">{{ r.name }}</span></td>
      <td>{{ r.tool }}</td>
      <td class="muted" style="max-width:220px;word-break:break-all">{{ r.start_url or '—' }}</td>
      <td>{% if r.trigger_mail_job %}<span class="pill">{{ r.trigger_mail_job }}</span>{% else %}<span class="muted">Manual</span>{% endif %}</td>
      <td>{% if r.tool == 'codegen' %}{% if r.has_script %}<span class="ok">saved</span>{% else %}<span class="err">not recorded</span>{% endif %}{% else %}<span class="muted">built-in</span>{% endif %}</td>
      <td>{{ r.last_run or '—' }}</td>
      <td class="{{ 'ok' if r.last_status=='ok' else 'err' if r.last_status else '' }}">{{ r.last_status or '—' }}</td>
      <td style="white-space:nowrap">
        <form method="post" action="/rpa/{{ r.rpa_id }}/run" style="display:inline"><button class="btn-sm btn-run">Run</button></form>
        <a href="/rpa/{{ r.rpa_id }}/edit"><button type="button" class="btn-sm">Edit</button></a>
        {% if r.tool == 'codegen' %}
        <form method="post" action="/rpa/{{ r.rpa_id }}/record" style="display:inline"><button class="btn-sm">Record</button></form>
        {% endif %}
        <form method="post" action="/rpa/{{ r.rpa_id }}/toggle" style="display:inline"><button class="btn-sm">{{ 'Off' if r.enabled else 'On' }}</button></form>
      </td>
    </tr>{% endfor %}</table>
    {% else %}<p class="muted">No RPA tools registered.</p>{% endif %}
  </div></div>
  <div class="panel"><h2>Which tool is which?</h2>
    <p class="muted" style="line-height:1.6">
      <b>nerp_upload_pi</b> — built-in NERP Upload + P/I (hardcoded in code)<br>
      <b>Everything else</b> — your recorded scripts. The grey <b>pill</b> in the first column is the real id
      (e.g. <code>order_creation</code> or <code>order_creation_2</code>). The line below is the display name you typed.<br>
      Your ZLSDF50270 + Sales Org flow is a <b>custom</b> script — check the pill to see if it is
      <code>order_creation</code> or <code>order_creation_2</code>.
    </p>
  </div>
  <div class="panel"><h2>Windows files (upload / save)</h2>
    <p class="muted" style="line-height:1.6">
      Set <b>Upload folder</b> and <b>Download folder</b> on Edit (or link a mail job — uses that job's Desktop folder).<br>
      <b>Upload:</b> your file from the upload folder (or exact file path) is picked in Chrome&apos;s file picker —
      the real path on disk, not renamed to <code>sample_bulk.XLSX</code>. Folder = newest spreadsheet there.<br>
      <b>Save:</b> Playwright downloads are auto-saved to the download folder. For native Save As dialogs use
      <code>win_save_as(RPA_DOWNLOAD_DIR)</code> (same helper as W1 mail).
    </p>
  </div>
  <div class="panel"><h2>Recording your own script</h2>
    <p class="muted" style="line-height:1.6">
      1. Click <b>+ New recorded script</b> and set the <b>start URL</b><br>
      2. Click <b>Record</b> — Playwright codegen opens in a new window<br>
      3. Perform your steps, then close the codegen window (script auto-saves)<br>
      4. Or paste/edit the Python script on the Edit page<br>
      5. <b>Run</b> to execute, or link to a mail job for auto-trigger after download
    </p>
  </div>
  <div class="panel"><h2>Recent RPA runs</h2><div class="scroll">
    {% if runs %}<table>
    <tr><th>Time</th><th>Tool</th><th>File</th><th>Status</th><th>Detail</th></tr>
    {% for r in runs %}<tr>
      <td>{{ r.ran_at }}</td><td>{{ r.rpa_id }}</td><td>{{ r.upload_file or '—' }}</td>
      <td class="{{ 'ok' if r.status=='ok' else 'err' }}">{{ r.status }}</td>
      <td class="muted">{{ r.message or '' }}</td>
    </tr>{% endfor %}</table>{% else %}<p class="muted">No RPA runs yet.</p>{% endif %}
  </div></div>
"""
    return _layout("RPA Tools", "rpa", body, jobs=jobs, runs=runs, msg=msg, err=err)


@app.route("/rpa/new", methods=["GET", "POST"])
def rpa_new():
    seed_rpa()
    error = None
    form = {
        "rpa_id": "",
        "name": "",
        "start_url": config.NERP_URL,
        "description": "",
        "trigger_mail_job": "",
        "enabled": False,
    }
    mail_jobs = list_jobs()

    if request.method == "POST":
        form = {
            "rpa_id": request.form.get("rpa_id", ""),
            "name": request.form.get("name", ""),
            "start_url": request.form.get("start_url", ""),
            "description": request.form.get("description", ""),
            "trigger_mail_job": request.form.get("trigger_mail_job", ""),
            "enabled": request.form.get("enabled") == "on",
        }
        try:
            slug = add_rpa_job(
                rpa_id=form["rpa_id"],
                name=form["name"],
                start_url=form["start_url"],
                description=form["description"],
                trigger_mail_job=form["trigger_mail_job"],
                enabled=form["enabled"],
            )
            return redirect(url_for("rpa_edit", rpa_id=slug, msg="Created — click Record to capture steps"))
        except Exception as e:
            error = str(e)

    body = """
  <h1>New recorded RPA script</h1>
  <div class="sub">Creates a custom tool you record with Playwright codegen.</div>
  {% if error %}<div class="flash err">{{ error }}</div>{% endif %}
  <form method="post" class="panel">
    <div class="form-row"><label>RPA id (e.g. my_upload_flow)</label>
      <input type="text" name="rpa_id" required value="{{ form.rpa_id }}" placeholder="my_upload_flow"></div>
    <div class="form-row"><label>Display name</label>
      <input type="text" name="name" required value="{{ form.name }}" placeholder="My upload flow"></div>
    <div class="form-row"><label>Start URL (codegen opens this page)</label>
      <input type="url" name="start_url" required value="{{ form.start_url }}" style="width:100%"></div>
    <div class="form-row"><label>Trigger after mail job</label>
      <select name="trigger_mail_job">
        <option value="">— Manual only —</option>
        {% for m in mail_jobs %}<option value="{{ m.job_id }}" {{ 'selected' if form.trigger_mail_job==m.job_id else '' }}>{{ m.job_id }} — {{ m.name }}</option>{% endfor %}
      </select></div>
    <div class="form-row"><label>Notes</label>
      <input type="text" name="description" value="{{ form.description }}"></div>
    <div class="form-row"><label><input type="checkbox" name="enabled" {{ 'checked' if form.enabled else '' }}> Enabled</label></div>
    <button type="submit">Create</button>
    <a href="/rpa"><button type="button">Cancel</button></a>
  </form>
"""
    return _layout("New RPA", "rpa", body, form=form, mail_jobs=mail_jobs, error=error)


@app.route("/rpa/<rpa_id>/edit", methods=["GET", "POST"])
def rpa_edit(rpa_id):
    seed_rpa()
    from rpa.codegen import has_script, read_script, save_script

    job = get_rpa_job(rpa_id)
    if not job:
        return redirect(url_for("rpa_list", err="RPA tool not found"))
    mail_jobs = list_jobs()
    error = None
    msg = request.args.get("msg")
    script_text = read_script(rpa_id) if job["tool"] == "codegen" else ""
    script_saved = has_script(rpa_id) if job["tool"] == "codegen" else True

    if request.method == "POST":
        try:
            fields = dict(
                name=request.form["name"].strip(),
                description=request.form.get("description", "").strip(),
                trigger_mail_job=request.form.get("trigger_mail_job", "").strip(),
                enabled=request.form.get("enabled") == "on",
            )
            if job["tool"] == "codegen":
                fields["start_url"] = request.form.get("start_url", "").strip()
                fields["upload_folder"] = request.form.get("upload_folder", "").strip()
                fields["download_folder"] = request.form.get("download_folder", "").strip()
            update_rpa_job(rpa_id, **fields)
            if job["tool"] == "codegen" and "script" in request.form:
                body = request.form.get("script", "")
                if body.strip():
                    save_script(rpa_id, body)
                    script_saved = True
            return redirect(url_for("rpa_list", msg=f"RPA '{rpa_id}' updated"))
        except Exception as e:
            error = str(e)
            script_text = request.form.get("script", script_text)

    body = """
  <h1>Edit RPA: {{ job.rpa_id }}</h1>
  <p class="muted">{{ job.description }}</p>
  {% if msg %}<div class="flash ok">{{ msg }}</div>{% endif %}
  {% if error %}<div class="flash err">{{ error }}</div>{% endif %}
  <form method="post" class="panel">
    <div class="form-row"><label>Display name</label>
      <input type="text" name="name" value="{{ job.name }}" required></div>
    <div class="form-row"><label>Start URL</label>
      <input type="url" name="start_url" value="{{ job.start_url }}" style="width:100%" {{ 'readonly' if job.tool != 'codegen' else '' }}></div>
    {% if job.tool == 'codegen' %}
    <div class="form-row"><label>Upload folder (Windows path)</label>
      <input type="text" name="upload_folder" value="{{ job.upload_folder }}" style="width:100%"
        placeholder="e.g. C:/Users/you/Desktop/My-Uploads — or full path to one .xlsx file"></div>
    <p class="muted">If set, this overrides the linked mail job file. Uses the newest Excel in that folder, or the exact file if you paste a full path. Does not auto-fill — type your path and Save.</p>
    <div class="form-row"><label>Download folder (Windows path)</label>
      <input type="text" name="download_folder" value="{{ job.download_folder }}" style="width:100%"
        placeholder="e.g. C:/Users/you/Desktop/Order-Results"></div>
    <div class="form-row">
      <label>Recorded script {% if script_saved %}<span class="ok">(saved)</span>{% else %}<span class="err">(not recorded yet)</span>{% endif %}</label>
      <textarea name="script" rows="18" style="width:100%;font-family:monospace;font-size:12px" placeholder="Paste Playwright codegen output here, or click Record below.">{{ script_text }}</textarea>
    </div>
    <p class="muted">File: rpa/scripts/{{ job.rpa_id }}.py</p>
    {% endif %}
    <div class="form-row"><label>Trigger after mail job</label>
      <select name="trigger_mail_job">
        <option value="">— Manual only —</option>
        {% for m in mail_jobs %}<option value="{{ m.job_id }}" {{ 'selected' if job.trigger_mail_job==m.job_id else '' }}>{{ m.job_id }} — {{ m.name }}</option>{% endfor %}
      </select></div>
    <div class="form-row"><label>Notes</label>
      <input type="text" name="description" value="{{ job.description }}"></div>
    <div class="form-row"><label><input type="checkbox" name="enabled" {{ 'checked' if job.enabled else '' }}> Enabled</label></div>
    <button type="submit">Save</button>
    {% if job.tool == 'codegen' %}
    <button type="submit" formaction="/rpa/{{ job.rpa_id }}/record" formmethod="post">Open recorder</button>
    {% endif %}
    <a href="/rpa"><button type="button">Back</button></a>
    {% if job.tool == 'codegen' %}
    <button type="submit" formaction="/rpa/{{ job.rpa_id }}/delete" formmethod="post" style="float:right;background:#5a2020" onclick="return confirm('Delete this RPA script?')">Delete</button>
    {% endif %}
  </form>
"""
    return _layout(
        "Edit RPA", "rpa", body,
        job=job, mail_jobs=mail_jobs, error=error, msg=msg,
        script_text=script_text, script_saved=script_saved,
    )


@app.route("/rpa/<rpa_id>/record", methods=["POST"])
def rpa_record(rpa_id):
    from rpa.codegen import launch_recorder

    job = get_rpa_job(rpa_id)
    if not job:
        return redirect(url_for("rpa_list", err="RPA tool not found"))
    if job["tool"] != "codegen":
        return redirect(url_for("rpa_edit", rpa_id=rpa_id, err="Only custom scripts can be recorded"))

    start_url = request.form.get("start_url", "").strip() or job.get("start_url", "")
    if request.form.get("start_url"):
        update_rpa_job(rpa_id, start_url=start_url)

    try:
        path = launch_recorder(rpa_id, start_url)
        return redirect(
            url_for(
                "rpa_list",
                msg=f"Recorder opened for '{rpa_id}'. Perform steps, close codegen when done. Saves to {os.path.basename(path)}",
            )
        )
    except Exception as e:
        return redirect(url_for("rpa_edit", rpa_id=rpa_id, err=str(e)))


@app.route("/rpa/<rpa_id>/delete", methods=["POST"])
def rpa_delete(rpa_id):
    try:
        delete_rpa_job(rpa_id)
        return redirect(url_for("rpa_list", msg=f"Deleted RPA '{rpa_id}'"))
    except Exception as e:
        return redirect(url_for("rpa_edit", rpa_id=rpa_id, err=str(e)))


@app.route("/rpa/<rpa_id>/run", methods=["POST"])
def rpa_run(rpa_id):
    import threading
    from rpa.runner import run_rpa

    if not get_rpa_job(rpa_id):
        return redirect(url_for("rpa_list", err="RPA tool not found"))

    def _bg():
        try:
            run_rpa(rpa_id)
        except Exception as e:
            print(f"RPA run failed: {e}")

    threading.Thread(target=_bg, daemon=True, name=f"rpa-{rpa_id}").start()
    return redirect(url_for("rpa_list", msg=f"Started '{rpa_id}' — check back in a few minutes"))


@app.route("/rpa/<rpa_id>/toggle", methods=["POST"])
def rpa_toggle(rpa_id):
    job = get_rpa_job(rpa_id)
    if not job:
        return redirect(url_for("rpa_list", err="RPA tool not found"))
    update_rpa_job(rpa_id, enabled=not job["enabled"])
    state = "disabled" if job["enabled"] else "enabled"
    return redirect(url_for("rpa_list", msg=f"RPA '{rpa_id}' {state}"))


@app.route("/api/ingestions")
def api_ingestions():
    if err := _api_guard():
        return err
    df, _ = _read_sql("SELECT * FROM ingestion_log ORDER BY id DESC LIMIT 100")
    return jsonify({"ingestions": df.to_dict("records")})


if __name__ == "__main__":
    seed_from_config()
    seed_rpa()
    if config.DASHBOARD_RUNS_SCHEDULER:
        from mail.cron import start_background
        start_background()
        print("Mail scheduler running in background (no instant run on startup).")
    ip = _local_ip()
    print(f"Dashboard: http://{ip}:{config.DASHBOARD_PORT}  (LAN)")
    print(f"           http://127.0.0.1:{config.DASHBOARD_PORT}  (local)")
    print("Manage mail cron jobs at /jobs")
    print("Manage RPA tools at /rpa")
    app.run(host=config.DASHBOARD_HOST, port=config.DASHBOARD_PORT, debug=False, threaded=True)
