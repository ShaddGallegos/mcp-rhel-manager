#!/usr/bin/env python3
"""Render quick_start.md from Jinja2 template using an OS profile."""

from __future__ import annotations

import argparse
import platform
import sys
from pathlib import Path


def detect_os_profile() -> str:
    system = platform.system().lower()
    if system == 'darwin':
        return 'macos'
    if system != 'linux':
        return 'ubuntu'

    release = Path('/etc/os-release')
    text = release.read_text(encoding='utf-8', errors='ignore').lower() if release.exists() else ''
    if 'fedora' in text:
        return 'fedora'
    if 'rhel' in text or 'red hat' in text or 'centos' in text or 'rocky' in text or 'alma' in text:
        return 'rhel'
    if 'debian' in text:
        return 'debian'
    if 'ubuntu' in text:
        return 'ubuntu'
    if 'microsoft' in text or 'wsl' in text:
        return 'wsl'
    return 'ubuntu'


def profile_context(os_profile: str) -> dict[str, str]:
    mapping = {
        'rhel': ('dnf', 'systemd'),
        'fedora': ('dnf', 'systemd'),
        'ubuntu': ('apt-get', 'systemd'),
        'debian': ('apt-get', 'systemd'),
        'wsl': ('apt-get', 'systemd'),
        'macos': ('brew', 'launchd'),
    }
    package_manager, service_manager = mapping.get(os_profile, ('apt-get', 'systemd'))
    return {
        'os_profile': os_profile,
        'package_manager': package_manager,
        'service_manager': service_manager,
    }


def _current_profile(output_path: Path) -> str | None:
    try:
        if not output_path.exists():
            return None
        for line in output_path.read_text(encoding='utf-8', errors='ignore').splitlines()[:30]:
            if line.strip().lower().startswith('- os profile:'):
                return line.split(':', 1)[1].strip().lower()
    except Exception:
        return None
    return None


def render(template_path: Path, output_path: Path, context: dict[str, str]) -> None:
    try:
        from jinja2 import Environment, FileSystemLoader, StrictUndefined
    except Exception as exc:
        raise RuntimeError('Jinja2 is required. Install with: pip install jinja2') from exc

    env = Environment(
        loader=FileSystemLoader(str(template_path.parent)),
        undefined=StrictUndefined,
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template(template_path.name)
    output_path.write_text(template.render(**context) + '\n', encoding='utf-8')


def main() -> int:
    parser = argparse.ArgumentParser(description='Render quick_start.md from templates/quick_start.md.j2')
    parser.add_argument('--os', default='auto', choices=['auto', 'rhel', 'fedora', 'ubuntu', 'debian', 'macos', 'wsl'], help='Target OS profile')
    parser.add_argument('--template', default='templates/quick_start.md.j2', help='Path to input Jinja2 template')
    parser.add_argument('--output', default='quick_start.md', help='Path to rendered markdown output')
    parser.add_argument('--only-if-mismatch', action='store_true', help='Skip rendering when output already matches the target OS profile')
    parser.add_argument('--quiet', action='store_true', help='Suppress informational output')
    args = parser.parse_args()

    os_profile = detect_os_profile() if args.os == 'auto' else args.os
    template_path = Path(args.template).resolve()
    output_path = Path(args.output).resolve()

    if not template_path.exists():
        print(f'Template not found: {template_path}', file=sys.stderr)
        return 2

    if args.only_if_mismatch:
        current = _current_profile(output_path)
        if current == os_profile and output_path.exists():
            if not args.quiet:
                print(f'quick_start already matches profile: {os_profile}')
            return 0

    context = profile_context(os_profile)
    render(template_path, output_path, context)
    if not args.quiet:
        print(f'Rendered {output_path} for profile: {os_profile}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
