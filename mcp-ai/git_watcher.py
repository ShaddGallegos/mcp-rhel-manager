#!/usr/bin/env python3
"""Lightweight Git repository watcher.

Polls configured roots for additions/removals of `.git` directories and
changes to `.gitignore` files. Designed to avoid external dependencies and
run as a long-lived background task or foreground daemon.

It writes a small state file to `~/.mcp-ai/git_watch.json` and calls an
optional callback on detected events.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

DEFAULT_INTERVAL = 30.0
STATE_FILE = Path(os.path.expanduser('~')) / '.mcp-ai' / 'git_watch.json'


def _scan_roots(roots: Iterable[Path]) -> Tuple[Set[str], Dict[str, float]]:
    gits: Set[str] = set()
    gitignores: Dict[str, float] = {}
    for root in roots:
        if not root.exists():
            continue
        # walk up to a reasonable depth
        for p in root.rglob('.git'):
            if p.is_dir():
                try:
                    gits.add(str(p.parent.resolve()))
                except Exception:
                    gits.add(str(p.parent))

        for gi in root.rglob('.gitignore'):
            try:
                gitignores[str(gi.resolve())] = gi.stat().st_mtime
            except Exception:
                try:
                    gitignores[str(gi)] = gi.stat().st_mtime
                except Exception:
                    pass

    return gits, gitignores


class GitWatcher:
    def __init__(self, roots: Optional[List[Path]] = None, interval: float = DEFAULT_INTERVAL,
                 callback: Optional[Callable[[Dict], None]] = None, state_file: Optional[Path] = None):
        self.roots = roots or [Path.cwd(), Path.home() / 'GIT']
        self.interval = float(interval)
        self.callback = callback
        self.state_file = state_file or STATE_FILE
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_gits: Set[str] = set()
        self._last_gitignores: Dict[str, float] = {}

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name='git-watcher', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)

    def _emit(self, event: Dict) -> None:
        # write to state file
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.state_file, 'w', encoding='utf-8') as fh:
                json.dump({'timestamp': time.time(), 'event': event}, fh, indent=2)
        except Exception:
            pass

        if self.callback:
            try:
                self.callback(event)
            except Exception:
                pass

    def _run(self):
        # initial snapshot
        try:
            gits, gitignores = _scan_roots(self.roots)
            self._last_gits = gits
            self._last_gitignores = gitignores
        except Exception:
            gits, gitignores = set(), {}

        while not self._stop_event.wait(self.interval):
            try:
                cur_gits, cur_gitignores = _scan_roots(self.roots)
                added = sorted(list(cur_gits - self._last_gits))
                removed = sorted(list(self._last_gits - cur_gits))

                gitignore_changed: List[str] = []
                for path, mtime in cur_gitignores.items():
                    last = self._last_gitignores.get(path)
                    if last is None or mtime > last:
                        gitignore_changed.append(path)

                if added or removed or gitignore_changed:
                    event = {
                        'added_repos': added,
                        'removed_repos': removed,
                        'gitignore_changed': gitignore_changed,
                        'roots': [str(r) for r in self.roots],
                    }
                    self._emit(event)

                self._last_gits = cur_gits
                self._last_gitignores = cur_gitignores
            except Exception:
                # swallow errors and continue
                pass


def start_watch(roots: Optional[List[str]] = None, interval: float = DEFAULT_INTERVAL,
                callback: Optional[Callable[[Dict], None]] = None, state_file: Optional[str] = None) -> GitWatcher:
    root_paths = [Path(p).expanduser() if isinstance(p, str) else p for p in (roots or [Path.cwd(), Path.home() / 'GIT'])]
    gw = GitWatcher(roots=root_paths, interval=interval, callback=callback, state_file=Path(state_file) if state_file else None)
    gw.start()
    return gw


if __name__ == '__main__':
    import argparse

    def _print_event(ev: Dict):
        print('GitWatcher event:')
        for k, v in ev.items():
            print(f'  {k}: {v}')

    p = argparse.ArgumentParser(prog='git_watcher')
    p.add_argument('--roots', help='Comma-separated roots to watch', default='')
    p.add_argument('--interval', type=float, default=DEFAULT_INTERVAL)
    args = p.parse_args()

    roots = [Path(r) for r in args.roots.split(',')] if args.roots else None
    gw = start_watch(roots=roots, interval=args.interval, callback=_print_event)
    try:
        print('git_watcher running (press Ctrl-C to stop)')
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print('\nStopping git_watcher...')
        gw.stop()
        print('Stopped')
