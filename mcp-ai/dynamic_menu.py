#!/usr/bin/env python3
"""Simple dynamic menu helper for operator choice prompts.

Provides functions to render a numbered menu to the terminal and return the
selected option(s). Can be used by HAL dashboard or CLI workflows to present
multiple actionable suggestions returned by LLMs.
"""
from __future__ import annotations

import sys
from typing import List, Optional


def choose(options: List[str], prompt: str = 'Select an option', multi: bool = False) -> Optional[List[int]]:
    """Display options and return selected index(es).

    Returns list of selected indices (0-based) or None on abort.
    If multi is False, returns a single-index list.
    """
    if not options:
        return None
    print(prompt + ':')
    for i, o in enumerate(options, start=1):
        print(f'  {i}) {o}')
    print('  0) Cancel')
    try:
        if multi:
            s = input('Enter comma-separated choices (e.g. 1,3): ').strip()
            if not s or s == '0':
                return None
            parts = [p.strip() for p in s.split(',') if p.strip()]
            idxs = []
            for p in parts:
                n = int(p)
                if n == 0:
                    return None
                if 1 <= n <= len(options):
                    idxs.append(n - 1)
            return idxs
        else:
            s = input('Enter choice number: ').strip()
            if not s or s == '0':
                return None
            n = int(s)
            if 1 <= n <= len(options):
                return [n - 1]
            return None
    except (KeyboardInterrupt, EOFError):
        print('\nCancelled')
        return None
    except Exception:
        print('Invalid selection')
        return None


if __name__ == '__main__':
    # Simple demo
    opts = ['Install package', 'Create file', 'Remove file', 'Restart service']
    sel = choose(opts, prompt='Choose an action', multi=False)
    if sel is None:
        print('No selection')
        sys.exit(1)
    print('You selected:', opts[sel[0]])
*** End Patch