#!/usr/bin/env python3
"""Integration test: verify LLM client failover across endpoints."""
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)

from llm_client import LLMClient


class BadHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.send_response(500)
        self.end_headers()

    def log_message(self, *args):
        return


class GoodHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        _ = self.rfile.read(length) if length else b''
        resp = {'ok': True, 'from': 'good'}
        data = json.dumps(resp).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        return


def run_server(port, handler):
    srv = HTTPServer(('127.0.0.1', port), handler)
    srv.serve_forever()


def main():
    bad_port = 18083
    good_port = 18084
    t1 = threading.Thread(target=run_server, args=(bad_port, BadHandler), daemon=True)
    t2 = threading.Thread(target=run_server, args=(good_port, GoodHandler), daemon=True)
    t1.start(); t2.start()

    endpoints = [f'http://127.0.0.1:{bad_port}/api/chat', f'http://127.0.0.1:{good_port}/api/chat']
    client = LLMClient(endpoints=endpoints)
    payload = {'model': 'test', 'messages': [{'role': 'user', 'content': 'ping'}], 'stream': False}
    try:
        resp = client.call_with_failover(payload)
        print('Failover response:', resp)
        print('PASS')
        return 0
    except Exception as e:
        print('FAIL:', e)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
