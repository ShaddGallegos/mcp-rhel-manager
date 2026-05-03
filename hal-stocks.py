#!/usr/bin/env python3
"""HAL Stocks: dedicated stock quote and watch tools.

This script provides a focused CLI for stock features while reusing
the stock implementation from hal-studio.py in-process.
"""

import argparse
import importlib.util
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Optional


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HAL_STUDIO = os.path.join(BASE_DIR, "hal-studio.py")
_HAL_STUDIO_MOD = None


def _load_hal_studio_module():
    global _HAL_STUDIO_MOD
    if _HAL_STUDIO_MOD is not None:
        return _HAL_STUDIO_MOD
    if not os.path.isfile(HAL_STUDIO):
        print(f"HAL Studio script not found: {HAL_STUDIO}", file=sys.stderr)
        return None

    spec = importlib.util.spec_from_file_location("hal_studio", HAL_STUDIO)
    if spec is None or spec.loader is None:
        print(f"Unable to load HAL Studio module from: {HAL_STUDIO}", file=sys.stderr)
        return None

    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _HAL_STUDIO_MOD = mod
    return _HAL_STUDIO_MOD


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _normalize_symbol(symbol: str) -> str:
    return (symbol or "").strip().upper()


def _validate_symbol(symbol: str) -> Optional[str]:
    if not symbol:
        return "Ticker symbol cannot be empty."
    if len(symbol) > 15:
        return "Ticker symbol is too long (max 15 characters)."
    if not re.match(r"^[A-Z0-9.\-^=]+$", symbol):
        return "Ticker symbol contains invalid characters."
    return None


def _get_quote_data(symbol: str) -> Optional[Dict[str, Any]]:
    mod = _load_hal_studio_module()
    if mod is None:
        return None
    try:
        return mod._fetch_quote(symbol)  # reuses existing quote/fallback logic
    except Exception:
        return None


def _ollama_generate(prompt: str, model: Optional[str] = None, timeout: int = 45) -> Optional[str]:
    ollama_url = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/") + "/api/generate"
    payload = {
        "model": model or os.environ.get("HAL_STOCKS_AI_MODEL", "qwen2.5-coder:7b"),
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


def _ai_stock_advice(symbol: str, quote: Dict[str, Any], events: Optional[list[Dict[str, Any]]] = None) -> Optional[str]:
    prompt = (
        "You are a stock monitoring assistant for operations users. "
        "Provide concise, practical monitoring guidance only. "
        "Do not provide investment advice. "
        "Return plain text only (no markdown/code fences) with: "
        "1) quick summary, 2) suggested alert thresholds, 3) next command examples. "
        "Command examples must be HAL-native only, using hal-stocks.py quote/watch/advisor.\n\n"
        f"Symbol: {symbol}\n"
        f"Quote JSON: {json.dumps(quote)}\n"
        f"Recent watch events JSON: {json.dumps(events or [])}\n"
    )
    text = _ollama_generate(prompt)
    if not text:
        return None
    cleaned = text.replace("```", "").strip()
    return cleaned


def _quote(symbol: str, json_output: bool, ai: bool) -> int:
    symbol = _normalize_symbol(symbol)
    err = _validate_symbol(symbol)
    if err:
        print(err, file=sys.stderr)
        return 2

    mod = _load_hal_studio_module()
    if mod is None:
        return 2

    if not json_output:
        rc = mod.stock_quote(symbol)
        if rc == 0 and ai:
            quote = _get_quote_data(symbol)
            if quote:
                advice = _ai_stock_advice(symbol, quote)
                if advice:
                    print("\nAI advisory")
                    print("-" * 40)
                    print(advice)
        return rc

    quote = _get_quote_data(symbol)
    if not quote:
        payload = {
            "ok": False,
            "symbol": symbol,
            "error": "quote_unavailable",
            "as_of": _utc_now(),
        }
        print(json.dumps(payload, indent=2))
        return 1

    payload = {
        "ok": True,
        "symbol": symbol,
        "name": quote.get("shortName") or quote.get("longName") or symbol,
        "price": quote.get("regularMarketPrice"),
        "previous_close": quote.get("regularMarketPreviousClose"),
        "change": quote.get("regularMarketChange"),
        "change_percent": quote.get("regularMarketChangePercent"),
        "market_state": quote.get("marketState", "UNKNOWN"),
        "currency": quote.get("currency", "USD"),
        "as_of": _utc_now(),
    }
    if ai:
        advice = _ai_stock_advice(symbol, quote)
        if advice:
            payload["ai_advisory"] = advice
    print(json.dumps(payload, indent=2))
    return 0


def _watch(symbol: str, above: Optional[float], below: Optional[float], interval: int, checks: int, once: bool, json_output: bool, ai: bool) -> int:
    symbol = _normalize_symbol(symbol)
    err = _validate_symbol(symbol)
    if err:
        print(err, file=sys.stderr)
        return 2

    if once:
        checks = 1

    if above is None and below is None:
        print("You must set --above and/or --below.", file=sys.stderr)
        return 2
    if interval <= 0:
        print("--interval must be > 0", file=sys.stderr)
        return 2
    if checks <= 0:
        print("--checks must be > 0", file=sys.stderr)
        return 2

    mod = _load_hal_studio_module()
    if mod is None:
        return 2

    if not json_output:
        rc = mod.stock_watch(symbol, above, below, interval, checks)
        if ai:
            quote = _get_quote_data(symbol)
            if quote:
                advice = _ai_stock_advice(symbol, quote)
                if advice:
                    print("\nAI advisory")
                    print("-" * 40)
                    print(advice)
        return rc

    events = []
    try:
        for idx in range(checks):
            quote = _get_quote_data(symbol)
            price = quote.get("regularMarketPrice") if quote else None
            event: Dict[str, Any] = {
                "check": idx + 1,
                "checks": checks,
                "symbol": symbol,
                "price": price,
                "as_of": _utc_now(),
                "hit": False,
                "direction": None,
            }

            if isinstance(price, (int, float)):
                if above is not None and price >= above:
                    event["hit"] = True
                    event["direction"] = "above"
                if below is not None and price <= below and not event["hit"]:
                    event["hit"] = True
                    event["direction"] = "below"
            else:
                event["error"] = "quote_unavailable"

            events.append(event)

            if event["hit"]:
                print(json.dumps({
                    "ok": True,
                    "triggered": True,
                    "symbol": symbol,
                    "thresholds": {"above": above, "below": below},
                    "event": event,
                    "events": events,
                    "ai_advisory": _ai_stock_advice(symbol, quote, events) if ai and quote else None,
                }, indent=2))
                return 0

            if idx + 1 < checks:
                time.sleep(interval)
    except KeyboardInterrupt:
        print(json.dumps({
            "ok": True,
            "triggered": False,
            "interrupted": True,
            "symbol": symbol,
            "thresholds": {"above": above, "below": below},
            "events": events,
        }, indent=2))
        return 0

    latest_quote = _get_quote_data(symbol)
    print(json.dumps({
        "ok": True,
        "triggered": False,
        "symbol": symbol,
        "thresholds": {"above": above, "below": below},
        "events": events,
        "ai_advisory": _ai_stock_advice(symbol, latest_quote, events) if ai and latest_quote else None,
    }, indent=2))
    return 1


def _advisor(symbol: str, json_output: bool) -> int:
    symbol = _normalize_symbol(symbol)
    err = _validate_symbol(symbol)
    if err:
        print(err, file=sys.stderr)
        return 2
    quote = _get_quote_data(symbol)
    if not quote:
        msg = "Unable to retrieve quote for AI advisory."
        if json_output:
            print(json.dumps({"ok": False, "symbol": symbol, "error": msg}, indent=2))
        else:
            print(msg)
        return 1

    advice = _ai_stock_advice(symbol, quote)
    if json_output:
        print(json.dumps({
            "ok": advice is not None,
            "symbol": symbol,
            "quote": quote,
            "ai_advisory": advice,
        }, indent=2))
    else:
        print("AI stock advisory")
        print("-" * 40)
        if advice:
            print(advice)
        else:
            print("AI advisor unavailable. Verify local Ollama is running and model is present.")
    return 0 if advice else 1


def _stock_menu() -> int:
    while True:
        print("\nHAL Stocks")
        print("-" * 40)
        print("1) Stock quote")
        print("2) Stock watch alert")
        print("3) Exit")
        choice = input("Choose an option: ").strip()

        if choice == "1":
            symbol = input("Ticker symbol (example: AAPL): ").strip()
            rc = _quote(symbol, json_output=False, ai=False)
            if rc != 0:
                print(f"Command failed (exit {rc})")
        elif choice == "2":
            symbol = input("Ticker symbol: ").strip()
            above_s = input("Alert when price >= (blank to skip): ").strip()
            below_s = input("Alert when price <= (blank to skip): ").strip()
            interval_s = input("Interval seconds [60]: ").strip() or "60"
            checks_s = input("Checks [30]: ").strip() or "30"

            above = float(above_s) if above_s else None
            below = float(below_s) if below_s else None
            rc = _watch(symbol, above, below, int(interval_s), int(checks_s), once=False, json_output=False, ai=False)
            if rc != 0:
                print(f"Command failed (exit {rc})")
        elif choice == "3":
            return 0
        else:
            print("Invalid option")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="HAL dedicated stock tools")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("menu", help="Launch stock-only interactive menu")

    quote = sub.add_parser("quote", help="Get stock quote")
    quote.add_argument("symbol")
    quote.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    quote.add_argument("--ai", action="store_true", help="Include AI advisory in output")

    advisor = sub.add_parser("advisor", help="AI-only advisory for a symbol")
    advisor.add_argument("symbol")
    advisor.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    watch = sub.add_parser("watch", help="Watch stock price thresholds")
    watch.add_argument("symbol")
    watch.add_argument("--above", type=float)
    watch.add_argument("--below", type=float)
    watch.add_argument("--interval", type=int, default=60)
    watch.add_argument("--checks", type=int, default=30)
    watch.add_argument("--once", action="store_true", help="Single check only (equivalent to --checks 1)")
    watch.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    watch.add_argument("--ai", action="store_true", help="Include AI advisory in output")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command in (None, "menu"):
        return _stock_menu()

    if args.command == "quote":
        return _quote(args.symbol, json_output=args.json, ai=args.ai)

    if args.command == "advisor":
        return _advisor(args.symbol, json_output=args.json)

    if args.command == "watch":
        return _watch(
            args.symbol,
            args.above,
            args.below,
            args.interval,
            args.checks,
            once=args.once,
            json_output=args.json,
            ai=args.ai,
        )

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
