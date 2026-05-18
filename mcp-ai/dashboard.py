#!/usr/bin/env python3
"""Minimal Flask dashboard to view and approve plans.

Run: `FLASK_APP=mcp-ai/dashboard.py flask run --host=0.0.0.0 --port=8080`
Requires: `flask` (install into venv if needed)
"""
from pathlib import Path
from flask import Flask, render_template_string, request, redirect, jsonify
import json, os

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


@app.route('/health')
def health():
    """Simple health endpoint for monitoring.

    Returns JSON with counts of plan files and approvals and a 200 status when
    the dashboard can access its storage directories.
    """
    try:
        p = Path(FIXES)
        plans = 0
        if p.exists():
            plans = sum(1 for _ in p.glob('plan-*.json') if _.is_file())
        a = Path(APPROVALS)
        approvals = 0
        if a.exists():
            approvals = sum(1 for _ in a.glob('plan-*.approved.json') if _.is_file())
        return jsonify({"status": "ok", "plans": plans, "approvals": approvals}), 200
    except Exception as exc:
        return jsonify({"status": "error", "error": str(exc)}), 500

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
    host = os.environ.get('MCP_DASH_HOST', '0.0.0.0')
    port = int(os.environ.get('MCP_DASH_PORT', '8080'))
    app.run(host=host, port=port)
