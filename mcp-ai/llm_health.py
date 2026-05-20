#!/usr/bin/env python3
"""Simple health HTTP server for the LLM client.

Exposes GET /health which returns JSON with endpoint health checks.
"""
from __future__ import annotations

import json
import os
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)

from llm_client import get_client


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != '/health':
            self.send_response(404)
            self.end_headers()
            return
        client = get_client()
        try:
            h = client.health()
            body = json.dumps({'ok': True, 'endpoints': h}).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            body = json.dumps({'ok': False, 'error': str(e)}).encode('utf-8')
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)


def run_server(port: int = 18082):
    srv = HTTPServer(('127.0.0.1', port), HealthHandler)
    print('LLM health server listening on port', port)
    srv.serve_forever()


if __name__ == '__main__':
    p = __import__('argparse').ArgumentParser()
    p.add_argument('--port', type=int, default=int(os.environ.get('LLM_HEALTH_PORT', '18082')))
    args = p.parse_args()
    run_server(port=args.port)
