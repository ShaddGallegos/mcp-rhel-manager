#!/usr/bin/env python3
"""HAL Multimedia: dedicated image/video/audio/OBS/office tools.

This script routes all non-stock HAL Studio features through `hal-studio.py`
so multimedia workflows are available in a separate entry-point.
"""

import argparse
import json
import os
import urllib.error
import urllib.request
import subprocess
import sys


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HAL_STUDIO = os.path.join(BASE_DIR, "hal-studio.py")

MULTIMEDIA_COMMANDS = {
    "menu",
    "capabilities",
    "image-describe",
    "image-resize",
    "image-poster",
    "text-image",
    "ai-image",
    "text-music",
    "text-speech",
    "music-vocals",
    "pipeline",
    "video-info",
    "video-trim",
    "video-build",
    "obs-status",
    "obs-launch",
    "obs-recordings",
    "obsbot-status",
    "obsbot-controls",
    "obsbot-set",
    "lo-convert",
}


def _ollama_generate(prompt: str, model: str | None = None, timeout: int = 60) -> str | None:
    ollama_url = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/") + "/api/generate"
    payload = {
        "model": model or os.environ.get("HAL_MULTIMEDIA_AI_MODEL", "qwen2.5-coder:7b"),
        "stream": False,
        "prompt": prompt,
    }
    req = urllib.request.Request(
        ollama_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            text = (body.get("response") or "").strip()
            return text or None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def _coerce_plan_json(raw_text: str) -> dict | None:
    try:
        obj = json.loads(raw_text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass

    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(raw_text[start:end + 1])
            return obj if isinstance(obj, dict) else None
        except Exception:
            pass

    repair_prompt = (
        "Convert the following text into STRICT JSON only with schema "
        "{\"summary\": string, \"commands\": [string], \"notes\": [string]}. "
        "No markdown, no prose, JSON object only.\n\n"
        f"TEXT:\n{raw_text}"
    )
    repaired = _ollama_generate(repair_prompt)
    if not repaired:
        return None
    try:
        obj = json.loads(repaired)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _ai_plan(goal: str, execute_first: bool = False, json_output: bool = False) -> int:
    prompt = (
        "You are HAL multimedia workflow planner. "
        "Given a user goal, return strict JSON with schema: "
        "{\"summary\": string, \"commands\": [string], \"notes\": [string]}. "
        "Commands must start with one of these hal-studio commands only: "
        f"{sorted(MULTIMEDIA_COMMANDS)}. "
        "Do not include stock-* commands."
        f"\nUser goal: {goal}\n"
    )
    text = _ollama_generate(prompt)
    if not text:
        print("AI planner unavailable. Ensure local Ollama is running.", file=sys.stderr)
        return 1

    plan = _coerce_plan_json(text)

    if not isinstance(plan, dict):
        print("AI planner returned invalid format.", file=sys.stderr)
        return 1

    commands = plan.get("commands") if isinstance(plan.get("commands"), list) else []
    safe_commands = []
    for cmd in commands:
        if not isinstance(cmd, str):
            continue
        parts = cmd.strip().split()
        if not parts:
            continue
        if parts[0] in MULTIMEDIA_COMMANDS:
            safe_commands.append(parts)

    result = {
        "summary": plan.get("summary"),
        "commands": [" ".join(p) for p in safe_commands],
        "notes": plan.get("notes") if isinstance(plan.get("notes"), list) else [],
    }

    if json_output:
        print(json.dumps(result, indent=2))
    else:
        print("AI multimedia plan")
        print("-" * 40)
        if result["summary"]:
            print(result["summary"])
            print()
        if result["commands"]:
            print("Suggested commands:")
            for idx, cmd in enumerate(result["commands"], start=1):
                print(f"{idx}. python3 hal-multimedia.py {cmd}")
        else:
            print("No executable commands produced.")
        if result["notes"]:
            print("\nNotes:")
            for note in result["notes"]:
                print(f"- {note}")

    if execute_first and safe_commands:
        return _run_hal_studio(safe_commands[0])
    return 0


def _run_hal_studio(args_list: list[str]) -> int:
    if not os.path.isfile(HAL_STUDIO):
        print(f"HAL Studio script not found: {HAL_STUDIO}", file=sys.stderr)
        return 2
    proc = subprocess.run([sys.executable, HAL_STUDIO] + args_list)
    return proc.returncode


def _multimedia_menu() -> int:
    # Delegate to the full multimedia menu in hal-studio.py
    return _run_hal_studio(["menu"])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="HAL dedicated multimedia tools (image/video/audio/OBS/office)"
    )
    parser.add_argument(
        "studio_args",
        nargs=argparse.REMAINDER,
        help="Arguments forwarded to hal-studio.py (excluding stock-* commands)",
    )
    parser.add_argument("--ai-plan", metavar="GOAL", help="Generate AI multimedia command plan from a natural-language goal")
    parser.add_argument("--execute-first", action="store_true", help="Execute first AI-planned command")
    parser.add_argument("--json", action="store_true", help="Print AI plan as JSON (with --ai-plan)")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.ai_plan:
        return _ai_plan(args.ai_plan, execute_first=args.execute_first, json_output=args.json)

    if not args.studio_args:
        return _multimedia_menu()

    forward = list(args.studio_args)
    if forward and forward[0] == "--":
        forward = forward[1:]

    if not forward:
        return _multimedia_menu()

    cmd = forward[0]
    if cmd.startswith("stock-"):
        print("Stock commands are handled by hal-stocks.py", file=sys.stderr)
        return 2

    if cmd not in MULTIMEDIA_COMMANDS:
        print(f"Unsupported multimedia command: {cmd}", file=sys.stderr)
        print("Use hal-stocks.py for stock-only commands.", file=sys.stderr)
        return 2

    return _run_hal_studio(forward)


if __name__ == "__main__":
    sys.exit(main())
