#!/usr/bin/env python3
import os, json
home = os.environ.get("HOME", os.path.expanduser("~"))
cfg_file = os.path.join(home, ".mcp-ai", "dashboard_config.json")
try:
    with open(cfg_file, "r") as fh:
        old = json.load(fh)
except Exception:
    old = {}
new = {"features": {}}
if isinstance(old, dict):
    if "enable_ai_features" in old:
        new["features"].setdefault("ai", {})["enabled"] = bool(old.get("enable_ai_features"))
    if "enable_realtime_stats" in old:
        new["features"].setdefault("realtime", {})["enabled"] = bool(old.get("enable_realtime_stats"))
    if "enable_chat" in old:
        new["features"].setdefault("ai", {}).setdefault("chat", {})["enabled"] = bool(old.get("enable_chat"))
    if "features" in old and isinstance(old["features"], dict):
        for k, v in old["features"].items():
            new["features"].setdefault(k, {})
            if isinstance(v, dict):
                new["features"][k].update(v)
            else:
                new["features"][k]["enabled"] = v
r = new["features"].setdefault("redhat", {})
r.setdefault("insights", {})["enabled"] = True
r["insights"].setdefault("auto_collect", True)
r.setdefault("compliance", {})["enabled"] = True
r["compliance"].setdefault("auto_scan", True)
os.makedirs(os.path.dirname(cfg_file), exist_ok=True)
with open(cfg_file, "w") as fh:
    json.dump(new, fh, indent=2)
print("UPDATED")
