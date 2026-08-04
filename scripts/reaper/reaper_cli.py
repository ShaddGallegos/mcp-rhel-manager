#!/usr/bin/env python3
"""Reaper CLI - non-GUI wrapper to run SocialReaper jobs from HAL.

Provides commands to list available sources/functions and run jobs in a
non-interactive, scriptable way. Intended for local/offline use only.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Optional

try:
    import yaml
except Exception:  # pragma: no cover - best-effort import
    yaml = None

try:
    import socialreaper
    from socialreaper import tools as sr_tools
except Exception:
    socialreaper = None
    sr_tools = None

REPO_ROOT = Path(__file__).resolve().parents[1]
REAPER_DIR = Path(__file__).resolve().parent
SOURCES_XML = REAPER_DIR / "sources.xml"

ANSIBLE_ENV = Path.home() / ".ansible" / "conf" / "env.yml"
ANSIBLE_VAULT_PASS = Path.home() / ".ansible" / "conf" / ".vaultpass.txt"


def load_keys_from_ansible(env_path: Path = ANSIBLE_ENV, vault_pass: Path = ANSIBLE_VAULT_PASS) -> Dict:
    """Load a dict of keys from an Ansible env.yml file.

    If the file is encrypted with ansible-vault, we attempt to decrypt it
    using the provided vault password file. The function returns a plain
    mapping (empty on error).
    """
    if not env_path.exists():
        return {}

    raw = env_path.read_text(encoding="utf-8")
    if raw.lstrip().startswith("$ANSIBLE_VAULT") or "ANSIBLE_VAULT" in raw:
        if not vault_pass.exists():
            print(f"Vault password file not found: {vault_pass}", file=sys.stderr)
            return {}
        cmd = ["ansible-vault", "view", str(env_path), "--vault-password-file", str(vault_pass)]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            print("ansible-vault view failed:", p.stderr, file=sys.stderr)
            return {}
        raw = p.stdout

    if yaml is None:
        print("PyYAML not available; cannot parse ansible env.yml", file=sys.stderr)
        return {}

    try:
        data = yaml.safe_load(raw)
        if not isinstance(data, dict):
            return {}
        # The keys may be nested; return mapping as-is and let callers look up
        return data
    except Exception as e:
        print(f"Failed to parse env.yml: {e}", file=sys.stderr)
        return {}


def parse_sources(sources_xml: Path = SOURCES_XML) -> Dict[str, str]:
    """Return mapping of source name -> xml filename."""
    if not sources_xml.exists():
        return {}
    root = ET.parse(sources_xml).getroot()
    out = {}
    for src in root.findall("source"):
        name = src.find("name").text
        loc = src.find("location").text
        out[name] = loc
    return out


def list_sources():
    srcs = parse_sources()
    for name, loc in srcs.items():
        print(f"{name}: {loc}")


def list_functions(source_name: str):
    srcs = parse_sources()
    loc = srcs.get(source_name)
    if not loc:
        print(f"Unknown source: {source_name}", file=sys.stderr)
        return
    fpath = (REAPER_DIR / "sources" / loc).resolve()
    if not fpath.exists():
        print(f"Source xml not found: {fpath}", file=sys.stderr)
        return
    root = ET.parse(fpath).getroot()
    children = root.find("children")
    if children is None:
        print("No functions found.")
        return
    for node in children.findall("node"):
        nm = node.find("name").text
        fn = node.find("function").text
        desc = node.find("description")
        desc_text = desc.text if desc is not None else ""
        print(f"{nm} -> {fn} -- {desc_text}")


def run_job(source_name: str, function_name: str, primary: Optional[str] = None,
            kwargs: Optional[Dict] = None, keys: Optional[Dict] = None,
            output: Optional[str] = None, append: bool = False):
    """Run a single job via socialreaper and write CSV output."""
    if socialreaper is None:
        print("socialreaper package not installed. Install with: pip install socialreaper", file=sys.stderr)
        return 2

    # Load keys for this source if provided mapping contains them
    keys_for_source = {}
    if keys:
        # keys may be nested by source name or be a flat dict
        if isinstance(keys.get(source_name), dict):
            keys_for_source = keys[source_name]
        else:
            # fallback: use keys as-is
            keys_for_source = keys

    # Instantiate source
    source_cls = None
    for attr in (source_name, source_name.title(), source_name.lower()):
        if hasattr(socialreaper, attr):
            source_cls = getattr(socialreaper, attr)
            break
    if source_cls is None:
        # last resort: try eval (mirrors upstream Reaper behaviour)
        try:
            source = eval(f"socialreaper.{source_name}(**{keys_for_source})")
        except Exception as e:
            print(f"Failed to create source instance: {e}", file=sys.stderr)
            return 2
    else:
        try:
            source = source_cls(**(keys_for_source or {}))
        except Exception as e:
            print(f"Failed to instantiate {source_name}: {e}", file=sys.stderr)
            return 2

    # Obtain callable
    if not hasattr(source, function_name):
        print(f"Source does not expose function: {function_name}", file=sys.stderr)
        return 2
    func = getattr(source, function_name)

    # Build iterator
    try:
        if primary is not None:
            if kwargs:
                iterator = func(primary, **kwargs)
            else:
                iterator = func(primary)
        else:
            if kwargs:
                iterator = func(**kwargs)
            else:
                iterator = func()
    except Exception as e:
        print(f"Failed to call function: {e}", file=sys.stderr)
        return 2

    # Ensure output path
    out_path = Path(output or (Path.cwd() / "output" / f"{source_name}_{function_name}.csv"))
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Use socialreaper.tools.CSV to write iterator to CSV (best-effort)
    try:
        if sr_tools is None:
            print("socialreaper.tools not available; cannot write CSV", file=sys.stderr)
            return 2
        # sr_tools.CSV consumes an iterator
        sr_tools.CSV(iterator, file_name=str(out_path), flat=False, append=append, key_column=None, encoding="utf-8", fill_gaps=False)
        print(f"Saved -> {out_path}")
        return 0
    except Exception as e:
        print(f"Failed to write CSV: {e}", file=sys.stderr)
        return 2


def batch_run(source_name: str, function_name: str, input_file: Path, column: Optional[str], **kw):
    """Run jobs for a list of primaries read from a CSV or text file."""
    primaries = []
    if input_file.suffix.lower() in (".csv", ".txt"):
        import csv

        with open(input_file, newline="", encoding="utf-8") as f:
            if input_file.suffix.lower() == ".csv":
                reader = csv.DictReader(f)
                if column and column in reader.fieldnames:
                    primaries = [row[column].strip() for row in reader if row.get(column)]
                else:
                    # fallback to first column
                    for row in reader:
                        primaries.append(next(iter(row.values())).strip())
            else:
                for line in f:
                    if line.strip():
                        primaries.append(line.strip())
    else:
        raise SystemExit("Unsupported input file type")

    rc = 0
    for p in primaries:
        rc = run_job(source_name, function_name, primary=p, **kw) or rc
    return rc


def build_parser():
    ap = argparse.ArgumentParser(description="Reaper CLI wrapper (non-GUI)")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("list-sources", help="List available sources from sources.xml")

    lf = sub.add_parser("list-functions", help="List functions for a source")
    lf.add_argument("source")

    runp = sub.add_parser("run", help="Run a single job")
    runp.add_argument("source")
    runp.add_argument("function")
    runp.add_argument("--primary", help="Primary input (e.g. username or query)")
    runp.add_argument("--kwargs", help="JSON string of kwargs to pass to function")
    runp.add_argument("--use-ansible-keys", action="store_true", help="Load API keys from ~/.ansible/conf/env.yml (supports ansible-vault)")
    runp.add_argument("--output", help="Output CSV path")
    runp.add_argument("--append", action="store_true", help="Append to output CSV if exists")

    br = sub.add_parser("batch", help="Run jobs from CSV/text file")
    br.add_argument("source")
    br.add_argument("function")
    br.add_argument("input_file", type=Path)
    br.add_argument("--column", help="CSV column name for primary values")
    br.add_argument("--use-ansible-keys", action="store_true")
    br.add_argument("--output-dir", help="Output directory prefix (optional)")

    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)

    if args.cmd == "list-sources":
        return list_sources()

    if args.cmd == "list-functions":
        return list_functions(args.source)

    if args.cmd == "run":
        keys = {}
        if getattr(args, "use_ansible_keys", False):
            keys = load_keys_from_ansible()
        kwargs = None
        if args.kwargs:
            try:
                kwargs = json.loads(args.kwargs)
            except Exception:
                # fallback: try python literal
                kwargs = eval(args.kwargs)
        rc = run_job(args.source, args.function, primary=args.primary, kwargs=kwargs, keys=keys, output=args.output, append=args.append)
        raise SystemExit(rc)

    if args.cmd == "batch":
        keys = {}
        if getattr(args, "use_ansible_keys", False):
            keys = load_keys_from_ansible()
        outdir = args.output_dir
        rc = batch_run(args.source, args.function, args.input_file, args.column, keys=keys, output=None)
        raise SystemExit(rc)

    ap.print_help()


if __name__ == "__main__":
    main()
