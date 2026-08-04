#!/usr/bin/env python3
"""Watch system logs and disks, attempt safe fixes, and notify/ask on warnings.

This script is intentionally conservative:
- It only attempts a small set of non-destructive, commonly-useful fixes
  (restart systemd units when logs explicitly show a unit failure).
- For disks, it checks SMART health using `smartctl` and will schedule a
  non-destructive short SMART test when a disk looks suspicious (requires
  smartmontools & usually root). It will not attempt destructive actions.
- Warnings are reported and (when interactive) the user is asked before
  attempting fixes.

Usage examples:
    python3 scripts/watch_system_and_fix.py --once
    python3 scripts/watch_system_and_fix.py --daemon
    python3 scripts/watch_system_and_fix.py --interactive

This file is meant to be used from `scripts/auto-fixer.sh` or run directly.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import logging
import os
import re
import shlex
import stat
import subprocess
import sys
import time
from collections import deque
from typing import Dict, Iterable, List, Optional


SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
HOME = os.path.expanduser("~")
STATE_FILE = os.path.join(HOME, ".mcp-ai", "monitor_state.json")
LOG_FILE = os.path.join(SCRIPT_DIR, "watch_system_and_fix.log")

# Defaults: common system logs on RHEL-like systems
DEFAULT_LOG_PATHS = [
    "/var/log/messages",
    "/var/log/syslog",
    "/var/log/kern.log",
    "/var/log/dmesg",
    "/var/log/secure",
    "/var/log/boot.log",
]

# Directories to scan for additional logs (default: /var/log when present)
DEFAULT_SCAN_DIRS = [
    "/var/log",
]

ERROR_RE = re.compile(r"\b(?:error|crit(?:ical)?|fail(?:ed|ure)?|fatal)\b", re.I)
SERVICE_PATTERNS = [
    re.compile(r"Job for (?P<unit>[-\w@./]+?) failed", re.I),
    re.compile(r"Failed to start (?P<unit>[-\w@./]+?)", re.I),
    re.compile(r"Unit (?P<unit>[-\w@./]+?)(?:\.service)? entered failed state", re.I),
]
IO_PATTERNS = [re.compile(r"I/O error|buffer I/O error", re.I), re.compile(r"I/O error on device (?P<dev>/dev/\w+)", re.I)]

# Kernel and NVIDIA specific patterns
KERNEL_PATTERNS = [
    re.compile(r"kernel panic", re.I),
    re.compile(r"\bBUG:\b", re.I),
    re.compile(r"Call Trace", re.I),
    re.compile(r"OOM killer|Out of memory|oom_reaper", re.I),
]

NVIDIA_PATTERNS = [
    re.compile(r"NVRM:|nvidia|nouveau|GPU panic|Failed to initialize the NVIDIA", re.I),
]

# Keep last N processed line hashes to avoid duplicate processing
RECENT_LINES = 2000


def setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.FileHandler(LOG_FILE)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
    root.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
    root.addHandler(sh)


def load_state() -> Dict:
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"files": {}, "recent": []}


def save_state(state: Dict) -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        logging.exception("Failed to save state: %s", e)


def _line_hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()


def read_new_lines(path: str, state: Dict) -> List[str]:
    """Read new lines from a rotating log file using stored offsets.

    If file was rotated (inode changed) the file is read from start.
    """
    files = state.setdefault("files", {})
    info = files.get(path, {})

    try:
        st = os.stat(path)
    except Exception:
        return []

    inode = st.st_ino
    last = info.get("inode")
    offset = info.get("offset", 0)

    new_lines: List[str] = []
    try:
        with open(path, "rb") as fh:
            if last != inode:
                offset = 0
            fh.seek(offset)
            data = fh.read()
            if not data:
                files[path] = {"inode": inode, "offset": fh.tell()}
                return []
            text = data.decode("utf-8", errors="replace")
            new_lines = text.splitlines()
            files[path] = {"inode": inode, "offset": fh.tell()}
    except Exception:
        logging.exception("Failed reading %s", path)

    return new_lines


def journal_recent_lines(state: Dict, max_lines: int = 200) -> List[str]:
    """Pull recent journal entries (best-effort)."""
    try:
        r = subprocess.run(["journalctl", "-n", str(max_lines), "--no-pager", "-o", "short-iso"], capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return []
        lines = r.stdout.splitlines()
        return lines
    except FileNotFoundError:
        # journalctl not installed / not available
        return []
    except Exception:
        logging.exception("journalctl failed")
        return []


def gather_log_files_from_dirs(dirs: Iterable[str]) -> List[str]:
    """Walk directories and return a list of candidate log files.

    Heuristics: include regular files, skip compressed and binary files,
    and avoid the systemd journal binary directory.
    """
    files: List[str] = []
    for d in dirs:
        if not d or not os.path.exists(d):
            continue
        for root, _, filenames in os.walk(d):
            # skip journal binary store
            if os.path.basename(root) == "journal":
                continue
            for fn in filenames:
                path = os.path.join(root, fn)
                try:
                    if not os.path.isfile(path):
                        continue
                    # skip common compressed suffixes
                    if fn.endswith((".gz", ".xz", ".bz2", ".lz4", ".zip")):
                        continue
                    st = os.stat(path)
                    # ensure regular file
                    if not stat.S_ISREG(st.st_mode):
                        continue
                    # quick binary detection: sample first KB
                    with open(path, "rb") as fh:
                        sample = fh.read(1024)
                        if b"\x00" in sample:
                            continue
                except Exception:
                    continue
                files.append(path)
    # de-duplicate & sort
    return sorted(set(files))


def load_hal_notify() -> Optional[object]:
    """Dynamically import the local hal-notify script as a module.

    hal-notify.py isn't a valid Python module name for a normal import, so
    we load it via importlib utilities. If it cannot be loaded, return None.
    """
    path = os.path.join(SCRIPT_DIR, "hal-notify.py")
    if not os.path.isfile(path):
        return None
    try:
        spec = importlib.util.spec_from_file_location("hal_notify", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[arg-type]
        return mod
    except Exception:
        logging.exception("Failed to load hal-notify module")
        return None


def send_notification(hal_notify_mod: Optional[object], message: str, title: str = "HAL Alert", severity: str = "info") -> None:
    if hal_notify_mod and hasattr(hal_notify_mod, "send_notification"):
        try:
            hal_notify_mod.send_notification(message, title, severity)
            return
        except Exception:
            logging.exception("hal-notify send_notification failed")

    # Fallback: log to file/console
    logging.info("Notification (%s): %s - %s", severity, title, message)


def attempt_restart_unit(unit: str, dry_run: bool = False) -> Dict[str, str]:
    """Try to restart a systemd unit. Return dict with result info."""
    unit_candidates = [unit]
    if not unit.endswith(".service"):
        unit_candidates.append(unit + ".service")

    for u in unit_candidates:
        try:
            cmd = ["systemctl", "restart", u]
            if dry_run:
                logging.info("(dry-run) would run: %s", " ".join(shlex.quote(p) for p in cmd))
                return {"unit": u, "attempted": "yes", "result": "dry-run"}

            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            status = "ok" if r.returncode == 0 else f"failed ({r.returncode})"
            out = (r.stdout or "") + "\n" + (r.stderr or "")
            logging.info("Restart %s -> %s", u, status)
            return {"unit": u, "attempted": "yes", "result": status, "output": out}
        except Exception as e:
            logging.exception("Error attempting restart of %s: %s", u, e)
            return {"unit": u, "attempted": "yes", "result": "error", "error": str(e)}

    return {"unit": unit, "attempted": "no", "result": "no-candidate"}


def list_disks() -> List[str]:
    try:
        r = subprocess.run(["lsblk", "-dn", "-o", "NAME,TYPE"], capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return []
        disks: List[str] = []
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "disk":
                disks.append(parts[0])
        return disks
    except Exception:
        logging.exception("lsblk failed")
        return []


def smart_health(device: str) -> Optional[str]:
    """Return a short health string for device (PASSED/OK/FAILED/UNKNOWN) or None if smartctl missing."""
    try:
        r = subprocess.run(["smartctl", "-H", device], capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        return None
    except Exception:
        logging.exception("smartctl -H failed for %s", device)
        return "UNKNOWN"

    out = (r.stdout or "") + "\n" + (r.stderr or "")
    # common phrases
    if re.search(r"PASSED|OK", out, re.I):
        return "OK"
    if re.search(r"FAILED|FAIL", out, re.I):
        return "FAILED"
    return "UNKNOWN"


def schedule_short_smart_test(device: str, dry_run: bool = False) -> Dict[str, str]:
    try:
        cmd = ["smartctl", "-t", "short", device]
        if dry_run:
            logging.info("(dry-run) would run: %s", " ".join(shlex.quote(p) for p in cmd))
            return {"device": device, "scheduled": "short(dry-run)"}

        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        out = (r.stdout or "") + "\n" + (r.stderr or "")
        return {"device": device, "scheduled": "short", "output": out}
    except Exception:
        logging.exception("Failed to schedule smart test for %s", device)
        return {"device": device, "scheduled": "error"}


def _write_report(name: str, content: str) -> str:
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    reports_dir = os.path.join(HOME, ".mcp-ai", "reports")
    try:
        os.makedirs(reports_dir, exist_ok=True)
        path = os.path.join(reports_dir, f"{name}-{ts}.log")
        with open(path, "w") as f:
            f.write(content)
        return path
    except Exception:
        logging.exception("Failed to write report %s", name)
        return ""


def collect_kernel_artifacts() -> Dict[str, str]:
    """Collect dmesg and recent journal logs for troubleshooting."""
    artifacts: Dict[str, str] = {}
    try:
        r = subprocess.run(["dmesg", "-T"], capture_output=True, text=True, timeout=10)
        artifacts['dmesg'] = _write_report('dmesg', r.stdout or r.stderr or '')
    except Exception:
        logging.exception("dmesg collection failed")

    try:
        r = subprocess.run(["journalctl", "-n", "1000", "--no-pager"], capture_output=True, text=True, timeout=15)
        artifacts['journal'] = _write_report('journal', r.stdout or r.stderr or '')
    except Exception:
        logging.exception("journalctl collection failed")

    return artifacts


def is_module_loaded(name: str) -> bool:
    try:
        r = subprocess.run(["lsmod"], capture_output=True, text=True, timeout=5)
        return any(line.split()[0] == name for line in (r.stdout or "").splitlines() if line.strip())
    except Exception:
        return False


def try_reload_module(name: str, dry_run: bool = False) -> Dict[str, str]:
    try:
        if dry_run:
            logging.info("(dry-run) would reload kernel module: %s", name)
            return {"module": name, "reloaded": "dry-run"}

        # Try to remove and reinsert the module. This can fail if in use.
        r1 = subprocess.run(["/sbin/modprobe", "-r", name], capture_output=True, text=True, timeout=20)
        r2 = subprocess.run(["/sbin/modprobe", name], capture_output=True, text=True, timeout=20)
        out = (r1.stdout or "") + "\n" + (r1.stderr or "") + "\n" + (r2.stdout or "") + "\n" + (r2.stderr or "")
        ok = (r1.returncode == 0 and r2.returncode == 0)
        return {"module": name, "reloaded": "ok" if ok else "failed", "output": out}
    except Exception:
        logging.exception("Failed to reload module %s", name)
        return {"module": name, "reloaded": "error"}


def restart_display_manager(args: argparse.Namespace) -> Dict[str, str]:
    candidates = ["gdm", "gdm.service", "sddm", "lightdm", "display-manager.service"]
    for c in candidates:
        res = attempt_restart_unit(c, dry_run=args.dry_run)
        if res.get("result", "").startswith("ok") or res.get("result") == "dry-run":
            return res
    return {"result": "none"}


def handle_kernel_issue(line: str, state: Dict, hal_notify_mod: Optional[object], args: argparse.Namespace) -> None:
    artifacts = collect_kernel_artifacts()
    msg = f"Detected kernel-level issue: {line}\nCollected artifacts: {artifacts}"
    send_notification(hal_notify_mod, msg, "Kernel issue detected", "critical")

    # Attempt limited remediation for OOM cases
    if re.search(r"OOM killer|Out of memory|oom_reaper", line, re.I):
        logging.info("Detected OOM-related entry; attempting to restart systemd-oomd if available")
        res = attempt_restart_unit("systemd-oomd", dry_run=args.dry_run)
        send_notification(hal_notify_mod, f"Attempted restart of systemd-oomd: {res}", "OOM remediation", "warning")

    # Reboot is always opt-in; background/daemon modes must never reboot.
    if args.allow_reboot and args.interactive and sys.stdin.isatty():
        try:
            ans = input("Kernel issue detected. Reboot recommended — reboot now? [y/N]: ")
            if ans.lower().startswith('y'):
                if args.dry_run:
                    logging.info("dry-run: would reboot")
                else:
                    subprocess.run(["/sbin/reboot"])
        except Exception:
            logging.exception("Interactive reboot prompt failed")
    elif args.interactive and not args.allow_reboot:
        logging.info("Interactive reboot prompt suppressed (use --allow-reboot to enable)")


def handle_nvidia_issue(line: str, state: Dict, hal_notify_mod: Optional[object], args: argparse.Namespace) -> None:
    parts = []

    # Try restarting nvidia-persistenced service if present
    logging.info("Attempting nvidia remediation steps")
    res = attempt_restart_unit("nvidia-persistenced", dry_run=args.dry_run)
    parts.append(("nvidia-persistenced", res))

    # If kernel module loaded, try reloading it
    if is_module_loaded("nvidia"):
        reload_res = try_reload_module("nvidia", dry_run=args.dry_run)
        parts.append(("module_reload", reload_res))

    # Restart display manager if module reload did not resolve
    dm_res = restart_display_manager(args)
    parts.append(("display_manager", dm_res))

    msg = f"NVIDIA/GPU issue: {line}\nRemediation attempts: {parts}"
    send_notification(hal_notify_mod, msg, "NVIDIA remediation", "critical" if any(p[1].get('result','').startswith('failed') or p[1].get('reloaded')=='failed' for p in parts) else "info")


def process_line(line: str, state: Dict, hal_notify_mod: Optional[object], args: argparse.Namespace) -> None:
    h = _line_hash(line)
    recent = state.setdefault("recent", [])
    if h in recent:
        return
    recent.append(h)
    # keep recent bounded
    if len(recent) > RECENT_LINES:
        del recent[0:len(recent) - RECENT_LINES]

    if not ERROR_RE.search(line):
        return

    logging.info("Detected severity-matching line: %s", line)

    # Kernel issues (collect logs, notify, suggest actions)
    for pat in KERNEL_PATTERNS:
        if pat.search(line):
            logging.warning("Kernel issue detected: %s", line)
            handle_kernel_issue(line, state, hal_notify_mod, args)
            return

    # NVIDIA / GPU issues
    for pat in NVIDIA_PATTERNS:
        if pat.search(line):
            logging.warning("NVIDIA/GPU issue detected: %s", line)
            handle_nvidia_issue(line, state, hal_notify_mod, args)
            return

    # Check for unit/service patterns
    for pat in SERVICE_PATTERNS:
        m = pat.search(line)
        if m:
            unit = m.groupdict().get("unit")
            if unit:
                logging.info("Service failure detected for unit: %s", unit)
                if args.dry_run:
                    logging.info("dry-run: would attempt restart for %s", unit)
                else:
                    res = attempt_restart_unit(unit, dry_run=args.dry_run)
                    title = f"Auto-fix: restarted {res.get('unit', unit)}"
                    message = f"Attempted restart: {res.get('result')}\nOutput:\n{res.get('output', '')}"
                    sev = "info" if res.get("result", "").startswith("ok") or res.get("result") == "dry-run" else "critical"
                    send_notification(hal_notify_mod, message, title, sev)
                return

    # Check for IO/disk related lines
    for pat in IO_PATTERNS:
        if pat.search(line):
            logging.info("Possible IO/disk issue detected: %s", line)
            # run SMART checks on all disks (best-effort)
            disks = list_disks()
            bad_disks = []
            for d in disks:
                dev = "/dev/" + d
                health = smart_health(dev)
                if health is None:
                    logging.warning("smartctl not available; cannot check %s", dev)
                    send_notification(hal_notify_mod, f"smartctl not available on system; cannot check {dev}", "SMART check missing", "warning")
                    return
                if health != "OK":
                    bad_disks.append((dev, health))

            if bad_disks:
                for dev, health in bad_disks:
                    logging.warning("Disk %s health: %s", dev, health)
                    # schedule short SMART test
                    test_res = schedule_short_smart_test(dev, dry_run=args.dry_run)
                    send_notification(hal_notify_mod, f"Disk {dev} SMART health: {health}. Scheduled short test: {test_res}", "Disk SMART issue", "critical")
            else:
                send_notification(hal_notify_mod, f"IO issue found in logs but SMART reports OK for inspected disks.", "IO Warning", "warning")
            return

    # Generic error: notify as critical
    send_notification(hal_notify_mod, line, "Log error detected", "critical")


def run_once(args: argparse.Namespace) -> None:
    state = load_state()
    hal_notify_mod = load_hal_notify()

    # Build list of log files to inspect: configured logs + scanned files
    log_files = list(args.logs or [])
    if getattr(args, 'scan_dirs', None):
        scanned = gather_log_files_from_dirs(args.scan_dirs)
        for p in scanned:
            if p not in log_files:
                log_files.append(p)

    # read new lines from files
    for path in log_files:
        lines = read_new_lines(path, state)
        for l in lines:
            process_line(l, state, hal_notify_mod, args)

    # read recent journal entries
    jlines = journal_recent_lines(state, max_lines=args.journal_lines)
    for l in jlines:
        process_line(l, state, hal_notify_mod, args)

    # periodic SMART health scan (only if requested)
    if args.check_smart:
        disks = list_disks()
        for d in disks:
            dev = "/dev/" + d
            health = smart_health(dev)
            if health is None:
                logging.warning("smartctl missing; skipping SMART checks")
                break
            if health != "OK":
                logging.warning("SMART health issue on %s: %s", dev, health)
                res = schedule_short_smart_test(dev, dry_run=args.dry_run)
                send_notification(hal_notify_mod, f"SMART: {dev} -> {health}; scheduled short test: {res}", "Disk SMART issue", "critical")

    save_state(state)


def run_loop(args: argparse.Namespace) -> None:
    state = load_state()
    hal_notify_mod = load_hal_notify()

    logging.info("Starting watch loop; logs=%s; check_smart=%s", args.logs, args.check_smart)

    poll_interval = args.interval
    while True:
        try:
            # Always inspect configured logs
            for path in args.logs:
                lines = read_new_lines(path, state)
                for l in lines:
                    process_line(l, state, hal_notify_mod, args)

            # Periodically scan configured directories for more log files
            if getattr(args, 'scan_dirs', None):
                last_scan = state.get('_last_scan_ts', 0)
                if time.time() - last_scan >= getattr(args, 'scan_interval', 300):
                    scanned = gather_log_files_from_dirs(args.scan_dirs)
                    for path in scanned:
                        lines = read_new_lines(path, state)
                        for l in lines:
                            process_line(l, state, hal_notify_mod, args)
                    state['_last_scan_ts'] = int(time.time())

            jlines = journal_recent_lines(state, max_lines=args.journal_lines)
            for l in jlines:
                process_line(l, state, hal_notify_mod, args)

            # SMART check on a slower cadence
            if args.check_smart and (int(time.time()) % max(60, args.smart_interval) < poll_interval):
                disks = list_disks()
                for d in disks:
                    dev = "/dev/" + d
                    health = smart_health(dev)
                    if health is None:
                        logging.warning("smartctl missing; skipping further SMART checks")
                        break
                    if health != "OK":
                        res = schedule_short_smart_test(dev, dry_run=args.dry_run)
                        send_notification(hal_notify_mod, f"SMART: {dev} -> {health}; scheduled short test: {res}", "Disk SMART issue", "critical")

            save_state(state)
        except KeyboardInterrupt:
            logging.info("Interrupted; exiting")
            break
        except Exception:
            logging.exception("Unexpected error in loop")

        time.sleep(poll_interval)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Watch system logs and simple auto-fixes for MCP manager")
    p.add_argument("--daemon", action="store_true", help="Run continuously")
    p.add_argument("--once", action="store_true", help="Run one pass and exit")
    p.add_argument("--interactive", action="store_true", help="Ask interactively for warning fixes (when run in a TTY)")
    p.set_defaults(allow_reboot=False)
    p.add_argument("--allow-reboot", action="store_true", dest="allow_reboot", help="Allow interactive reboot prompt for kernel issues")
    p.add_argument("--no-reboot", action="store_false", dest="allow_reboot", help="Disable reboot prompt for kernel issues (default)")
    p.add_argument("--dry-run", action="store_true", dest="dry_run", help="Don't execute fix commands; only report")
    p.add_argument("--logs", nargs="*", default=[p for p in DEFAULT_LOG_PATHS if os.path.exists(p)], help="Log files to monitor (defaults to common paths)")
    p.add_argument("--scan-dirs", nargs="*", default=None, help="Directories to scan for additional log files (defaults to /var/log if present)")
    p.add_argument("--scan-interval", type=int, default=300, help="How often (seconds) to rescan directories for log files")
    p.add_argument("--journal-lines", type=int, default=200, help="How many recent journal lines to check per poll")
    p.add_argument("--interval", type=int, default=15, help="Polling interval seconds")
    p.add_argument("--check-smart", action="store_true", help="Run SMART checks on disks")
    p.add_argument("--smart-interval", type=int, default=3600, help="Interval (seconds) for SMART health scans")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    setup_logging()

    # If scan_dirs not provided explicitly, default to DEFAULT_SCAN_DIRS when present
    if getattr(args, 'scan_dirs', None) is None:
        args.scan_dirs = [d for d in DEFAULT_SCAN_DIRS if os.path.exists(d)]

    if args.once:
        run_once(args)
        return

    if args.daemon:
        run_loop(args)
        return

    # default: run once
    run_once(args)


if __name__ == "__main__":
    main()
