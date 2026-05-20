#!/usr/bin/env python3
"""Simple MCP bridge that proxies requests to local Ollama.

Runs on port 1776 and forwards chat requests to Ollama (port 11434).
This allows HAL to communicate with Ollama through a standard MCP interface.

Usage:
    python3 mcp-ai/bridge.py              # Start bridge on port 1776
    python3 mcp-ai/bridge.py --port 8888  # Custom port
    python3 mcp-ai/bridge.py --help       # Show options
"""
from __future__ import annotations

import argparse
import json
import sys
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.request import urlopen, Request
from urllib.error import URLError
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('MCPBridge')

OLLAMA_URL = 'http://localhost:11434/api/chat'
DEFAULT_PORT = 1776


class MCPBridgeHandler(BaseHTTPRequestHandler):
    """HTTP handler that proxies MCP requests to Ollama."""

    def log_message(self, format, *args):
        """Override to use our logger."""
        logger.info(format % args)

    def do_POST(self):
        """Handle POST requests from HAL."""
        if self.path != '/api/chat':
            self.send_error(404, 'Only /api/chat endpoint supported')
            return

        try:
            content_len = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_len).decode('utf-8')
            request_data = json.loads(body)
            
            # Forward to Ollama (or configured LLM endpoints) using pooled HTTP client when available
            logger.debug(f'Forwarding request: {request_data.get("model", "unknown")}')
            payload = request_data
            try:
                # Import the new centralized client if present (module lives next to this file)
                import llm_client
                client = llm_client.get_client()
                # Stream response from upstream and forward chunks to HAL
                upstream = client.call_with_failover(payload, stream=True, timeout=300)
                status = getattr(upstream, 'status_code', 200)
                ctype = upstream.headers.get('Content-Type', 'application/json') if hasattr(upstream, 'headers') else 'application/json'
                self.send_response(status)
                self.send_header('Content-Type', ctype)
                # Do not set Content-Length for streaming responses
                self.end_headers()
                for chunk in upstream.iter_content(chunk_size=8192):
                    if not chunk:
                        continue
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except BrokenPipeError:
                        break
                logger.debug('Streamed response sent to HAL')
            except Exception as e:
                # Fallback to raw urllib behavior if llm_client isn't available or fails
                logger.debug('llm_client unavailable or failed, falling back to urllib: %s', e)
                req = Request(
                    OLLAMA_URL,
                    data=body.encode('utf-8'),
                    headers={'Content-Type': 'application/json'}
                )
                with urlopen(req, timeout=300) as resp:
                    # Buffer entire response
                    response_data = b''
                    while True:
                        chunk = resp.read(8192)
                        if not chunk:
                            break
                        response_data += chunk
                    # Send complete response back to HAL
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', len(response_data))
                    self.end_headers()
                    self.wfile.write(response_data)
                    logger.debug('Response sent to HAL (urllib fallback)')
                
        except json.JSONDecodeError:
            self.send_error(400, 'Invalid JSON in request body')
            logger.error('Invalid JSON received')
        except URLError as e:
            self.send_error(503, f'Ollama unavailable: {e}')
            logger.error(f'Ollama error: {e}')
        except Exception as e:
            self.send_error(500, f'Internal server error: {e}')
            logger.error(f'Unexpected error: {e}')

    def do_GET(self):
        """Handle health check requests."""
        if self.path == '/health':
            ollama_ok = True
            ollama_status = 'ok'
            try:
                with urlopen('http://localhost:11434/api/tags', timeout=3) as resp:
                    if resp.status != 200:
                        ollama_ok = False
                        ollama_status = f'http_{resp.status}'
            except Exception as e:
                ollama_ok = False
                ollama_status = str(e)

            self.send_response(200 if ollama_ok else 503)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            payload = {
                'status': 'ok' if ollama_ok else 'degraded',
                'bridge': 'up',
                'ollama': ollama_status,
            }
            self.wfile.write(json.dumps(payload).encode('utf-8'))
        elif self.path == '/api/chat':
            self.send_error(405, 'Use POST for /api/chat')
        else:
            self.send_error(404, 'Only /health and /api/chat endpoints available')


def main():
    global OLLAMA_URL
    
    ap = argparse.ArgumentParser(
        description='MCP Bridge — proxies requests from HAL to local Ollama'
    )
    ap.add_argument(
        '--port', type=int, default=DEFAULT_PORT,
        help=f'Port to listen on (default: {DEFAULT_PORT})'
    )
    ap.add_argument(
        '--host', default='localhost',
        help='Host to bind to (default: localhost)'
    )
    ap.add_argument(
        '--ollama-url', default=OLLAMA_URL,
        help=f'Ollama API URL (default: {OLLAMA_URL})'
    )
    ap.add_argument(
        '--verbose', '-v', action='store_true',
        help='Verbose logging'
    )
    
    args = ap.parse_args()
    
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    
    # Update global Ollama URL
    OLLAMA_URL = args.ollama_url
    
    # Start server
    server = ThreadingHTTPServer((args.host, args.port), MCPBridgeHandler)
    
    logger.info(f'MCP Bridge listening on http://{args.host}:{args.port}')
    logger.info(f'Forwarding requests to {args.ollama_url}')
    logger.info('Press Ctrl+C to stop')
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info('Bridge stopped by user')
        sys.exit(0)


if __name__ == '__main__':
    main()
