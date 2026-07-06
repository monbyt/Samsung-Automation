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
from flask import Flask, jsonify, render_template_string, request
from sqlalchemy import create_engine, inspect, text

import config
from db import init_db

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
button, select { background: #232a38; color: #e6e9ef; border: 1px solid #3a4458;
  border-radius: 8px; padding: 8px 14px; cursor: pointer; }
"""


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
    <a href="/mail" class="{{{{ 'active' if active=='mail' else '' }}}}">Mail Monitor</a>
    <a href="/data" class="{{{{ 'active' if active=='data' else '' }}}}">Data Explorer</a>
    <a href="/query" class="{{{{ 'active' if active=='query' else '' }}}}">SQL Query</a>
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

    for t in config.MAIL_FILTERS:
        tbl = t["table"]
        if tbl in _tables():
            cnt_df, _ = _read_sql(f"SELECT COUNT(*) AS c FROM {tbl}")
            rows = int(cnt_df.iloc[0]["c"]) if not cnt_df.empty else 0
            total_rows += rows
            table_stats.append({"id": t["id"], "table": tbl, "rows": rows})

    body = """
  <h1>Overview</h1>
  <div class="sub">LAN: http://{{ lan_ip }}:{{ port }} · Refreshes every 60s · {{ now }}</div>
  <div class="cards">
    <div class="card"><div class="label">Total rows</div><div class="value">{{ total_rows }}</div></div>
    <div class="card"><div class="label">Files ingested</div><div class="value">{{ total_files }}</div></div>
    <div class="card"><div class="label">Last ingestion</div><div class="value" style="font-size:15px">{{ last_run }}</div></div>
    <div class="card"><div class="label">Monitor interval</div><div class="value" style="font-size:18px">{{ interval }}m</div></div>
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
        interval=config.MONITOR_INTERVAL_MINUTES, table_stats=table_stats, log_rows=log_rows,
    )


@app.route("/mail")
def mail_status():
    runs_df, _ = _read_sql(
        "SELECT checked_at, downloads, errors, status, error_detail "
        "FROM monitor_runs ORDER BY id DESC LIMIT 100"
    )
    runs = runs_df.to_dict("records") if not runs_df.empty else []
    last_check = str(runs[0]["checked_at"]) if runs else "Never"
    filters = config.MAIL_FILTERS

    body = """
  <h1>Mail Monitor</h1>
  <div class="sub">Last check: {{ last_check }} · Run <code>python mail/monitor.py</code> on the always-on PC</div>
  <div class="panel"><h2>Active filters</h2>
    <table><tr><th>ID</th><th>Mailbox</th><th>Subject pattern</th><th>SQL table</th></tr>
    {% for f in filters %}<tr>
      <td><span class="pill">{{ f.id }}</span></td><td>{{ f.mailbox }}</td>
      <td><code>{{ f.subject }}</code></td><td>{{ f.table }}</td>
    </tr>{% endfor %}</table>
  </div>
  <div class="panel"><h2>Check history</h2><div class="scroll">
    {% if runs %}<table>
    <tr><th>Checked at</th><th>Downloads</th><th>Errors</th><th>Status</th><th>Detail</th></tr>
    {% for r in runs %}<tr>
      <td>{{ r.checked_at }}</td><td>{{ r.downloads }}</td><td>{{ r.errors }}</td>
      <td class="{{ 'ok' if r.status=='ok' else 'err' }}">{{ r.status }}</td>
      <td class="muted">{{ r.error_detail or '' }}</td>
    </tr>{% endfor %}</table>{% else %}<p class="muted">Monitor hasn't run yet.</p>{% endif %}
  </div></div>
"""
    return _layout("Mail Monitor", "mail", body, last_check=last_check, filters=filters, runs=runs)


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

@app.route("/api/health")
def api_health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})


@app.route("/api/tables")
def api_tables():
    return jsonify({"tables": _tables()})


@app.route("/api/table/<name>")
def api_table(name):
    if name not in _tables():
        return jsonify({"error": "table not found"}), 404
    limit = min(int(request.args.get("limit", 100)), 1000)
    offset = int(request.args.get("offset", 0))
    df, err = _read_sql(f"SELECT * FROM {name} LIMIT :lim OFFSET :off", {"lim": limit, "off": offset})
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"table": name, "rows": df.to_dict("records"), "count": len(df)})


@app.route("/api/ingestions")
def api_ingestions():
    df, _ = _read_sql("SELECT * FROM ingestion_log ORDER BY id DESC LIMIT 100")
    return jsonify({"ingestions": df.to_dict("records")})


if __name__ == "__main__":
    ip = _local_ip()
    print(f"Dashboard: http://{ip}:{config.DASHBOARD_PORT}  (LAN)")
    print(f"           http://127.0.0.1:{config.DASHBOARD_PORT}  (local)")
    app.run(host=config.DASHBOARD_HOST, port=config.DASHBOARD_PORT, debug=False, threaded=True)
