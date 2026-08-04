#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORE_INSTALLER="$REPO_ROOT/scripts/install_system.sh"
PRESET_FILE="$REPO_ROOT/config/remote-bootstrap-presets.tsv"

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

need_file() {
  local f="$1"
  [[ -f "$f" ]] || fail "Required file not found: $f"
}

run_cmd() {
  echo
  echo "+ $*"
  "$@"
}

run_bash_script() {
  local script="$1"
  shift || true
  if [[ -f "$script" ]]; then
    run_cmd bash "$script" "$@"
  else
    echo "Missing: $script"
  fi
}

run_python_script() {
  local script="$1"
  shift || true
  if [[ -f "$script" ]]; then
    run_cmd python3 "$script" "$@"
  else
    echo "Missing: $script"
  fi
}

require_cmd() {
  local cmd="$1"
  command -v "$cmd" >/dev/null 2>&1 || fail "Required command not found: $cmd"
}

copy_repo_to_remote() {
  local host="$1"
  local user="$2"
  local port="$3"
  local remote_dir="$4"

  [[ -n "$host" ]] || fail "Remote host cannot be empty"
  [[ -n "$user" ]] || fail "Remote user cannot be empty"
  [[ -n "$port" ]] || fail "Remote port cannot be empty"
  [[ -n "$remote_dir" ]] || fail "Remote directory cannot be empty"
  [[ "$remote_dir" != *"'"* ]] || fail "Remote directory cannot contain single quotes"

  require_cmd ssh
  require_cmd tar

  run_cmd bash -lc "cd \"$REPO_ROOT\" && tar --exclude=.git --exclude=venv --exclude=.venv --exclude=.precommit-venv --exclude=__pycache__ -czf - . | ssh -p \"$port\" -o StrictHostKeyChecking=accept-new \"$user@$host\" \"mkdir -p '$remote_dir' && tar -xzf - -C '$remote_dir'\""
}

run_remote_profile() {
  local host="$1"
  local user="$2"
  local port="$3"
  local remote_dir="$4"
  local install_args="$5"

  copy_repo_to_remote "$host" "$user" "$port" "$remote_dir"
  run_cmd ssh -p "$port" -o StrictHostKeyChecking=accept-new "$user@$host" "cd '$remote_dir' && bash ./install_system.sh $install_args"
}

ensure_preset_store() {
  mkdir -p "$(dirname "$PRESET_FILE")"
  [[ -f "$PRESET_FILE" ]] || touch "$PRESET_FILE"
}

list_presets() {
  ensure_preset_store
  awk -F'|' 'NF>=6 {printf "  - %s (hosts=%s user=%s port=%s dir=%s)\n", $1, $2, $3, $4, $5}' "$PRESET_FILE"
}

save_preset() {
  local name="$1"
  local hosts="$2"
  local user="$3"
  local port="$4"
  local remote_dir="$5"
  local install_args="$6"

  [[ -n "$name" ]] || fail "Preset name cannot be empty"
  [[ "$name" != *"|"* ]] || fail "Preset name cannot contain |"
  [[ "$hosts" != *"|"* ]] || fail "Hosts cannot contain |"
  [[ "$user" != *"|"* ]] || fail "User cannot contain |"
  [[ "$port" != *"|"* ]] || fail "Port cannot contain |"
  [[ "$remote_dir" != *"|"* ]] || fail "Remote dir cannot contain |"
  [[ "$install_args" != *"|"* ]] || fail "Install args cannot contain |"

  ensure_preset_store
  grep -vE "^${name//./\\.}\|" "$PRESET_FILE" >"$PRESET_FILE.tmp" || true
  printf "%s|%s|%s|%s|%s|%s\n" "$name" "$hosts" "$user" "$port" "$remote_dir" "$install_args" >>"$PRESET_FILE.tmp"
  mv "$PRESET_FILE.tmp" "$PRESET_FILE"
  echo "Saved preset: $name"
}

delete_preset() {
  local name="$1"
  ensure_preset_store
  if ! grep -qE "^${name//./\\.}\|" "$PRESET_FILE"; then
    echo "Preset not found: $name"
    return
  fi
  grep -vE "^${name//./\\.}\|" "$PRESET_FILE" >"$PRESET_FILE.tmp"
  mv "$PRESET_FILE.tmp" "$PRESET_FILE"
  echo "Deleted preset: $name"
}

get_preset_line() {
  local name="$1"
  ensure_preset_store
  awk -F'|' -v n="$name" '$1==n {line=$0} END {if (line) print line; else exit 1}' "$PRESET_FILE"
}

run_preset() {
  local name="$1"
  local line
  line="$(get_preset_line "$name" || true)"
  [[ -n "$line" ]] || fail "Preset not found: $name"

  local hosts user port remote_dir install_args
  IFS='|' read -r _name hosts user port remote_dir install_args <<< "$line"

  IFS=',' read -r -a host_array <<< "$hosts"
  for host in "${host_array[@]}"; do
    host="${host//[[:space:]]/}"
    [[ -z "$host" ]] && continue
    echo "Running preset $name on: $host"
    run_remote_profile "$host" "$user" "$port" "$remote_dir" "$install_args"
  done
}

edit_preset() {
  local name="$1"
  local line
  line="$(get_preset_line "$name" || true)"
  [[ -n "$line" ]] || fail "Preset not found: $name"

  local _name hosts user port remote_dir install_args
  IFS='|' read -r _name hosts user port remote_dir install_args <<< "$line"

  echo "Editing preset: $name"
  read -r -p "Hosts/IPs (comma-separated) [$hosts]: " new_hosts || return 0
  read -r -p "Remote user [$user]: " new_user || return 0
  read -r -p "SSH port [$port]: " new_port || return 0
  read -r -p "Remote repo dir [$remote_dir]: " new_remote_dir || return 0
  read -r -p "Installer args [$install_args]: " new_install_args || return 0

  new_hosts="${new_hosts:-$hosts}"
  new_user="${new_user:-$user}"
  new_port="${new_port:-$port}"
  new_remote_dir="${new_remote_dir:-$remote_dir}"
  new_install_args="${new_install_args:-$install_args}"

  save_preset "$name" "$new_hosts" "$new_user" "$new_port" "$new_remote_dir" "$new_install_args"
}

maybe_save_preset() {
  local hosts="$1"
  local user="$2"
  local port="$3"
  local remote_dir="$4"
  local install_args="$5"
  read -r -p "Save these values as a preset? [y/N]: " save_ans || return 0
  case "$save_ans" in
    [Yy]*)
      read -r -p "Preset name: " preset_name || return 0
      save_preset "$preset_name" "$hosts" "$user" "$port" "$remote_dir" "$install_args"
      ;;
  esac
}

export_presets_file() {
  local out_path="$1"
  [[ -n "$out_path" ]] || fail "Output path is required"

  ensure_preset_store
  mkdir -p "$(dirname "$out_path")"

  run_cmd python3 - "$PRESET_FILE" "$out_path" <<'PY'
import json
import os
import sys

src, out_path = sys.argv[1], sys.argv[2]
ext = os.path.splitext(out_path.lower())[1]

presets = []
with open(src, "r", encoding="utf-8") as fh:
    for raw in fh:
        line = raw.strip()
        if not line:
            continue
        parts = line.split("|", 5)
        if len(parts) != 6:
            continue
        name, hosts, user, port, remote_dir, install_args = parts
        presets.append(
            {
                "name": name,
                "hosts": hosts,
                "user": user,
                "port": port,
                "remote_dir": remote_dir,
                "install_args": install_args,
            }
        )

presets.sort(key=lambda p: p["name"])
payload = {"version": 1, "presets": presets}

if ext == ".json":
    with open(out_path, "w", encoding="utf-8") as out:
        json.dump(payload, out, indent=2, sort_keys=False)
        out.write("\n")
elif ext in (".yaml", ".yml"):
    # Write YAML without external deps. JSON strings are valid YAML scalars.
    with open(out_path, "w", encoding="utf-8") as out:
        out.write("version: 1\n")
        out.write("presets:\n")
        for p in presets:
            out.write("  - name: " + json.dumps(p["name"], ensure_ascii=False) + "\n")
            out.write("    hosts: " + json.dumps(p["hosts"], ensure_ascii=False) + "\n")
            out.write("    user: " + json.dumps(p["user"], ensure_ascii=False) + "\n")
            out.write("    port: " + json.dumps(p["port"], ensure_ascii=False) + "\n")
            out.write("    remote_dir: " + json.dumps(p["remote_dir"], ensure_ascii=False) + "\n")
            out.write("    install_args: " + json.dumps(p["install_args"], ensure_ascii=False) + "\n")
else:
    raise SystemExit(f"Unsupported export format for {out_path}. Use .json, .yaml, or .yml")

print(f"Exported {len(presets)} presets to {out_path}")
PY
}

import_presets_file() {
  local in_path="$1"
  [[ -n "$in_path" ]] || fail "Input path is required"
  [[ -f "$in_path" ]] || fail "Preset import file not found: $in_path"

  local tsv_tmp
  tsv_tmp="$(mktemp)"

  python3 - "$in_path" >"$tsv_tmp" <<'PY'
import json
import os
import sys

path = sys.argv[1]
ext = os.path.splitext(path.lower())[1]
text = open(path, "r", encoding="utf-8").read()

def normalize_item(item, index):
    if not isinstance(item, dict):
        raise ValueError(f"Preset at index {index} is not an object")
    name = str(item.get("name", "")).strip()
    hosts = str(item.get("hosts", "")).strip()
    user = str(item.get("user", "root")).strip() or "root"
    port = str(item.get("port", "22")).strip() or "22"
    remote_dir = str(item.get("remote_dir", "~/mcp-rhel-manager")).strip() or "~/mcp-rhel-manager"
    install_args = str(item.get("install_args", "--start --background-only --yes")).strip() or "--start --background-only --yes"
    if not name:
        raise ValueError(f"Preset at index {index} missing name")
    if not hosts:
        raise ValueError(f"Preset {name} missing hosts")
    for field_name, field_val in {
        "name": name,
        "hosts": hosts,
        "user": user,
        "port": port,
        "remote_dir": remote_dir,
        "install_args": install_args,
    }.items():
        if "|" in field_val:
            raise ValueError(f"Preset {name} field {field_name} contains unsupported '|' character")
    return [name, hosts, user, port, remote_dir, install_args]

def parse_payload():
    if ext == ".json":
        return json.loads(text)
    if ext in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except Exception:
            # Dependency-free fallback parser for the exported YAML shape.
            items = []
            current = None
            for raw in text.splitlines():
                line = raw.rstrip()
                if not line or line.lstrip().startswith("#"):
                    continue
                if line.startswith("version:") or line.startswith("presets:"):
                    continue
                if line.startswith("  - "):
                    if current:
                        items.append(current)
                    current = {}
                    kv = line[4:]
                    if ":" in kv:
                        k, v = kv.split(":", 1)
                        v = v.strip()
                        if v:
                            try:
                                current[k.strip()] = json.loads(v)
                            except Exception:
                                current[k.strip()] = v.strip('"\'')
                    continue
                if line.startswith("    ") and current is not None and ":" in line:
                    k, v = line.strip().split(":", 1)
                    v = v.strip()
                    try:
                        current[k.strip()] = json.loads(v)
                    except Exception:
                        current[k.strip()] = v.strip('"\'')
            if current:
                items.append(current)
            if items:
                return {"presets": items}
            # JSON is valid YAML; allow JSON-formatted YAML as final fallback.
            return json.loads(text)
        return yaml.safe_load(text)
    raise SystemExit(f"Unsupported import format for {path}. Use .json, .yaml, or .yml")

payload = parse_payload()

if isinstance(payload, dict) and "presets" in payload:
    items = payload.get("presets")
elif isinstance(payload, list):
    items = payload
elif isinstance(payload, dict):
    # Support map form: {"preset-name": {hosts: ..., ...}}
    items = []
    for key, value in payload.items():
        if not isinstance(value, dict):
            continue
        entry = dict(value)
        entry.setdefault("name", str(key))
        items.append(entry)
else:
    raise SystemExit("Unsupported preset file shape; expected list or object with 'presets'")

if not isinstance(items, list):
    raise SystemExit("'presets' must be a list")

for i, item in enumerate(items):
    values = normalize_item(item, i)
    print("|".join(values))
PY

  local imported_count=0
  while IFS='|' read -r name hosts user port remote_dir install_args; do
    [[ -z "${name:-}" ]] && continue
    save_preset "$name" "$hosts" "$user" "$port" "$remote_dir" "$install_args" >/dev/null
    imported_count=$((imported_count + 1))
  done <"$tsv_tmp"

  rm -f "$tsv_tmp"
  echo "Imported/updated $imported_count presets from $in_path"
}

show_preset_format_help() {
  cat <<'EOF'
Preset Import/Export Formats
  - JSON: .json
  - YAML: .yaml or .yml

Canonical structure:
{
  "version": 1,
  "presets": [
    {
      "name": "lab-single",
      "hosts": "10.0.0.11",
      "user": "root",
      "port": "22",
      "remote_dir": "~/mcp-rhel-manager",
      "install_args": "--dry-run"
    }
  ]
}

Idempotence behavior:
  - Import merges by preset name.
  - Existing preset names are updated in place.
  - Re-importing the same file does not duplicate entries.
EOF
}

remote_bootstrap_menu() {
  while true; do
    show_header
    cat <<'EOF'
[Remote Bootstrap Profiles]
  1) Single node profile (install + start background)
  2) Cluster worker profile (batch hosts)
  3) Cluster controller profile (dev+AI packages, eager start)
  4) Remote dry-run profile (no changes)
  5) Run saved preset
  6) Save preset (manual entry)
  7) List presets
  8) Delete preset
  9) Edit preset
  10) Export presets (JSON/YAML)
  11) Import presets (JSON/YAML)
  12) Show preset format help
  b) Back
EOF
    read -r -p "Choose an option: " choice || return 0
    case "$choice" in
      1)
        read -r -p "Target host/IP: " host || return 0
        read -r -p "Remote user [root]: " user || return 0
        read -r -p "SSH port [22]: " port || return 0
        read -r -p "Remote repo dir [~/mcp-rhel-manager]: " remote_dir || return 0
        user="${user:-root}"
        port="${port:-22}"
        remote_dir="${remote_dir:-~/mcp-rhel-manager}"
        run_remote_profile "$host" "$user" "$port" "$remote_dir" "--start --background-only --yes"
        maybe_save_preset "$host" "$user" "$port" "$remote_dir" "--start --background-only --yes"
        pause_prompt
        ;;
      2)
        read -r -p "Target hosts/IPs (comma-separated): " hosts || return 0
        read -r -p "Remote user [root]: " user || return 0
        read -r -p "SSH port [22]: " port || return 0
        read -r -p "Remote repo dir [~/mcp-rhel-manager]: " remote_dir || return 0
        user="${user:-root}"
        port="${port:-22}"
        remote_dir="${remote_dir:-~/mcp-rhel-manager}"
        IFS=',' read -r -a host_array <<< "$hosts"
        for host in "${host_array[@]}"; do
          host="${host//[[:space:]]/}"
          [[ -z "$host" ]] && continue
          echo "Bootstrapping worker: $host"
          run_remote_profile "$host" "$user" "$port" "$remote_dir" "--start --background-only --yes"
        done
        maybe_save_preset "$hosts" "$user" "$port" "$remote_dir" "--start --background-only --yes"
        pause_prompt
        ;;
      3)
        read -r -p "Controller host/IP: " host || return 0
        read -r -p "Remote user [root]: " user || return 0
        read -r -p "SSH port [22]: " port || return 0
        read -r -p "Remote repo dir [~/mcp-rhel-manager]: " remote_dir || return 0
        user="${user:-root}"
        port="${port:-22}"
        remote_dir="${remote_dir:-~/mcp-rhel-manager}"
        run_remote_profile "$host" "$user" "$port" "$remote_dir" "--start --eager-start --dev-packages --ai-workstation --yes"
        maybe_save_preset "$host" "$user" "$port" "$remote_dir" "--start --eager-start --dev-packages --ai-workstation --yes"
        pause_prompt
        ;;
      4)
        read -r -p "Target host/IP: " host || return 0
        read -r -p "Remote user [root]: " user || return 0
        read -r -p "SSH port [22]: " port || return 0
        read -r -p "Remote repo dir [~/mcp-rhel-manager]: " remote_dir || return 0
        user="${user:-root}"
        port="${port:-22}"
        remote_dir="${remote_dir:-~/mcp-rhel-manager}"
        run_remote_profile "$host" "$user" "$port" "$remote_dir" "--dry-run"
        maybe_save_preset "$host" "$user" "$port" "$remote_dir" "--dry-run"
        pause_prompt
        ;;
      5)
        read -r -p "Preset name to run: " preset_name || return 0
        run_preset "$preset_name"
        pause_prompt
        ;;
      6)
        read -r -p "Preset name: " preset_name || return 0
        read -r -p "Hosts/IPs (comma-separated): " hosts || return 0
        read -r -p "Remote user [root]: " user || return 0
        read -r -p "SSH port [22]: " port || return 0
        read -r -p "Remote repo dir [~/mcp-rhel-manager]: " remote_dir || return 0
        read -r -p "Installer args [--start --background-only --yes]: " install_args || return 0
        user="${user:-root}"
        port="${port:-22}"
        remote_dir="${remote_dir:-~/mcp-rhel-manager}"
        install_args="${install_args:---start --background-only --yes}"
        save_preset "$preset_name" "$hosts" "$user" "$port" "$remote_dir" "$install_args"
        pause_prompt
        ;;
      7)
        echo "Saved presets:"
        list_presets
        pause_prompt
        ;;
      8)
        read -r -p "Preset name to delete: " preset_name || return 0
        delete_preset "$preset_name"
        pause_prompt
        ;;
      9)
        read -r -p "Preset name to edit: " preset_name || return 0
        edit_preset "$preset_name"
        pause_prompt
        ;;
      10)
        read -r -p "Export file [config/remote-bootstrap-presets.json]: " export_path || return 0
        export_path="${export_path:-$REPO_ROOT/config/remote-bootstrap-presets.json}"
        export_presets_file "$export_path"
        pause_prompt
        ;;
      11)
        read -r -p "Import file path: " import_path || return 0
        import_presets_file "$import_path"
        pause_prompt
        ;;
      12)
        show_preset_format_help
        pause_prompt
        ;;
      b|B) return ;;
      *) echo "Invalid option" ; pause_prompt ;;
    esac
  done
}

pause_prompt() {
  echo
  read -r -p "Press Enter to continue..." _ || true
}

show_header() {
  clear || true
  cat <<'EOF'
============================================================
 MCP RHEL MANAGER - BASE INSTALLER
============================================================
This menu wraps install and operations functions found in scripts/.
For direct automation, pass arguments to this script and they will be
forwarded to scripts/install_system.sh.
EOF
  echo
}

show_usage() {
  cat <<EOF
Usage:
  ./install_system.sh                # interactive menu
  ./install_system.sh --menu         # interactive menu
  ./install_system.sh [installer args]

Examples:
  ./install_system.sh --dry-run
  ./install_system.sh --start --yes --background-only
  ./install_system.sh --verify

Note:
  Unknown/non-menu flags are forwarded to:
  $CORE_INSTALLER
EOF
}

forward_to_core_installer() {
  need_file "$CORE_INSTALLER"
  run_cmd bash "$CORE_INSTALLER" "$@"
}

install_menu() {
  while true; do
    show_header
    cat <<'EOF'
[Install / Upgrade]
  1) Dry-run preview
  2) Full install + start (background-only)
  3) Full install + eager-start
  4) In-repo venv install + start
  5) Install with dev packages
  6) Install with AI workstation packages
  7) Reconfigure ansible env
  8) Uninstall (preserve data)
  9) Uninstall (full, forced)
  b) Back
EOF
    read -r -p "Choose an option: " choice || return 0
    case "$choice" in
      1) forward_to_core_installer --dry-run ; pause_prompt ;;
      2) forward_to_core_installer --start --background-only --yes ; pause_prompt ;;
      3) forward_to_core_installer --start --eager-start --yes ; pause_prompt ;;
      4) forward_to_core_installer --venv --start --yes ; pause_prompt ;;
      5) forward_to_core_installer --start --background-only --dev-packages --yes ; pause_prompt ;;
      6) forward_to_core_installer --start --background-only --ai-workstation --yes ; pause_prompt ;;
      7) forward_to_core_installer --reconfigure ; pause_prompt ;;
      8) forward_to_core_installer --uninstall --preserve-data --yes ; pause_prompt ;;
      9) forward_to_core_installer --uninstall --really-force --yes ; pause_prompt ;;
      b|B) return ;;
      *) echo "Invalid option" ; pause_prompt ;;
    esac
  done
}

verify_menu() {
  while true; do
    show_header
    cat <<'EOF'
[Verification / Health]
  1) Installer verification
  2) Installer verification (JSON)
  3) Run health + fix once
  4) Run HAL diagnostics
  5) Check file reference integrity
  b) Back
EOF
    read -r -p "Choose an option: " choice || return 0
    case "$choice" in
      1) forward_to_core_installer --verify ; pause_prompt ;;
      2) forward_to_core_installer --verify-json ; pause_prompt ;;
      3) run_bash_script "$REPO_ROOT/scripts/run_health_and_fix_once.sh" ; pause_prompt ;;
      4) run_python_script "$REPO_ROOT/scripts/hal_diagnostics.py" ; pause_prompt ;;
      5) run_python_script "$REPO_ROOT/scripts/check_file_refs.py" ; pause_prompt ;;
      b|B) return ;;
      *) echo "Invalid option" ; pause_prompt ;;
    esac
  done
}

services_menu() {
  while true; do
    show_header
    cat <<'EOF'
[Services / Automation]
  1) Install watch service
  2) Install auto-update timer
  3) Install approvals service
  4) Install code-fix service
  5) Install model service
  6) Show status: mcp-bridge.service
  7) Show status: hal-watch.service
  b) Back
EOF
    read -r -p "Choose an option: " choice || return 0
    case "$choice" in
      1) run_bash_script "$REPO_ROOT/scripts/install_watch_service.sh" ; pause_prompt ;;
      2) run_bash_script "$REPO_ROOT/scripts/install-auto-update.sh" ; pause_prompt ;;
      3) run_bash_script "$REPO_ROOT/scripts/install_approvals_service.sh" ; pause_prompt ;;
      4) run_bash_script "$REPO_ROOT/scripts/install_code_fix_service.sh" ; pause_prompt ;;
      5) run_bash_script "$REPO_ROOT/scripts/install_model_service.sh" ; pause_prompt ;;
      6) run_cmd bash -lc "systemctl status mcp-bridge.service --no-pager || true" ; pause_prompt ;;
      7) run_cmd bash -lc "systemctl status hal-watch.service --no-pager || true" ; pause_prompt ;;
      b|B) return ;;
      *) echo "Invalid option" ; pause_prompt ;;
    esac
  done
}

cluster_mesh_menu() {
  while true; do
    show_header
    cat <<'EOF'
[Cluster / Mesh]
  1) Setup SSH mesh (hosts list)
  2) Setup SSH mesh (hosts file)
  3) Show SSH mesh helper usage
  4) Remote bootstrap profiles
  b) Back
EOF
    read -r -p "Choose an option: " choice || return 0
    case "$choice" in
      1)
        read -r -p "Hosts (comma-separated): " hosts || return 0
        read -r -p "Remote user [root]: " user || return 0
        user="${user:-root}"
        read -r -s -p "Password (optional; leave blank to use key/env): " pass || return 0
        echo
        if [[ -n "${pass}" ]]; then
          HAL_SSH_PASSWORD="$pass" run_python_script "$REPO_ROOT/scripts/setup_ssh_mesh.py" --hosts "$hosts" --user "$user"
        else
          run_python_script "$REPO_ROOT/scripts/setup_ssh_mesh.py" --hosts "$hosts" --user "$user"
        fi
        pause_prompt
        ;;
      2)
        read -r -p "Hosts file path: " hosts_file || return 0
        read -r -p "Remote user [root]: " user || return 0
        user="${user:-root}"
        read -r -s -p "Password (optional; leave blank to use key/env): " pass || return 0
        echo
        if [[ -n "${pass}" ]]; then
          HAL_SSH_PASSWORD="$pass" run_python_script "$REPO_ROOT/scripts/setup_ssh_mesh.py" --hosts-file "$hosts_file" --user "$user"
        else
          run_python_script "$REPO_ROOT/scripts/setup_ssh_mesh.py" --hosts-file "$hosts_file" --user "$user"
        fi
        pause_prompt
        ;;
      3) run_python_script "$REPO_ROOT/scripts/setup_ssh_mesh.py" --help ; pause_prompt ;;
      4) remote_bootstrap_menu ;;
      b|B) return ;;
      *) echo "Invalid option" ; pause_prompt ;;
    esac
  done
}

ai_tools_menu() {
  while true; do
    show_header
    cat <<'EOF'
[AI / Model Tools]
  1) Download model (helper)
  2) Manage models CLI
  3) Start LLM container helper
  4) Start inference queue
  b) Back
EOF
    read -r -p "Choose an option: " choice || return 0
    case "$choice" in
      1) run_bash_script "$REPO_ROOT/scripts/download_model.sh" ; pause_prompt ;;
      2) run_python_script "$REPO_ROOT/scripts/manage_models.py" ; pause_prompt ;;
      3) run_bash_script "$REPO_ROOT/scripts/start_llm_container.sh" ; pause_prompt ;;
      4) run_bash_script "$REPO_ROOT/scripts/start_inference_queue.sh" ; pause_prompt ;;
      b|B) return ;;
      *) echo "Invalid option" ; pause_prompt ;;
    esac
  done
}

advanced_menu() {
  while true; do
    show_header
    cat <<'EOF'
[Advanced Maintenance]
  1) Install OS deps from bindep.txt
  2) Run system fixer
  3) Fix systemd units
  4) Render systemd units
  5) Install pre-commit hooks
  6) Run pre-commit
  b) Back
EOF
    read -r -p "Choose an option: " choice || return 0
    case "$choice" in
      1) run_bash_script "$REPO_ROOT/scripts/install_deps.sh" ; pause_prompt ;;
      2) run_bash_script "$REPO_ROOT/scripts/fix-system.sh" ; pause_prompt ;;
      3) run_bash_script "$REPO_ROOT/scripts/fix_systemd_units.sh" ; pause_prompt ;;
      4) run_bash_script "$REPO_ROOT/scripts/render_systemd_units.sh" ; pause_prompt ;;
      5) run_bash_script "$REPO_ROOT/scripts/install_precommit.sh" ; pause_prompt ;;
      6) run_bash_script "$REPO_ROOT/scripts/run_precommit.sh" ; pause_prompt ;;
      b|B) return ;;
      *) echo "Invalid option" ; pause_prompt ;;
    esac
  done
}

main_menu() {
  need_file "$CORE_INSTALLER"
  while true; do
    show_header
    cat <<'EOF'
[Main Menu]
  1) Install / Upgrade
  2) Verification / Health
  3) Services / Automation
  4) Cluster / Mesh
  5) AI / Model Tools
  6) Advanced Maintenance
  0) Exit
EOF
    read -r -p "Choose an option: " choice || exit 0
    case "$choice" in
      1) install_menu ;;
      2) verify_menu ;;
      3) services_menu ;;
      4) cluster_mesh_menu ;;
      5) ai_tools_menu ;;
      6) advanced_menu ;;
      0) echo "Exiting." ; exit 0 ;;
      *) echo "Invalid option" ; pause_prompt ;;
    esac
  done
}

if [[ $# -gt 0 ]]; then
  case "$1" in
    --menu)
      shift
      main_menu "$@"
      ;;
    --help|-h)
      show_usage
      ;;
    *)
      forward_to_core_installer "$@"
      ;;
  esac
else
  main_menu
fi
