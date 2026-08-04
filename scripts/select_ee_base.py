#!/usr/bin/env python3
"""Select execution-environment base image based on NVIDIA GPU presence.

Usage: python3 scripts/select_ee_base.py [--yes]

This will update `path/to/execution-environment.yml`, backing up the
original to a timestamped .bak file.
"""
import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone

EE_PATH = os.path.join(os.path.dirname(__file__), '..', 'path', 'to', 'execution-environment.yml')

def has_nvidia_gpu():
    # Check common signs of NVIDIA GPU
    if shutil_which('nvidia-smi'):
        return True
    if os.path.exists('/dev/nvidia0'):
        return True
    try:
        out = subprocess.run(['lspci'], capture_output=True, text=True, check=False)
        if 'NVIDIA' in out.stdout or 'NVIDIA' in out.stderr:
            return True
    except Exception:
        pass
    return False

def shutil_which(name):
    from shutil import which
    return which(name)

def prompt_default(prompt, default=True):
    yes = 'Y' if default else 'y'
    no = 'n' if default else 'N'
    full = f"{prompt} [{yes}/{no}] "
    resp = input(full).strip().lower()
    if resp == '':
        return default
    return resp in ('y', 'yes')

def update_base_image(new_image):
    ee = os.path.abspath(EE_PATH)
    if not os.path.exists(ee):
        print(f"error: execution environment file not found: {ee}")
        return 2
    # backup
    bak = ee + '.bak.' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    with open(ee, 'r', encoding='utf-8') as fh:
        data = fh.read()
    with open(bak, 'w', encoding='utf-8') as fh:
        fh.write(data)
    # naive but robust replacement: replace the first occurrence of a line
    # that starts with '    name:' under images.base_image. We'll do a
    # simple stateful parse to preserve formatting.
    lines = data.splitlines()
    out = []
    state = 'root'
    replaced = False
    for ln in lines:
        stripped = ln.lstrip()
        if state == 'root' and stripped.startswith('images:'):
            state = 'in_images'
            out.append(ln)
            continue
        if state == 'in_images' and stripped.startswith('base_image:'):
            state = 'in_base'
            out.append(ln)
            continue
        if state == 'in_base' and stripped.startswith('name:') and not replaced:
            indent = ln[:len(ln)-len(stripped)]
            out.append(f"{indent}name: {new_image}")
            replaced = True
            continue
        # if we find another top-level section, exit specialized states
        if state in ('in_images','in_base') and (not ln.startswith(' ') and not ln.startswith('\t')):
            state = 'root'
        out.append(ln)
    if not replaced:
        print('warning: did not find images.base_image.name line to replace; file left unchanged')
        return 3
    with open(ee, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out) + '\n')
    print(f"Updated {ee} -> base image: {new_image}; backup saved to {bak}")
    return 0

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--yes', '-y', action='store_true', help='Assume yes to prompts')
    args = p.parse_args()

    gpu = has_nvidia_gpu()
    if gpu:
        default_image = 'nvcr.io/nvidia/pytorch:23.11-py3'
        prompt = f'NVIDIA GPU detected. Use NVIDIA base image {default_image}?'
    else:
        default_image = 'registry.redhat.io/ubi10/ubi:10.0'
        prompt = f'No NVIDIA GPU detected. Use UBI10 base image {default_image}?'

    choice = args.yes or prompt_default(prompt, default=gpu)
    if not choice:
        print('Aborted by user; no changes made.')
        return 0
    rc = update_base_image(default_image)
    return rc

if __name__ == '__main__':
    sys.exit(main())
