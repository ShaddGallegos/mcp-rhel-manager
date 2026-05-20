#!/usr/bin/env python3
"""Enhanced MCP dashboard with toggles, chat, basic reporting and realtime stats.
#!/usr/bin/env python3
"""Minimal Flask dashboard to view and approve plans.

Run: `FLASK_APP=mcp-ai/dashboard.py flask run --host=0.0.0.0 --port=8080`
Requires: `flask` (install into venv if needed)
"""
from pathlib import Path
import sys, logging, json, os
from flask import Flask, render_template_string, request, redirect

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Lightweight dependency-check mode useful to fail fast under systemd
if "--check-deps" in sys.argv:
  try:
    import flask  # noqa: F401
    print("OK")
    sys.exit(0)
  except Exception as e:
    print(f"Missing dependency: {e}", file=sys.stderr)
    sys.exit(2)

HOME = os.path.expanduser('~')
FIXES = os.path.join(HOME, '.mcp-ai', 'fixes')
APPROVALS = os.path.join(HOME, '.mcp-ai', 'approvals')

app = Flask(__name__)

TEMPLATE = '''
<!doctype html>
<title>MCP Plans</title>
<h1>Plans</h1>
<ul>
{% for p in plans %}
  <li>
  <a href="/plan?file={{p}}">{{p}}</a>
  {% if p in approved %} - <strong>APPROVED</strong>{% endif %}
  </li>
{% endfor %}
</ul>
'''


@app.route('/')
def index():
  p = Path(FIXES)
  plans = [str(x.name) for x in sorted(p.glob('plan-*.json'))] if p.exists() else []
  appd = Path(APPROVALS)
  approved = [str(x.name).replace('.approved.json','') for x in appd.glob('plan-*.approved.json')] if appd.exists() else []
  return render_template_string(TEMPLATE, plans=plans, approved=approved)


@app.route('/plan')
def plan_view():
  fn = request.args.get('file')
  if not fn:
    return redirect('/')
  ppath = os.path.join(FIXES, fn)
  if not os.path.exists(ppath):
    return 'Plan not found', 404
  with open(ppath,'r') as fh:
    pl = json.load(fh)
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
  os.makedirs(APPROVALS, exist_ok=True)
  with open(ap,'w') as fh:
    json.dump({'plan': ppath}, fh)
  return redirect('/')


if __name__ == '__main__':
  try:
    host = os.environ.get('MCP_DASHBOARD_HOST', '127.0.0.1')
    port = int(os.environ.get('MCP_DASHBOARD_PORT', '8080'))
    app.run(host=host, port=port)
  except Exception:
    logging.exception("Dashboard failed to start")
    raise
def read_chat(limit=100):
    msgs = []
    if not os.path.exists(CHAT_LOG):
        return msgs
    with open(CHAT_LOG, 'r') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                msgs.append(json.loads(line))
            except Exception:
                continue
    return msgs[-limit:]


def get_stats():
    try:
        p = Path(FIXES)
        plans = sum(1 for _ in p.glob('plan-*.json') if _.is_file()) if p.exists() else 0
        a = Path(APPROVALS)
        approvals = sum(1 for _ in a.glob('plan-*.approved.json') if _.is_file()) if a.exists() else 0
        # memory usage
        usage = resource.getrusage(resource.RUSAGE_SELF)
        mem_kb = getattr(usage, 'ru_maxrss', 0)
        return {
            'timestamp': datetime.datetime.utcnow().isoformat() + 'Z',
            'plans': plans,
            'approvals': approvals,
            'pid': os.getpid(),
          'mem_kb': mem_kb,
        }
    except Exception as exc:
        return {'error': str(exc)}


@app.route('/')
def index():
    cfg = load_config()
    p = Path(FIXES)
    plans = [str(x.name) for x in sorted(p.glob('plan-*.json'))] if p.exists() else []
    appd = Path(APPROVALS)
    approved = [str(x.name).replace('.approved.json', '') for x in appd.glob('plan-*.approved.json')] if appd.exists() else []
    initial_chat = read_chat(50)

    TEMPLATE = r'''
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
        const initial = {{ initial_chat|tojson }};
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
          // Try SSE first
          try{
            const es = new EventSource('/stream');
            es.onmessage = function(e){
              try{const j=JSON.parse(e.data); addPoint(j.timestamp,j.plans,j.approvals);}catch(err){}
            };
          }catch(err){
            // fallback polling
            setInterval(async ()=>{ const r=await fetch('/api/stats'); if(r.ok){const j=await r.json(); addPoint(j.timestamp,j.plans,j.approvals);} }, 5000);
          }
        }
      </script>
    </body>
    </html>
    '''

    return render_template_string(TEMPLATE, plans=plans, approved=approved, cfg=cfg, initial_chat=initial_chat)


@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    if request.method == 'GET':
        return jsonify(load_config())
    data = request.get_json(silent=True) or {}
    cfg = load_config()
    cfg.update({k: bool(data.get(k, cfg.get(k))) for k in cfg.keys()})
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


# Prometheus metrics
REQUEST_COUNT = Counter('mcp_http_requests_total', 'Total HTTP requests', ['method', 'endpoint'])
PLANS_GAUGE = Gauge('mcp_plans_total', 'Number of pending plans')
APPROVALS_GAUGE = Gauge('mcp_approvals_total', 'Number of approved plans')
READINESS_GAUGE = Gauge('mcp_ready', 'Readiness (1=ready, 0=not ready)')
DISK_FREE_GAUGE = Gauge('mcp_disk_free_bytes', 'Available disk space in bytes')
MODELS_INDEX_GAUGE = Gauge('mcp_models_index_status', 'Models index status (1=ok,0=absent_or_invalid)')


@app.before_request
def before_request_metrics():
    try:
        REQUEST_COUNT.labels(method=request.method, endpoint=request.path).inc()
    except Exception:
        pass


@app.route('/api/stats')
def api_stats():
    return jsonify(get_stats())


@app.route('/metrics')
def metrics():
    # update gauges with current stats
    s = get_stats()
    try:
        PLANS_GAUGE.set(s.get('plans', 0))
        APPROVALS_GAUGE.set(s.get('approvals', 0))
    except Exception:
        pass
    # compute readiness and disk metrics
    try:
        ready, checks = check_readiness()
        READINESS_GAUGE.set(1 if ready else 0)
        disk_free = checks.get('disk_free_bytes') or 0
        DISK_FREE_GAUGE.set(disk_free)
        MODELS_INDEX_GAUGE.set(1 if checks.get('models_index') == 'ok' else 0)
    except Exception:
        pass
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


def sse_stream(delay=5):
    # generator yielding Server-Sent Events with JSON payload
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
def readiness():
  ready, checks = check_readiness()
  status = 200 if ready else 503
  return jsonify({'ready': ready, 'checks': checks}), status


def check_readiness():
  # readiness check: model directory and index readability + disk space
  checks = {}
  model_dir = '/var/lib/mcp-llms'
  ok = True
  try:
    if not os.path.isdir(model_dir):
      checks['model_dir'] = 'missing'
      ok = False
    else:
      idx = os.path.join(model_dir, 'models.json')
      if os.path.exists(idx):
        try:
          with open(idx, 'r') as fh:
            json.load(fh)
          checks['models_index'] = 'ok'
        except Exception:
          checks['models_index'] = 'invalid'
          ok = False
      else:
        checks['models_index'] = 'absent'
  except Exception as exc:
    checks['error'] = str(exc)
    ok = False

  try:
    du = shutil.disk_usage('/')
    checks['disk_free_bytes'] = du.free
    # require at least 100MB free
    if du.free < 100 * 1024 * 1024:
      checks['disk'] = 'low'
      ok = False
    else:
      checks['disk'] = 'ok'
  except Exception:
    checks['disk'] = 'unknown'

  return ok, checks


@app.route('/plan')
def plan_view():
    fn = request.args.get('file')
    if not fn:
        return redirect('/')
    ppath = os.path.join(FIXES, fn)
    if not os.path.exists(ppath):
        return 'Plan not found', 404
    with open(ppath, 'r') as fh:
        pl = json.load(fh)
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
    os.makedirs(APPROVALS, exist_ok=True)
    with open(ap, 'w') as fh:
        json.dump({'plan': ppath}, fh)
    return redirect('/')


if __name__ == '__main__':
    host = os.environ.get('MCP_DASH_HOST', '0.0.0.0')
    port = int(os.environ.get('MCP_DASH_PORT', '8080'))
    app.run(host=host, port=port)
