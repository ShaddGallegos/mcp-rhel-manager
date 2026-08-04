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
MAX_CONCURRENT_JOBS = int(os.environ.get('MCP_MAX_CONCURRENT_JOBS', '2'))
RESULT_TTL_SEC = int(os.environ.get('MCP_RESULT_TTL_SEC', '3600'))
MAX_STORED_RESULTS = int(os.environ.get('MCP_MAX_STORED_RESULTS', '1000'))

if FastAPI is None:
    # Fallback descriptive message when deps are missing
    print('Note: fastapi/uvicorn/aiohttp not available. This is a scaffold file.')


class InferenceRequest(BaseModel):
    prompt: str
    task: str = 'default'
    metadata: Dict | None = None


app = FastAPI(title='MCP Inference Queue')

# In-memory job/result stores — replace with Redis or persistent store for
# production use.
job_results: Dict[str, Dict] = {}
queue: 'asyncio.Queue[tuple[str, str, str, Dict | None]]' = asyncio.Queue()
_job_lock = asyncio.Lock()
_worker_semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
queue_stats: Dict[str, float | int] = {
    'submitted_total': 0,
    'completed_total': 0,
    'failed_total': 0,
    'last_latency_sec': 0.0,
}


def _select_model(task: str) -> str:
    """Select model from env by task profile.

    Uses:
      MCP_MODEL_<TASK>
      MCP_MODEL_DEFAULT
      OLLAMA_MODEL
    """
    key = f"MCP_MODEL_{(task or 'default').strip().upper()}"
    return os.environ.get(key, os.environ.get('MCP_MODEL_DEFAULT', os.environ.get('OLLAMA_MODEL', '')))


async def _call_model(prompt: str, task: str = 'default') -> str:
    """Call the configured bridge (OLLAMA_URL) and return assistant text."""
    payload = {
        'model': _select_model(task),
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


async def _trim_results_if_needed() -> None:
    """Bound in-memory result size/age to avoid unbounded growth."""
    now = time.time()
    # Remove stale results first
    stale_ids = []
    for job_id, data in list(job_results.items()):
        done_ts = float(data.get('finished_at', 0.0) or 0.0)
        if done_ts and now - done_ts > RESULT_TTL_SEC:
            stale_ids.append(job_id)
    for job_id in stale_ids:
        job_results.pop(job_id, None)

    # Bound total entries by oldest timestamp
    if len(job_results) > MAX_STORED_RESULTS:
        items = sorted(job_results.items(), key=lambda kv: float(kv[1].get('created_at', 0.0) or 0.0))
        for job_id, _ in items[: max(0, len(items) - MAX_STORED_RESULTS)]:
            job_results.pop(job_id, None)


async def _process_job(prompt: str, task: str, job_id: str, metadata: Dict | None = None) -> None:
    async with _worker_semaphore:
        started = time.time()
        async with _job_lock:
            entry = job_results.get(job_id, {})
            entry.update({'ok': None, 'state': 'running', 'started_at': started})
            job_results[job_id] = entry
        try:
            out = await _call_model(prompt, task=task)
            finished = time.time()
            async with _job_lock:
                job_results[job_id] = {
                    'ok': True,
                    'state': 'done',
                    'task': task,
                    'response': out,
                    'created_at': job_results.get(job_id, {}).get('created_at', started),
                    'started_at': started,
                    'finished_at': finished,
                    'latency_sec': finished - started,
                    'metadata': metadata or {},
                }
                queue_stats['completed_total'] += 1
                queue_stats['last_latency_sec'] = finished - started
        except Exception as e:
            finished = time.time()
            async with _job_lock:
                job_results[job_id] = {
                    'ok': False,
                    'state': 'failed',
                    'task': task,
                    'error': str(e),
                    'created_at': job_results.get(job_id, {}).get('created_at', started),
                    'started_at': started,
                    'finished_at': finished,
                    'latency_sec': finished - started,
                    'metadata': metadata or {},
                }
                queue_stats['failed_total'] += 1
                queue_stats['last_latency_sec'] = finished - started


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
        for prompt, task, job_id, metadata in batch:
            asyncio.create_task(_process_job(prompt, task, job_id, metadata))
        await _trim_results_if_needed()


@app.on_event('startup')
async def startup_event():
    asyncio.create_task(worker_loop())


@app.post('/v1/infer')
async def infer(req: InferenceRequest):
    job_id = uuid.uuid4().hex
    created = time.time()
    async with _job_lock:
        job_results[job_id] = {
            'ok': None,
            'state': 'queued',
            'task': req.task,
            'created_at': created,
            'metadata': req.metadata or {},
        }
        queue_stats['submitted_total'] += 1
    await queue.put((req.prompt, req.task, job_id, req.metadata))
    return {'job_id': job_id}


@app.get('/v1/result/{job_id}')
async def get_result(job_id: str):
    if job_id not in job_results:
        raise HTTPException(status_code=404, detail='job_id not found')
    return job_results[job_id]


@app.get('/v1/stats')
async def get_stats():
    running = 0
    queued = 0
    done = 0
    failed = 0
    for v in job_results.values():
        st = str(v.get('state', ''))
        if st == 'queued':
            queued += 1
        elif st == 'running':
            running += 1
        elif st == 'done':
            done += 1
        elif st == 'failed':
            failed += 1
    return {
        'queue_size': queue.qsize(),
        'running': running,
        'queued': queued,
        'done': done,
        'failed': failed,
        'totals': queue_stats,
        'max_concurrent_jobs': MAX_CONCURRENT_JOBS,
    }


if __name__ == '__main__':
    # Simple local run: `python3 scripts/mcp_inference_queue.py`
    if FastAPI is None:
        print('Missing runtime dependencies: fastapi/uvicorn/aiohttp. Install them with: pip install fastapi uvicorn aiohttp')
        raise SystemExit(1)
    uvicorn.run('scripts.mcp_inference_queue:app', host='0.0.0.0', port=PORT, reload=False)
