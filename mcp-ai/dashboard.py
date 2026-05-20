#!/usr/bin/env python3
"""MCP Dashboard

Lightweight Flask-based dashboard to list/approve planned remediation
JSON files, view a small chat log, and expose simple stats. Prometheus
metrics are optional (only enabled when `prometheus_client` is installed).

This module is intentionally minimal and defensive so it can run under
systemd or as a dev server. No secrets or credentials are stored here.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Dict, List

from flask import Flask, Response, jsonify, redirect, render_template_string, request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Optional prometheus support
try:
    from prometheus_client import Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST
    METRICS_AVAILABLE = True
except Exception:
    METRICS_AVAILABLE = False


HOME = os.path.expanduser("~")
BASE_DIR = os.path.join(HOME, ".mcp-ai")
FIXES = os.path.join(BASE_DIR, "fixes")
APPROVALS = os.path.join(BASE_DIR, "approvals")
CHAT_LOG = os.path.join(BASE_DIR, "chat.log")
CONFIG_PATH = os.path.join(BASE_DIR, "dashboard_config.json")

os.makedirs(FIXES, exist_ok=True)
os.makedirs(APPROVALS, exist_ok=True)
os.makedirs(BASE_DIR, exist_ok=True)

app = Flask(__name__)


def default_config() -> Dict:
    return {
        "enable_ai_features": False,
        "enable_chat": True,
        "enable_realtime_stats": True,
    }


def load_config() -> Dict:
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                return json.load(fh)
    except Exception:
        logging.exception("Failed to load dashboard config")
    return default_config()


def save_config(cfg: Dict) -> bool:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
        return True
    except Exception:
        logging.exception("Failed to save dashboard config")
        return False


def append_chat(msg: Dict) -> None:
    try:
        with open(CHAT_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(msg, ensure_ascii=False) + "\n")
    except Exception:
        logging.exception("Failed to append chat message")


def read_chat(limit: int = 100) -> List[Dict]:
    msgs: List[Dict] = []
    if not os.path.exists(CHAT_LOG):
        return msgs
    try:
        with open(CHAT_LOG, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    msgs.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        logging.exception("Failed to read chat log")
    return msgs[-limit:]


def get_stats() -> Dict:
    try:
        p = Path(FIXES)
        plans = sum(1 for _ in p.glob("plan-*.json") if _.is_file()) if p.exists() else 0
        a = Path(APPROVALS)
        approvals = sum(1 for _ in a.glob("plan-*.approved.json") if _.is_file()) if a.exists() else 0
        try:
            import resource

            usage = resource.getrusage(resource.RUSAGE_SELF)
            mem_kb = getattr(usage, "ru_maxrss", 0)
        except Exception:
            mem_kb = 0
        return {
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
            "plans": plans,
            "approvals": approvals,
            "pid": os.getpid(),
            "mem_kb": mem_kb,
        }
    except Exception as exc:
        logging.exception("get_stats failed")
        return {"error": str(exc)}


def check_readiness() -> (bool, Dict):
    checks: Dict = {}
    ok = True
    model_dir = "/var/lib/mcp-llms"
    try:
        if not os.path.isdir(model_dir):
            checks["model_dir"] = "missing"
            ok = False
        else:
            idx = os.path.join(model_dir, "models.json")
            if os.path.exists(idx):
                try:
                    with open(idx, "r", encoding="utf-8") as fh:
                        json.load(fh)
                    checks["models_index"] = "ok"
                except Exception:
                    checks["models_index"] = "invalid"
                    ok = False
            else:
                checks["models_index"] = "absent"
    except Exception as exc:
        checks["error"] = str(exc)
        ok = False

    try:
        du = shutil.disk_usage("/")
        checks["disk_free_bytes"] = du.free
        if du.free < 100 * 1024 * 1024:
            checks["disk"] = "low"
            ok = False
        else:
            checks["disk"] = "ok"
    except Exception:
        checks["disk"] = "unknown"

    return ok, checks


# Prometheus metrics (optional)
if METRICS_AVAILABLE:
    REQUEST_COUNT = Counter("mcp_http_requests_total", "Total HTTP requests", ["method", "endpoint"])
    PLANS_GAUGE = Gauge("mcp_plans_total", "Number of pending plans")
    APPROVALS_GAUGE = Gauge("mcp_approvals_total", "Number of approved plans")
    READINESS_GAUGE = Gauge("mcp_ready", "Readiness (1=ready, 0=not ready)")
    DISK_FREE_GAUGE = Gauge("mcp_disk_free_bytes", "Available disk space in bytes")
else:
    REQUEST_COUNT = PLANS_GAUGE = APPROVALS_GAUGE = READINESS_GAUGE = DISK_FREE_GAUGE = None


@app.before_request
def before_request_metrics():
    try:
        if REQUEST_COUNT is not None:
            REQUEST_COUNT.labels(method=request.method, endpoint=request.path).inc()
    except Exception:
        pass


@app.route("/")
def index():
    cfg = load_config()
    p = Path(FIXES)
    plans = [str(x.name) for x in sorted(p.glob("plan-*.json"))] if p.exists() else []
    appd = Path(APPROVALS)
    approved = [str(x.name).replace('.approved.json', '') for x in appd.glob('plan-*.approved.json')] if appd.exists() else []
    initial_chat = read_chat(50)

    TEMPLATE = r"""
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width,initial-scale=1" />
      <title>MCP Dashboard</title>
      <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
      <style>
        body{font-family: Arial, Helvetica, sans-serif; margin:20px}
        .col{display:inline-block;vertical-align:top;margin-right:20px}
        #chat {border:1px solid #ddd;padding:8px;width:380px;height:300px;overflow:auto}
        #messages {height:230px;overflow:auto}
      </style>
    </head>
    <body>
      <h1>MCP Dashboard</h1>
      <div class="col">
        <h3>Toggles</h3>
        <form id="cfgform">
          <label><input type="checkbox" id="enable_ai" /> Enable AI features</label><br/>
          <label><input type="checkbox" id="enable_chat" /> Enable Chat</label><br/>
          <label><input type="checkbox" id="enable_realtime" /> Enable Realtime Stats</label><br/>
          <button type="button" id="saveCfg">Save</button>
        </form>

        <h3>Plans</h3>
        <ul id="plansList">
          {% for p in plans %}
            <li>{{p}} {% if p in approved %}<strong>APPROVED</strong>{% endif %}</li>
          {% endfor %}
        </ul>
      </div>

      <div class="col">
        <h3>Realtime Stats</h3>
        <canvas id="statsChart" width="400" height="200"></canvas>
        <pre id="statsDump"></pre>
      </div>

      <div class="col">
        <h3>Chat</h3>
        <div id="chat">
          <div id="messages"></div>
          <div id="chatControls">
            <input id="chatUser" placeholder="user" style="width:90px" />
            <input id="chatMsg" placeholder="message" style="width:220px" />
            <button id="send">Send</button>
          </div>
        </div>
      </div>

      <script>
        const cfg = {{ cfg|tojson }};
        document.getElementById('enable_ai').checked = cfg.enable_ai_features;
        document.getElementById('enable_chat').checked = cfg.enable_chat;
        document.getElementById('enable_realtime').checked = cfg.enable_realtime_stats;

        document.getElementById('saveCfg').addEventListener('click', async ()=>{
          const newcfg = {
            enable_ai_features: document.getElementById('enable_ai').checked,
            enable_chat: document.getElementById('enable_chat').checked,
            enable_realtime_stats: document.getElementById('enable_realtime').checked,
          };
          const resp = await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(newcfg)});
          if(resp.ok) alert('Saved'); else alert('Save failed');
        });

        // Chat
        const messagesEl = document.getElementById('messages');
        async function loadChat(){
          const r = await fetch('/api/chat?limit=50');
          if(!r.ok) return;
          const data = await r.json();
          messagesEl.innerHTML = '';
          data.forEach(m=>{
            const el = document.createElement('div');
            el.textContent = (m.ts||'') + ' ' + (m.user||'') + ': ' + (m.msg||'');
            messagesEl.appendChild(el);
          });
          messagesEl.scrollTop = messagesEl.scrollHeight;
        }
        document.getElementById('send').addEventListener('click', async ()=>{
          const user = document.getElementById('chatUser').value || 'web';
          const msg = document.getElementById('chatMsg').value || '';
          if(!msg) return;
          await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body: JSON.stringify({user:user,msg:msg})});
          document.getElementById('chatMsg').value='';
          await loadChat();
        });
        // initial chat
        (async ()=>{ await loadChat(); })();

        // Stats chart
        const ctx = document.getElementById('statsChart').getContext('2d');
        const chart = new Chart(ctx, {
          type: 'line',
          data: {labels:[], datasets:[{label:'plans',data:[],borderColor:'blue'},{label:'approvals',data:[],borderColor:'green'}]},
          options: {animation:false,scales:{y:{beginAtZero:true}}}
        });

        function addPoint(ts, plans, approvals){
          chart.data.labels.push(ts);
          chart.data.datasets[0].data.push(plans);
          chart.data.datasets[1].data.push(approvals);
          if(chart.data.labels.length>30){ chart.data.labels.shift(); chart.data.datasets.forEach(ds=>ds.data.shift()); }
          chart.update();
          document.getElementById('statsDump').textContent = JSON.stringify({ts,plans,approvals},null,2);
        }

        if(cfg.enable_realtime_stats){
          try{
            const es = new EventSource('/stream');
            es.onmessage = function(e){ try{const j=JSON.parse(e.data); addPoint(j.timestamp,j.plans,j.approvals);}catch(err){} };
          }catch(err){
            setInterval(async ()=>{ const r=await fetch('/api/stats'); if(r.ok){const j=await r.json(); addPoint(j.timestamp,j.plans,j.approvals);} }, 5000);
          }
        }
      </script>
    </body>
    </html>
    """

    return render_template_string(TEMPLATE, plans=plans, approved=approved, cfg=cfg, initial_chat=initial_chat)


@app.route('/plan')
def plan_view():
    fn = request.args.get('file')
    if not fn:
        return redirect('/')
    ppath = os.path.join(FIXES, fn)
    if not os.path.exists(ppath):
        return 'Plan not found', 404
    try:
        with open(ppath, 'r', encoding='utf-8') as fh:
            pl = json.load(fh)
    except Exception:
        return 'Malformed plan', 500
    approved_path = os.path.join(APPROVALS, fn + '.approved.json')
    return f"<pre>{json.dumps(pl, indent=2)}</pre><form method='post' action='/approve?file={fn}'><button type='submit'>Approve</button></form>"


@app.route('/approve', methods=['POST'])
def approve():
    fn = request.args.get('file')
    if not fn:
        return redirect('/')
    ppath = os.path.join(FIXES, fn)
    if not os.path.exists(ppath):
        return 'Plan not found', 404
    ap = os.path.join(APPROVALS, fn + '.approved.json')
    try:
        with open(ap, 'w', encoding='utf-8') as fh:
            json.dump({'plan': ppath, 'approved_at': datetime.datetime.utcnow().isoformat() + 'Z'}, fh)
    except Exception:
        logging.exception('Failed to write approval file')
        return ('', 500)
    return redirect('/')


@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    if request.method == 'GET':
        return jsonify(load_config())
    data = request.get_json(silent=True) or {}
    cfg = load_config()
    for k in cfg.keys():
        if k in data:
            cfg[k] = bool(data.get(k))
    ok = save_config(cfg)
    return jsonify({'ok': ok, 'config': cfg}) if ok else ('', 500)


@app.route('/api/chat', methods=['GET', 'POST'])
def api_chat():
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        msg = {'user': data.get('user', 'web'), 'msg': data.get('msg', ''), 'ts': datetime.datetime.utcnow().isoformat() + 'Z'}
        append_chat(msg)
        return jsonify({'ok': True})
    limit = int(request.args.get('limit', '50'))
    return jsonify(read_chat(limit))


@app.route('/api/stats')
def api_stats():
    return jsonify(get_stats())


@app.route('/metrics')
def metrics():
    if not METRICS_AVAILABLE:
        return ('Metrics not enabled (install prometheus_client)', 501)
    s = get_stats()
    try:
        PLANS_GAUGE.set(s.get('plans', 0))
        APPROVALS_GAUGE.set(s.get('approvals', 0))
    except Exception:
        pass
    try:
        ready, checks = check_readiness()
        READINESS_GAUGE.set(1 if ready else 0)
        DISK_FREE_GAUGE.set(checks.get('disk_free_bytes') or 0)
    except Exception:
        pass
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


def sse_stream(delay: int = 5):
    while True:
        data = get_stats()
        payload = json.dumps(data)
        yield f"data: {payload}\n\n"
        time.sleep(delay)


@app.route('/stream')
def stream():
    cfg = load_config()
    if not cfg.get('enable_realtime_stats', True):
        return ('Realtime stats disabled', 404)
    return Response(sse_stream(), mimetype='text/event-stream')


@app.route('/health')
def health():
    s = get_stats()
    if 'error' in s:
        return jsonify({'status': 'error', 'error': s['error']}), 500
    return jsonify({'status': 'ok', 'plans': s.get('plans', 0), 'approvals': s.get('approvals', 0)})


@app.route('/health/ready')
def readiness_route():
    ready, checks = check_readiness()
    status = 200 if ready else 503
    return jsonify({'ready': ready, 'checks': checks}), status


if __name__ == '__main__':
    try:
        host = os.environ.get('MCP_DASHBOARD_HOST', '127.0.0.1')
        port = int(os.environ.get('MCP_DASHBOARD_PORT', '8080'))
        app.run(host=host, port=port)
    except Exception:
        logging.exception('Dashboard failed to start')
        raise
