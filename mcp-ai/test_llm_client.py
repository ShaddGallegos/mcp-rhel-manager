#!/usr/bin/env python3
"""Quick smoke test for llm_client using a mock HTTP endpoint.

Starts a simple HTTP server that responds to POST /api/chat and then
uses `mcp-ai/llm_client.py` to call it. Prints the response and exits.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

HERE = os.path.dirname(__file__)
# Ensure module dir on sys.path so `import llm_client` finds our module
sys.path.insert(0, HERE)

import llm_client

PORT = 18081

class MockHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length) if length else b''
        try:
            payload = json.loads(body.decode('utf-8'))
        except Exception:
            payload = {'raw': body.decode('utf-8', 'ignore')}
        # Simple echo-style response to simulate an LLM bridge
        resp = {'id': 'mock-1', 'object': 'chat.completion', 'choices': [{'message': {'content': json.dumps({'reply': 'ok', 'echo': payload})}}]}
        data = json.dumps(resp).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        return


def run_server():
    server = HTTPServer(('127.0.0.1', PORT), MockHandler)
    print('Mock server starting on port', PORT)
    server.serve_forever()


def main():
    t = threading.Thread(target=run_server, daemon=True)
    t.start()

    client = llm_client.get_client()
    # Use the mock endpoint directly
    ep = f'http://127.0.0.1:{PORT}/api/chat'
    payload = {'model': 'test-model', 'messages': [{'role': 'user', 'content': 'hello'}], 'stream': False}
    try:
        print('Calling mock endpoint via client.call')
        txt = client.call(payload, endpoint=ep)
        print('Response text:', txt)
    except Exception as e:
        print('Call failed:', e)
        sys.exit(2)

    print('Success')

if __name__ == '__main__':
    main()
