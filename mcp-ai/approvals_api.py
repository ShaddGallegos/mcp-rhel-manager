#!/usr/bin/env python3
"""Approvals API and helper for writing approval artifacts.

Provides a simple `write_approval(payload)` function and a tiny HTTP
API to list and create approvals. Designed to be lightweight and
importable from `mcp-ai/dashboard.py`.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import time
from pathlib import Path
from typing import Any, Dict

try:
    from flask import Flask, jsonify, request
    _FLASK_AVAILABLE = True
except Exception:
    Flask = None
    jsonify = lambda x: x
    request = None
    _FLASK_AVAILABLE = False

logging.basicConfig(level=logging.INFO)

AI_HOME = os.path.expanduser("~/.mcp-ai")
APPROVALS = os.path.join(AI_HOME, "approvals")
os.makedirs(APPROVALS, exist_ok=True)


def _normalize_plan_stem(plan: str) -> str:
    if not plan:
        return f"plan-{int(time.time())}"
    return Path(plan).stem


def write_approval(payload: Dict[str, Any]) -> str:
    """Write approval payload to the approvals directory and return path.

    Payload should include at least a `plan` key (filename or stem).
    """
    plan = payload.get("plan") or payload.get("file") or ""
    plan_stem = _normalize_plan_stem(str(plan))
    outpath = os.path.join(APPROVALS, f"{plan_stem}.approved.json")
    payload_out = dict(payload)
    payload_out.setdefault("approved_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    payload_out.setdefault("host", socket.gethostname())
    tmp = outpath + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload_out, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, outpath)
    logging.info("Wrote approval %s", outpath)
    return outpath


app = Flask(__name__) if _FLASK_AVAILABLE else None


def api_approve_impl():
    data = request.get_json(silent=True) or {}
    try:
        path = write_approval(data)
        return jsonify({"ok": True, "path": path})
    except Exception as e:
        logging.exception("Failed to write approval")
        return jsonify({"ok": False, "error": str(e)}), 500


def api_list_approvals_impl():
    out = []
    for p in sorted(Path(APPROVALS).glob("plan-*.approved.json")):
        try:
            with p.open("r", encoding="utf-8") as fh:
                out.append(json.load(fh))
        except Exception:
            out.append({"file": p.name})
    return jsonify(out)


def api_health_impl():
    return jsonify({"ok": True, "approvals": len(list(Path(APPROVALS).glob("plan-*.approved.json")))})


if _FLASK_AVAILABLE and app is not None:
    @app.route("/api/approve", methods=["POST"])
    def api_approve():
        return api_approve_impl()

    @app.route("/api/approvals", methods=["GET"])
    def api_list_approvals():
        return api_list_approvals_impl()

    @app.route("/api/health")
    def api_health():
        return api_health_impl()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("MCP_APPROVALS_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MCP_APPROVALS_PORT", "8001")))
    args = parser.parse_args()
    app.run(host=args.host, port=args.port)
if __name__ == "__main__":
    main()
