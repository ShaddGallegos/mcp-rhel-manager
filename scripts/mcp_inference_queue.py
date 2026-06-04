#!/usr/bin/env python3
"""
MCP Inference Queue — lightweight scaffold service

This file provides a simple HTTP API that accepts inference requests and
processes them via a small background worker. It's intended as a starting
point for integrating a queue + batching layer in front of a local LLM
runtime (Ollama/LocalAI/etc.).

Notes:
- Configure the model bridge URL via the `OLLAMA_URL` environment variable.
- This is a scaffold: adapt batching strategy, auth, persistence, and
  production hardening as needed.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Dict

try:
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel
    import uvicorn
    import aiohttp
except Exception:
    # The module is a scaffold; at import time we don't fail hard so tooling
    # that performs syntax checks can still validate the file.
    FastAPI = None  # type: ignore


OLLAMA_URL = os.environ.get('OLLAMA_URL', 'http://localhost:1776/api/chat')
PORT = int(os.environ.get('MCP_INFERENCE_PORT', '8080'))
WORKER_BATCH_SIZE = int(os.environ.get('MCP_BATCH_SIZE', '4'))
WORKER_BATCH_TIMEOUT = float(os.environ.get('MCP_BATCH_TIMEOUT', '0.5'))

if FastAPI is None:
    # Fallback descriptive message when deps are missing
    print('Note: fastapi/uvicorn/aiohttp not available. This is a scaffold file.')


class InferenceRequest(BaseModel):
    prompt: str
    metadata: Dict | None = None


app = FastAPI(title='MCP Inference Queue')

# In-memory job/result stores — replace with Redis or persistent store for
# production use.
job_results: Dict[str, Dict] = {}
queue: 'asyncio.Queue[tuple[str, str]]' = asyncio.Queue()


async def _call_model(prompt: str) -> str:
    """Call the configured bridge (OLLAMA_URL) and return assistant text."""
    payload = {
        'model': os.environ.get('OLLAMA_MODEL', ''),
        'messages': [{'role': 'user', 'content': prompt}],
        'stream': False,
    }
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        async with sess.post(OLLAMA_URL, json=payload) as resp:
            data = await resp.json()
            # Accept common wrapper formats
            if isinstance(data, dict):
                if 'message' in data:
                    return data['message'].get('content', '')
                if 'choices' in data and data['choices']:
                    return data['choices'][0]['message'].get('content', '')
                if 'response' in data:
                    return data['response']
            return str(data)


async def worker_loop():
    """Background worker that processes queued prompts."""
    while True:
        # Gather up to WORKER_BATCH_SIZE items or wait for timeout
        batch = []
        try:
            first = await asyncio.wait_for(queue.get(), timeout=WORKER_BATCH_TIMEOUT)
            batch.append(first)
        except asyncio.TimeoutError:
            await asyncio.sleep(0.1)
            continue

        start = time.time()
        while len(batch) < WORKER_BATCH_SIZE and (time.time() - start) < WORKER_BATCH_TIMEOUT:
            try:
                item = queue.get_nowait()
                batch.append(item)
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.01)

        # Process batch sequentially (scaffold). Replace with batched calls
        # if your bridge supports it.
        for prompt, job_id in batch:
            try:
                out = await _call_model(prompt)
                job_results[job_id] = {'ok': True, 'response': out}
            except Exception as e:
                job_results[job_id] = {'ok': False, 'error': str(e)}
            finally:
                # mark done is not necessary for simple asyncio.Queue
                pass


@app.on_event('startup')
async def startup_event():
    asyncio.create_task(worker_loop())


@app.post('/v1/infer')
async def infer(req: InferenceRequest):
    job_id = uuid.uuid4().hex
    job_results[job_id] = {'ok': None, 'state': 'queued'}
    await queue.put((req.prompt, job_id))
    return {'job_id': job_id}


@app.get('/v1/result/{job_id}')
async def get_result(job_id: str):
    if job_id not in job_results:
        raise HTTPException(status_code=404, detail='job_id not found')
    return job_results[job_id]


if __name__ == '__main__':
    # Simple local run: `python3 scripts/mcp_inference_queue.py`
    if FastAPI is None:
        print('Missing runtime dependencies: fastapi/uvicorn/aiohttp. Install them with: pip install fastapi uvicorn aiohttp')
        raise SystemExit(1)
    uvicorn.run('scripts.mcp_inference_queue:app', host='0.0.0.0', port=PORT, reload=False)
