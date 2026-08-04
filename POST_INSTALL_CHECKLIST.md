# Post-Install Checklist (RHEL/Fedora Target)

Use this checklist on the target system after pulling this branch.

## 1) Preflight

```bash
cd /path/to/mcp-rhel-manager
uname -a
cat /etc/os-release
command -v dnf || command -v yum
```

Also ensure Podman is available and that you are logged into Red Hat's registry before any container operations:

```bash
command -v podman || echo "podman missing: install podman first"
podman login registry.redhat.io
```

Expected:
- Linux host
- Fedora or RHEL-family distro
- dnf or yum available

## 2) Dry Run (No Changes)

```bash
./install_system.sh --dry-run
```

Expected:
- Command completes without unsupported-OS abort
- Shows planned actions

## 3) Install

```bash
./install_system.sh --start --background-only --yes
```

Alternative (interactive menu):

```bash
./install_system.sh --menu
```

## 4) Basic Verification

```bash
./install_system.sh --verify
./install_system.sh --verify-json
```

Expected:
- Verify mode returns healthy checks for required components

## 5) Services and Runtime

```bash
systemctl status mcp-bridge.service --no-pager || true
systemctl status hal-watch.service --no-pager || true
systemctl list-timers --all | grep -E "hal-auto-update|mcp-ai-collector" || true
```

## 6) Script/CLI Sanity Checks

```bash
python3 -m py_compile scripts/setup_ssh_mesh.py scripts/watch_system_and_fix.py scripts/hal_diagnostics.py scripts/server.py scripts/hal.py
python3 scripts/setup_ssh_mesh.py --help
```

## 7) Preset Import/Export Sanity (Optional)

```bash
./install_system.sh --menu
# Go to: Cluster / Mesh -> Remote bootstrap profiles
# Export presets to JSON/YAML, then import them back.
```

Expected:
- Import is idempotent (same preset names are updated, not duplicated)

## 8) Remote Bootstrap Smoke (Optional)

Use from installer menu:
- Cluster / Mesh
- Remote bootstrap profiles

Recommended first pass:
- Remote dry-run profile

## 9) If Something Fails

Collect diagnostics:

```bash
journalctl -u mcp-bridge.service -n 100 --no-pager || true
journalctl -u hal-watch.service -n 100 --no-pager || true
python3 scripts/hal_diagnostics.py || true
```

Then rerun:

```bash
./install_system.sh --dry-run
```

and check for missing packages/permissions.
