"""
Local dashboard to track everything loaded into the database.

Run it with:  python dashboard.py   (or double-click run_dashboard.bat)
Then open:    http://127.0.0.1:5000  in Chrome.
"""
import os
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

from datetime import datetime

import pandas as pd
from flask import Flask, render_template_string
from sqlalchemy import create_engine, inspect, text

import config

app = Flask(__name__)
engine = create_engine(config.DB_URL)


def _table_exists(name):
    try:
        return inspect(engine).has_table(name)
    except Exception:
        return False


def _read_sql(query):
    try:
        return pd.read_sql(text(query), engine)
    except Exception:
        return pd.DataFrame()


def _svg_bar_chart(labels, values, width=760, height=220):
    """Server-rendered bar chart — no external JS/CDN needed."""
    if not values:
        return "<p class='muted'>No data yet.</p>"
    pad_l, pad_b, pad_t = 40, 40, 20
    plot_w = width - pad_l - 20
    plot_h = height - pad_b - pad_t
    vmax = max(values) or 1
    n = len(values)
    gap = 10
    bar_w = max(6, (plot_w - gap * (n - 1)) / n)

    bars, xlabels = [], []
    for i, (lab, val) in enumerate(zip(labels, values)):
        bh = (val / vmax) * plot_h
        x = pad_l + i * (bar_w + gap)
        y = pad_t + (plot_h - bh)
        bars.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bh:.1f}" '
            f'rx="3" fill="#5b8def"><title>{lab}: {val}</title></rect>'
        )
        if n <= 16:
            xlabels.append(
                f'<text x="{x + bar_w/2:.1f}" y="{height - pad_b + 15}" '
                f'font-size="10" fill="#8a94a6" text-anchor="middle">{lab}</text>'
            )
    axis = (
        f'<line x1="{pad_l}" y1="{pad_t + plot_h}" x2="{width-20}" y2="{pad_t + plot_h}" '
        f'stroke="#2a3142"/>'
        f'<text x="8" y="{pad_t + 8}" font-size="10" fill="#8a94a6">{vmax}</text>'
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" '
        f'preserveAspectRatio="xMidYMid meet">{axis}{"".join(bars)}{"".join(xlabels)}</svg>'
    )


TEMPLATE = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Order Extract Dashboard</title>
<meta http-equiv="refresh" content="60">
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: #0d1017; color: #e6e9ef;
    font-family: -apple-system, Segoe UI, Roboto, sans-serif;
  }
  .wrap { max-width: 1100px; margin: 0 auto; padding: 32px 24px 64px; }
  h1 { font-size: 22px; margin: 0 0 4px; }
  .sub { color: #8a94a6; font-size: 13px; margin-bottom: 28px; }
  .cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 28px; }
  .card { background: #161b26; border: 1px solid #232a38; border-radius: 12px; padding: 18px; }
  .card .label { color: #8a94a6; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
  .card .value { font-size: 26px; font-weight: 600; margin-top: 6px; }
  .panel { background: #161b26; border: 1px solid #232a38; border-radius: 12px; padding: 20px; margin-bottom: 22px; }
  .panel h2 { font-size: 14px; margin: 0 0 14px; color: #b7c0d0; text-transform: uppercase; letter-spacing: .04em; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #232a38; white-space: nowrap; }
  th { color: #8a94a6; font-weight: 500; }
  tr:hover td { background: #1b2130; }
  .ok { color: #4ec98a; } .err { color: #ef6a6a; }
  .muted { color: #8a94a6; font-size: 13px; }
  .scroll { overflow-x: auto; }
  .pill { font-size: 11px; padding: 2px 8px; border-radius: 999px; background: #232a38; color: #b7c0d0; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Order Extract Dashboard</h1>
  <div class="sub">Auto-refreshes every 60s · Last viewed {{ now }}</div>

  <div class="cards">
    <div class="card"><div class="label">Total rows</div><div class="value">{{ total_rows }}</div></div>
    <div class="card"><div class="label">Files ingested</div><div class="value">{{ total_files }}</div></div>
    <div class="card"><div class="label">Last run</div><div class="value" style="font-size:16px">{{ last_run }}</div></div>
    <div class="card"><div class="label">Last status</div>
      <div class="value" style="font-size:18px">
        <span class="{{ 'ok' if last_status=='success' else 'err' }}">{{ last_status or '—' }}</span>
      </div>
    </div>
  </div>

  <div class="panel">
    <h2>Rows loaded per ingestion</h2>
    {{ chart|safe }}
  </div>

  <div class="panel">
    <h2>Ingestion history</h2>
    <div class="scroll">
      {% if log_rows %}
      <table>
        <tr><th>Loaded at</th><th>File</th><th>Rows</th><th>Status</th><th>Batch</th><th>Message</th></tr>
        {% for r in log_rows %}
        <tr>
          <td>{{ r.loaded_at }}</td>
          <td>{{ r.source_file }}</td>
          <td>{{ r.row_count }}</td>
          <td class="{{ 'ok' if r.status=='success' else 'err' }}">{{ r.status }}</td>
          <td><span class="pill">{{ r.batch_id }}</span></td>
          <td class="muted">{{ r.message }}</td>
        </tr>
        {% endfor %}
      </table>
      {% else %}
      <p class="muted">Nothing ingested yet. Run the scheduler to populate this.</p>
      {% endif %}
    </div>
  </div>

  <div class="panel">
    <h2>Latest data preview {% if preview_cols %}<span class="pill">{{ preview_count }} rows shown</span>{% endif %}</h2>
    <div class="scroll">
      {% if preview_cols %}
      <table>
        <tr>{% for c in preview_cols %}<th>{{ c }}</th>{% endfor %}</tr>
        {% for row in preview_rows %}
        <tr>{% for c in preview_cols %}<td>{{ row[c] }}</td>{% endfor %}</tr>
        {% endfor %}
      </table>
      {% else %}
      <p class="muted">No order data yet.</p>
      {% endif %}
    </div>
  </div>
</div>
</body>
</html>
"""


@app.route("/")
def index():
    total_rows = 0
    total_files = 0
    last_run = "—"
    last_status = None
    log_rows = []
    chart = "<p class='muted'>No data yet.</p>"
    preview_cols, preview_rows, preview_count = [], [], 0

    # Ingestion log
    if _table_exists("ingestion_log"):
        log_df = _read_sql(
            "SELECT loaded_at, source_file, row_count, status, batch_id, message "
            "FROM ingestion_log ORDER BY id DESC LIMIT 100"
        )
        if not log_df.empty:
            log_rows = log_df.to_dict("records")
            total_files = int((log_df["status"] == "success").sum())
            last = log_df.iloc[0]
            last_run = str(last["loaded_at"])
            last_status = last["status"]

            # chart: rows per successful ingestion (oldest -> newest)
            ok = log_df[log_df["status"] == "success"].iloc[::-1]
            if not ok.empty:
                labels = [str(x)[5:16] for x in ok["loaded_at"]]  # MM-DD HH:MM
                values = [int(v) for v in ok["row_count"]]
                chart = _svg_bar_chart(labels, values)

    # Orders totals + latest-batch preview
    if _table_exists(config.ORDERS_TABLE):
        cnt = _read_sql(f"SELECT COUNT(*) AS c FROM {config.ORDERS_TABLE}")
        if not cnt.empty:
            total_rows = int(cnt.iloc[0]["c"])

        latest_batch = _read_sql(
            f"SELECT batch_id FROM {config.ORDERS_TABLE} "
            f"ORDER BY loaded_at DESC LIMIT 1"
        )
        if not latest_batch.empty:
            bid = latest_batch.iloc[0]["batch_id"]
            prev = _read_sql(
                f"SELECT * FROM {config.ORDERS_TABLE} "
                f"WHERE batch_id = '{bid}' LIMIT 20"
            )
            if not prev.empty:
                # keep the table readable: first 10 columns
                preview_cols = list(prev.columns)[:10]
                preview_rows = prev.to_dict("records")
                preview_count = len(prev)

    return render_template_string(
        TEMPLATE,
        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        total_rows=total_rows,
        total_files=total_files,
        last_run=last_run,
        last_status=last_status,
        log_rows=log_rows,
        chart=chart,
        preview_cols=preview_cols,
        preview_rows=preview_rows,
        preview_count=preview_count,
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
