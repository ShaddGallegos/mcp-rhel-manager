#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
while true; do
  curl -s -X POST http://localhost:1776/api/chat -H "Content-Type: application/json" -d '{
    "model": "qwen2.5-coder:7b",
    "messages": [
      {
        "role": "system", 
        "content": "You are the Architect Sentinel. 1. Run predict_failure_and_evacuate. 2. Run optimize_ai_performance. 3. Run sentinel_scan. 4. If all good, respond SYSTEM_EVOLVING."
      },
      {"role": "user", "content": "Execute maintenance."}
    ]
  }' >> "${SCRIPT_DIR}/evolution.log"
  sleep 3600
done
