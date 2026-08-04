#!/usr/bin/env python3
"""HAL Terminal UI (lightweight): menu-driven interface to generate intel reports.

This is a simple, dependency-free textual TUI that works in any terminal.
Run: `python3 scripts/hal_tui.py`

Features:
- Main menu with numeric choices (0 = back/exit)
- Generate an intel report (on-screen or dump to file)
- Recent HAL interactions listing and view
- Export contacts CSV for an account
- Toggle quick settings (LLM augmentation, show full contacts)

This script imports the `scripts/hal.py` module at runtime and calls its
offline-first report functions. It only uses standard library features.
"""
from __future__ import annotations
import os
import sys
import json
import textwrap
import importlib.util
from pathlib import Path


def load_hal_module() -> object:
    here = Path(__file__).resolve().parent
    hal_path = here.joinpath('hal.py')
    if not hal_path.exists():
        # fallback to repo-level scripts path
        hal_path = Path(__file__).resolve().parents[1].joinpath('scripts', 'hal.py')
    if not hal_path.exists():
        print('Could not find scripts/hal.py. Run from repository root.')
        sys.exit(2)
    spec = importlib.util.spec_from_file_location('hal_cli', str(hal_path))
    hal = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hal)
    return hal


def prompt(msg: str, default: str | None = None) -> str:
    if default:
        res = input(f'{msg} [{default}]: ').strip()
        return res or default
    return input(f'{msg}: ').strip()


def print_header(title: str) -> None:
    print('\n' + '=' * 72)
    print(title)
    print('=' * 72 + '\n')


def list_recent_interactions(hal) -> list[Path]:
    d = Path(getattr(hal, 'TRAIN_DIR', os.path.expanduser('~/.mcp-ai/training')))
    files = sorted(d.glob('hal-*.jsonl'), key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def view_interaction(hal, path: Path) -> None:
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            j = json.load(fh)
        print('\n-- Interaction --\n')
        print('Timestamp:', j.get('timestamp'))
        print('User:', j.get('user'))
        print('\nRequest:\n', j.get('request'))
        print('\nAI Response (truncated first 1000 chars):\n')
        print(textwrap.fill((j.get('ai_response_raw') or '')[:2000], width=100))
        print('\n')
    except Exception as exc:
        print('Failed to open interaction:', exc)


def generate_report_flow(hal) -> None:
    # Present dynamic territory -> accounts selection when possible
    account = None
    try:
        # Build mapping from training index if available
        mapping = {}
        entries = []
        if hasattr(hal, '_get_training_index'):
            entries = hal._get_training_index()

        for item in entries:
            try:
                if item.get('doc_type') != 'business_intel_account':
                    continue
                rec = item.get('record') or {}
                acct = (rec.get('account_name') or rec.get('account') or '').strip()
                if not acct:
                    continue
                # prefer territory_name, fallback to territory_owner, pod_name, or 'Unassigned'
                terr = (rec.get('territory_name') or rec.get('territory_owner') or rec.get('pod_name') or rec.get('territory') or '').strip()
                if not terr:
                    terr = 'Unassigned'
                mapping.setdefault(terr, set()).add(acct)
            except Exception:
                continue

        if mapping:
            # Prefer to surface important territories first if present
            keys = list(mapping.keys())
            ordered = []
            for special in ('High Plains', 'Unassigned'):
                if special in mapping and special not in ordered:
                    ordered.append(special)
            if ordered:
                rest = sorted([k for k in keys if k not in ordered])
                territories = ordered + rest
            else:
                territories = sorted(keys)

            print('\nSelect a territory:')
            for i, t in enumerate(territories, start=1):
                print(f'  {i}) {t} ({len(mapping.get(t, []))} accounts)')
            print('  0) Cancel / back')
            tchoice = prompt('Select', '0')
            try:
                ti = int(tchoice)
                if ti <= 0:
                    return
                if 1 <= ti <= len(territories):
                    chosen_terr = territories[ti-1]
                    accounts = sorted(list(mapping.get(chosen_terr, [])))
                    if not accounts:
                        print('No accounts found for territory', chosen_terr)
                        return
                    # Show account submenu
                    print(f"\nAccounts in {chosen_terr}:")
                    for j, a in enumerate(accounts, start=1):
                        print(f'  {j}) {a}')
                    print('  0) Cancel / back')
                    achoice = prompt('Select account', '0')
                    try:
                        ai = int(achoice)
                        if ai <= 0:
                            return
                        if 1 <= ai <= len(accounts):
                            account = accounts[ai-1]
                        else:
                            print('Invalid selection')
                            return
                    except Exception:
                        print('Invalid selection')
                        return
                else:
                    print('Invalid selection')
                    return
            except Exception:
                print('Invalid selection')
                return
    except Exception:
        # Fall back to free-text prompt if anything fails
        pass

    if not account:
        account = prompt('Enter account/company name')
        if not account:
            print('No account provided, returning to menu.')
            return

    # Choose output
    print('\nChoose output:')
    print('  1) On-screen (default)')
    print('  2) Dump to file')
    out_choice = prompt('Select', '1')

    # LLM augmentation opt-in for this run
    cur = os.environ.get('HAL_ALLOW_LLM_INTEL', '0')
    llm_default = 'y' if cur and cur.lower() in ('1', 'true', 'yes', 'y') else 'n'
    use_llm = prompt('Allow LLM augmentation if offline data insufficient? (y/N)', llm_default)
    if use_llm and use_llm.lower() in ('1', 'true', 'yes', 'y'):
        os.environ['HAL_ALLOW_LLM_INTEL'] = '1'
    else:
        os.environ['HAL_ALLOW_LLM_INTEL'] = '0'

    # Export contacts option
    export_contacts = prompt('Export contacts CSV after report? (y/N)', 'n')
    export_dir = ''
    if export_contacts.lower() in ('1', 'y', 'yes'):
        export_dir = prompt('Export directory (will be created)', os.path.join(str(Path.home()), 'Downloads'))
        os.environ['HAL_EXPORT_CONTACTS_DIR'] = export_dir

    print('\nGenerating report (this will refresh public enrichment and convert to structured intel)...')
    try:
        report = hal._generate_intel_report_now(account)
    except Exception as exc:
        print('Report generation failed:', exc)
        return

    if not report:
        print('No report available for', account)
        return

    if out_choice.strip() == '2':
        # Write to HAL user reports dir
        reports_root = os.environ.get('HAL_USER_REPORTS_DIR', os.path.join(str(Path.home()), 'Documents', 'reports'))
        os.makedirs(reports_root, exist_ok=True)
        safe = ''.join([c if c.isalnum() or c in (' ', '-', '_') else '_' for c in account])[:120]
        fname = os.path.join(reports_root, f'intel-{safe}.txt')
        try:
            with open(fname, 'w', encoding='utf-8') as fh:
                fh.write(report)
            print('Report saved to', fname)
        except Exception as exc:
            print('Failed to write report file:', exc)
    else:
        print('\n' + report + '\n')

    # Optionally export contacts
    if export_dir:
        # Try to find structured contacts from best business record
        try:
            best = hal._find_best_business_intel_record(account)
            contacts_raw = best.get('contacts', []) if best else []
            contacts = hal._safely_parse_json_or_list(contacts_raw)
            fname = hal._maybe_export_contacts_csv(account, contacts) if hasattr(hal, '_maybe_export_contacts_csv') else None
            if fname:
                print('Contacts exported to:', fname)
            else:
                print('No contacts exported (none found or export failed).')
        except Exception:
            print('Contact export failed.')


def contact_list_flow(hal) -> None:
    """Display only the contact list for an account (compact view)."""
    account = prompt('Account name to show contacts for')
    if not account:
        print('No account provided, returning to menu.')
        return
    try:
        best = hal._find_best_business_intel_record(account)
        contacts_raw = best.get('contacts', []) if best else []
        contacts = hal._safely_parse_json_or_list(contacts_raw) if contacts_raw else []
    except Exception:
        contacts = []

    print_header(f'Contact list — {account}')
    if not contacts:
        print('No contacts found for', account)
        return
    # Normalize and print
    for c in (contacts if isinstance(contacts, list) else [contacts]):
        try:
            if isinstance(c, dict):
                name = (c.get('name') or '').strip()
                email = (c.get('email') or '').strip()
                conf = c.get('confidence') or c.get('verification', {}).get('confidence') or ''
                print(f'    • {name} — {email}' + (f' (confidence={conf})' if conf else ''))
            else:
                s = str(c).strip()
                # try to split pipe-separated name|email
                if '|' in s:
                    parts = [p.strip() for p in s.split('|', 1)]
                    if len(parts) == 2:
                        print(f'    • {parts[0]} — {parts[1]}')
                        continue
                # fallback to printing the raw string
                print(f'    • {s}')
        except Exception:
            print('    •', str(c))


def recent_reports_menu(hal) -> None:
    files = list_recent_interactions(hal)
    if not files:
        print('No recent HAL interactions found.')
        return
    print('\nRecent interactions:')
    for i, p in enumerate(files[:20]):
        print(f'  {i+1}) {p.name}  ({p.stat().st_mtime:.0f})')
    choice = prompt('Enter number to view (0 to go back)', '0')
    try:
        n = int(choice)
        if n <= 0:
            return
        idx = n - 1
        if 0 <= idx < len(files):
            view_interaction(hal, files[idx])
    except Exception:
        print('Invalid selection')


def settings_menu(hal) -> None:
    while True:
        print('\nSettings:')
        show_full = os.environ.get('HAL_SHOW_FULL_CONTACTS', '0')
        allow_llm = os.environ.get('HAL_ALLOW_LLM_INTEL', '0')
        print(f'  1) Toggle show full contacts (current={show_full})')
        print(f'  2) Toggle allow LLM augmentation (current={allow_llm})')
        print(f'  3) Set contacts export dir (HAL_EXPORT_CONTACTS_DIR={os.environ.get("HAL_EXPORT_CONTACTS_DIR","(not set)")})')
        print(f'  0) Back')
        choice = prompt('Select', '0')
        if choice == '0':
            return
        if choice == '1':
            cur = os.environ.get('HAL_SHOW_FULL_CONTACTS', '0')
            os.environ['HAL_SHOW_FULL_CONTACTS'] = '0' if cur and cur.lower() in ('1', 'true', 'yes', 'y') else '1'
            print('Updated HAL_SHOW_FULL_CONTACTS =', os.environ['HAL_SHOW_FULL_CONTACTS'])
        elif choice == '2':
            cur = os.environ.get('HAL_ALLOW_LLM_INTEL', '0')
            os.environ['HAL_ALLOW_LLM_INTEL'] = '0' if cur and cur.lower() in ('1', 'true', 'yes', 'y') else '1'
            print('Updated HAL_ALLOW_LLM_INTEL =', os.environ['HAL_ALLOW_LLM_INTEL'])
        elif choice == '3':
            d = prompt('Export directory for contacts', os.path.join(str(Path.home()), 'Downloads'))
            os.environ['HAL_EXPORT_CONTACTS_DIR'] = d
            print('Set HAL_EXPORT_CONTACTS_DIR =', d)
        else:
            print('Unknown choice')


def main() -> None:
    hal = load_hal_module()
    while True:
        print_header('HAL — Interactive Intel TUI')
        print('Main menu:')
        print('  1) Generate Intel Report')
        print('  2) Recent HAL Interactions')
        print('  3) Export contacts CSV for an account')
        print('  4) Settings')
        print('  5) Contact list')
        print('  0) Exit')
        print('')
        choice = prompt('Select an option', '0')
        if choice == '1':
            generate_report_flow(hal)
        elif choice == '2':
            recent_reports_menu(hal)
        elif choice == '3':
            account = prompt('Account name to export contacts for')
            if not account:
                continue
            export_dir = prompt('Export directory', os.path.join(str(Path.home()), 'Downloads'))
            os.environ['HAL_EXPORT_CONTACTS_DIR'] = export_dir
            try:
                best = hal._find_best_business_intel_record(account)
                contacts_raw = best.get('contacts', []) if best else []
                contacts = hal._safely_parse_json_or_list(contacts_raw)
                fname = hal._maybe_export_contacts_csv(account, contacts) if hasattr(hal, '_maybe_export_contacts_csv') else None
                if fname:
                    print('Contacts exported to:', fname)
                else:
                    print('No contacts exported (none found or failed).')
            except Exception as exc:
                print('Export failed:', exc)
        elif choice == '4':
            settings_menu(hal)
        elif choice == '5':
            contact_list_flow(hal)
        elif choice == '0':
            print('Goodbye.')
            return
        else:
            print('Unknown option')


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nInterrupted by user. Exiting.')
