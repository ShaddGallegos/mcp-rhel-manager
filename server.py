import sys, os, subprocess, psutil, platform, shutil
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("P-Series-Architect")

def run_cmd(cmd, sudo=False):
    if sudo: cmd = ['sudo'] + cmd
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return res.stdout.strip()
    except Exception as e: return f"ERR: {str(e)}"

# -- EVOLUTION & PERFORMANCE --
@mcp.tool()
def optimize_ai_performance():
    """Sets CPU to performance and enables NVIDIA persistence."""
    run_cmd(['cpupower', 'frequency-set', '-g', 'performance'], sudo=True)
    if os.path.exists('/dev/nvidia0'):
        run_cmd(['nvidia-smi', '-pm', '1'], sudo=True)
    return "Performance optimized for AI workloads."

@mcp.tool()
def system_evolution():
    """Updates Kernel/Drivers and ensures akmods are built."""
    update = run_cmd(['dnf', 'upgrade', '-y', 'kernel*', '*nvidia*'], sudo=True)
    build = run_cmd(['akmods', '--force'], sudo=True)
    return f"Update: {update}\nBuild: {build}"

# -- INFRASTRUCTURE AS CODE (CaC) --
@mcp.tool()
def generate_ansible_manifest():
    """Dumps system state into an Ansible role for future propagation."""
    pkgs = run_cmd(['dnf', 'repoquery', '--installed', '--queryformat', '%{name}'])
    path = "/home/sgallego/mcp-rhel-manager/ansible/roles/p_series_node/vars/main.yml"
    with open(path, 'w') as f:
        f.write("---\ninstalled_packages:\n")
        for p in pkgs.split('\n'): f.write(f"  - {p}\n")
    return f"Ansible manifest updated at {path}"

# -- SURVIVAL & EXFILTRATION --
@mcp.tool()
def predict_failure_and_evacuate():
    """Checks NVMe health; if failing, pushes config to git."""
    smart = run_cmd(['smartctl', '-H', '/dev/nvme0n1'], sudo=True)
    if "PASSED" not in smart:
        run_cmd(['git', 'add', '.'], False)
        run_cmd(['git', 'commit', '-m', 'EMERGENCY EVACUATION'], False)
        run_cmd(['git', 'push', 'origin', 'main'], False)
        return "CRITICAL: Drive failure detected. Configuration evacuated to GitHub."
    return "Hardware integrity stable."

# -- SECURITY --
@mcp.tool()
def sentinel_scan():
    """Scans for intrusion probes."""
    ssh = run_cmd(['journalctl', '-t', 'sshd', '--since', '1h ago', '-g', 'Failed|Invalid'])
    fw = run_cmd(['journalctl', '-k', '--since', '1h ago', '-g', 'FINAL_REJECT|FINAL_DROP'])
    return f"SENTINEL REPORT:\nSSH: {ssh}\nFW: {fw}"


@mcp.tool()
def hardware_diagnostics():
    """Collect simple hardware diagnostics and return a prioritized report.

    The returned text now lists each detected problem (ordered by remediation
    priority) and includes an "Remediation (immediate-first)" list under each
    problem so HAL can include those lines directly under the corresponding
    problem in its final report.
    """
    import re, datetime

    sensors_out = run_cmd(['sensors', '-u']) or ''
    dmesg_out = run_cmd(['dmesg', '-T', '--level=err,crit,alert,emerg']) or ''
    full_dmesg = run_cmd(['dmesg', '-T']) or ''
    lspci_out = run_cmd(['lspci', '-vvnn']) or ''
    journal_err = run_cmd(['journalctl', '-b', '-p', '3', '--no-pager']) or ''
    bridge_journal = run_cmd(['journalctl', '-u', 'mcp-bridge.service', '-n', '200', '--no-pager']) or ''
    flatpak_journal = run_cmd(['journalctl', '-t', 'flatpak', '-n', '200', '--no-pager']) or ''
    lsblk_out = run_cmd(['lsblk', '-J']) or ''
    smart_out = 'NO_NVME'
    if os.path.exists('/dev/nvme0n1'):
        smart_out = run_cmd(['smartctl', '-a', '/dev/nvme0n1'], sudo=True) or ''

    # IPMI sensors (if available)
    ipmi_out = ''
    if shutil.which('ipmitool'):
        ipmi_out = run_cmd(['ipmitool', 'sdr']) or ''

    # LLDP neighbor info (if available)
    lldp_out = ''
    if shutil.which('lldpctl'):
        lldp_out = run_cmd(['lldpctl']) or ''

    issues = []

    def add_issue(title, driver, hardware, error_text, severity, remediations):
        issues.append({
            'title': title,
            'driver': driver,
            'hardware': hardware,
            'error_text': error_text,
            'severity': severity,
            'remediations': remediations,
        })

    # 1) Temperature heuristic
    try:
        floats = [float(x) for x in re.findall(r'([0-9]+\.[0-9]+)', sensors_out)]
        temps = [t for t in floats if 0 < t < 200]
        max_temp = max(temps) if temps else None
        # Refined thresholds: Critical >=95C, High >=85C
        if max_temp and max_temp >= 95:
            add_issue(
                title=f'CPU over-temperature: {max_temp}C',
                driver='coretemp / acpitz',
                hardware='CPU package (system sensors)',
                error_text=f'Package/core peak temperature {max_temp}C detected in sensors output',
                severity='Critical',
                remediations=[
                    'Immediate: Reduce system load — run `ps -eo pid,ppid,cmd,%mem,%cpu --sort=-%cpu | head -n 20` and stop or restart heavy processes or services.',
                    'Immediate: Inspect `sensors` output and confirm fan operation; check `ipmitool sdr` if available.',
                    'Immediate: If temperatures do not fall, migrate workloads off this host and power-cycle in a maintenance window.',
                    'Short-term: Check BIOS/firmware fan curves and update firmware; clean dust and verify heatsink mounting.',
                    'Mid-term: Replace thermal interface or cooling hardware if persistent.'
                ]
            )
        elif max_temp and max_temp >= 85:
            add_issue(
                title=f'High CPU temperature: {max_temp}C',
                driver='coretemp / acpitz',
                hardware='CPU package (system sensors)',
                error_text=f'Package/core temperature {max_temp}C detected in sensors output',
                severity='High',
                remediations=[
                    'Immediate: Reduce non-essential load and monitor temperatures (`top`, `htop`).',
                    'Immediate: Run `dmesg -T | egrep -i "thermal|throttle|critical"` to look for thermal throttling events.',
                    'Short-term: Verify cooling (fans/heatsink), clean dust and confirm fan speeds.',
                    'Mid-term: Review BIOS power/thermal settings and plan firmware updates.'
                ]
            )
    except Exception:
        pass

    # 2) mcp-bridge service instability
    if 'Port 1776' in bridge_journal or 'Connection refused' in bridge_journal or 'already in use' in bridge_journal:
        add_issue(
            title='mcp-bridge service repeatedly failing / port conflict',
            driver='ollama_mcp_bridge (Python) / systemd',
            hardware='Local bridge service (port 1776) / Ollama',
            error_text=bridge_journal.strip()[:1200],
            severity='Critical',
            remediations=[
                'Immediate: Confirm Ollama is running: `systemctl status ollama` or check `ps aux | grep ollama`.',
                'Immediate: Identify listener on 1776: `ss -ltnp | grep 1776` and inspect `journalctl -u mcp-bridge.service -n 200 --no-pager`.',
                'Immediate: If a stray process binds 1776, stop it: `fuser -k 1776/tcp` (use with caution).',
                'Short-term: Run the bridge manually under the `venv-bridge` to capture stdout for debugging: `venv-bridge/bin/python -m ollama_mcp_bridge.main --port 1776 --ollama-url http://localhost:11434`.',
                'Mid-term: Create a systemd drop-in `/etc/systemd/system/mcp-bridge.service.d/override.conf` with `After=ollama.service` and `Restart=on-failure`, then `systemctl daemon-reload && systemctl restart mcp-bridge`.'
            ]
        )

    # 3) Intel SOF audio / ASoC errors
    if 'ASoC' in journal_err or 'sof-audio' in dmesg_out or 'snd_soc' in dmesg_out:
        excerpt_err = (dmesg_out + '\n' + journal_err)[:1200]
        add_issue(
            title='Intel SOF ASoC audio errors (iDisp/HDMI)',
            driver='snd_sof_pci_intel_tgl / sof-audio',
            hardware='Intel Audio DSP (PCI 00:1f.3) — iDisp/HDMI paths',
            error_text=excerpt_err,
            severity='Warning',
            remediations=[
                'Immediate: Ensure `sof-firmware` is installed: `dnf install -y sof-firmware` and verify `/lib/firmware/intel/sof/` files.',
                'Immediate: Restart user audio services (e.g. `systemctl --user restart pipewire`) and check `dmesg` for ABI mismatch.',
                'Short-term: Test legacy HDA driver as a workaround: add kernel param `snd-intel-dspcfg.dsp_driver=1` in grub and reboot (maintenance window).',
                'Mid-term: Align kernel and firmware versions by applying vendor/kernel updates together.'
            ]
        )

    # 3b) Kernel panic / OOPS / BUG traces scanning in full dmesg
    if any(x in full_dmesg for x in ('Kernel panic', 'BUG:', 'Call Trace', 'Oops:', 'BUG: unable to handle')):
        excerpt_err = full_dmesg[:1200]
        add_issue(
            title='Kernel OOPS / panic traces detected',
            driver='kernel',
            hardware='system kernel',
            error_text=excerpt_err,
            severity='Critical',
            remediations=[
                'Immediate: Capture full dmesg and /var/log/messages for post-mortem.',
                'Immediate: If recurring, schedule maintenance/rollback and avoid production workload on this host.',
                'Short-term: Gather kernel and module versions; reproduce under controlled conditions for vendor support.'
            ]
        )

    # 4) Unmaintained drivers detected
    if 'Unmaintained driver' in journal_err or 'unmaintained driver' in dmesg_out or 'cnic' in dmesg_out or 'bnx2i' in dmesg_out:
        snippet = (dmesg_out + '\n' + journal_err)[:800]
        add_issue(
            title='Unmaintained kernel driver(s) detected',
            driver='cnic / bnx2i (as reported)',
            hardware='Network / iSCSI adapters',
            error_text=snippet,
            severity='Warning',
            remediations=[
                'Immediate: Check `lsmod | grep cnic` and `lspci -k` to see if the driver is actively in use before blacklisting.',
                'Immediate (cautious): To suppress noisy modules, create `/etc/modprobe.d/blacklist-<driver>.conf` containing `blacklist <driver>` and reboot (only if confirmed unused).',
                'Short-term: Update kernel and vendor packages via `dnf update` to obtain supported drivers.',
                'Mid-term: Work with hardware vendor for driver/firmware alignment or move to supported kernel streams.'
            ]
        )

    # 5) NVMe SMART & unsafe shutdowns
    if smart_out and smart_out != 'NO_NVME':
        us = re.search(r'Unsafe\s+Shutdowns:\s*(\d+)', smart_out)
        pct_used = re.search(r'Percentage\s+Used:\s*(\d+)%?', smart_out)
        if us and int(us.group(1)) > 0:
            errtxt = f"Unsafe shutdown count: {us.group(1)}; SMART summary present."
            if pct_used:
                errtxt += f" Percentage Used: {pct_used.group(1)}%"
            add_issue(
                title='NVMe: unsafe shutdowns / SMART warnings',
                driver='nvme / smartctl',
                hardware='NVMe device',
                error_text=errtxt + '\n' + smart_out[:600],
                severity='Warning',
                remediations=[
                    'Immediate: Backup critical data and reduce I/O load; inspect system logs for power/panic events.',
                    'Immediate: Run `smartctl -a /dev/nvme0n1` and review reallocated/critical attributes.',
                    'Short-term: Run `smartctl -t short /dev/nvme0n1` and re-check results; schedule replacement if metrics worsen.',
                    'Mid-term: Investigate power delivery and controller stability that cause unsafe shutdowns.'
                ]
            )

    # 6) Flatpak extraction ownership failures
    if 'Cannot change ownership to uid' in flatpak_journal or 'Cannot change ownership' in flatpak_journal:
        add_issue(
            title='Flatpak extraction ownership failures',
            driver='flatpak-system-helper / tar',
            hardware='Flatpak cache / filesystem mounts',
            error_text=flatpak_journal.strip()[:800],
            severity='Info',
            remediations=[
                'Immediate: Inspect mount options for target filesystem: `mount | grep /var/lib/flatpak` and check if `nouid` or similar is set.',
                'Immediate: Run `flatpak repair` to attempt automatic fixes.',
                'Short-term: Move Flatpak cache to a compatible filesystem or adjust mount options.',
                'Mid-term: Document supported filesystems for Flatpak operations in deployment guides.'
            ]
        )

    # Sort issues by a severity -> priority mapping
    priority = {'Critical': 1, 'High': 2, 'Warning': 3, 'Info': 4}
    issues_sorted = sorted(issues, key=lambda x: priority.get(x.get('severity', 'Info'), 10))

    # Build formatted report
    node = platform.node() or run_cmd(['hostname'])
    now = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()
    # try to get OS VERSION_ID
    osrel = run_cmd(['cat', '/etc/os-release']) or ''
    ver = 'unknown'
    m = re.search(r'VERSION_ID="?([0-9.]+)"?', osrel)
    if m:
        ver = m.group(1)
    # try product/serial
    product = run_cmd(['cat', '/sys/class/dmi/id/product_name']) or 'unknown'
    serial = run_cmd(['cat', '/sys/class/dmi/id/product_serial']) or 'unknown'

    lines = []
    lines.append(f"The {node} {now} RHEL {ver} {product} {serial} is having these problems:")

    for idx, it in enumerate(issues_sorted, start=1):
        lines.append(f"{idx}. {it['title']} (driver: {it['driver']}; hardware: {it['hardware']}; Type: {it['severity']})")
        # include a short error excerpt
        err_excerpt = (it['error_text'] or '').splitlines()
        if err_excerpt:
            lines.append(f"   Error: {err_excerpt[0][:300]}")
        lines.append('   Remediation (immediate-first):')
        for r in it['remediations']:
            lines.append(f"     - {r}")
        lines.append('')

    # Append short excerpts for context
    def excerpt(s, lines_n=6):
        return '\n'.join((s or '').splitlines()[:lines_n])

    lines.append('\n--- sensors (top) ---')
    lines.append(excerpt(sensors_out, 6))
    lines.append('\n--- journal (errors, top) ---')
    lines.append(excerpt(journal_err, 8))
    lines.append('\n--- dmesg (errors, top) ---')
    lines.append(excerpt(dmesg_out, 8))
    lines.append('\n--- dmesg (full, excerpt) ---')
    lines.append(excerpt(full_dmesg, 12))
    lines.append('\n--- lspci (excerpt) ---')
    lines.append(excerpt(lspci_out, 12))
    if ipmi_out:
        lines.append('\n--- ipmitool sdr (excerpt) ---')
        lines.append(excerpt(ipmi_out, 30))
    if lldp_out:
        lines.append('\n--- lldp neighbors ---')
        lines.append(excerpt(lldp_out, 40))
    if smart_out != 'NO_NVME':
        lines.append('\n--- nvme smart (excerpt) ---')
        lines.append(excerpt(smart_out, 20))

    return '\n'.join(lines)


@mcp.tool()
def security_diagnostics():
    """Collect prioritized security findings and remediation steps.

    Produces the same "The <host> <ts> RHEL <ver> <product> <serial> is having these problems:" format
    with remediation lists ordered immediate-first under each problem.
    """
    import re, datetime

    ssh_journal = run_cmd(['journalctl', '-u', 'sshd', '--since', '24 hours', '--no-pager']) or ''
    sudo_journal = run_cmd(['journalctl', '-t', 'sudo', '--since', '24 hours', '--no-pager']) or ''
    auth_journal = run_cmd(['journalctl', '-b', '-p', '4', '--no-pager']) or ''
    ss_listen = run_cmd(['ss', '-ltnp']) or ''
    selinux_state = run_cmd(['getenforce']) or ''
    firewall_state = run_cmd(['systemctl', 'is-active', 'firewalld']) or ''
    auditctl_status = run_cmd(['auditctl', '-s']) or ''
    dnf_updates = run_cmd(['dnf', 'check-update']) or ''

    issues = []

    def add_issue(title, driver, hardware, error_text, severity, remediations):
        issues.append({
            'title': title,
            'driver': driver,
            'hardware': hardware,
            'error_text': error_text,
            'severity': severity,
            'remediations': remediations,
        })

    # SSH/Brute-force detection
    failed = len(re.findall(r'Failed password', ssh_journal, re.I))
    accepted = len(re.findall(r'Accepted password|Accepted publickey', ssh_journal, re.I))
    root_accepted = len(re.findall(r'Accepted .* for root', ssh_journal, re.I))
    if failed > 20:
        add_issue(
            title=f'Multiple SSH failed logins ({failed} in 24h)',
            driver='sshd',
            hardware='SSH service',
            error_text=ssh_journal.strip()[:800],
            severity='Critical' if failed > 100 else 'High',
            remediations=[
                'Immediate: Block offending IPs (example): `firewall-cmd --permanent --add-rich-rule="rule family=\"ipv4\" source address=1.2.3.4 reject" && firewall-cmd --reload` or use `fail2ban-client set sshd banip <IP>`.',
                'Immediate: Harden SSH: set `PermitRootLogin no` and `PasswordAuthentication no` in /etc/ssh/sshd_config and `systemctl restart sshd`.',
                'Short-term: Install and enable `fail2ban` (`dnf install -y fail2ban && systemctl enable --now fail2ban`) and review `/var/log/secure` for sources.',
                'Mid-term: Enforce key-based auth, MFA, and centralized authentication (e.g., LDAP with 2FA).'
            ]
        )
    if root_accepted:
        add_issue(
            title='Remote root login(s) observed',
            driver='sshd',
            hardware='SSH / root account',
            error_text=ssh_journal.strip()[:800],
            severity='Critical',
            remediations=[
                "Immediate: Remove any SSH keys in root's `~/.ssh/authorized_keys`, set `PermitRootLogin no` in /etc/ssh/sshd_config and `systemctl restart sshd`.",
                'Short-term: Rotate exposed credentials, review `last`/`journalctl` for root sessions, and audit recent root actions.',
                'Mid-term: Implement centralized privileged access management and require sudo for admin tasks.'
            ]
        )

    # Sudo/auth failures
    sudo_fails = len(re.findall(r'authentication failure|FAILED', sudo_journal, re.I))
    if sudo_fails > 5:
        add_issue(
            title=f'Sudo authentication failures ({sudo_fails} in 24h)',
            driver='sudo',
            hardware='sudo/auth subsystem',
            error_text=sudo_journal.strip()[:800],
            severity='Warning',
            remediations=[
                'Immediate: Investigate accounts causing failures (`journalctl -t sudo -n 200`) and verify legitimacy.',
                'Short-term: Enforce stronger auth (MFA) for privileged accounts and minimize sudoers entries.',
                'Mid-term: Ship sudo logs to a central SIEM/ELK for long-term audit.'
            ]
        )

    # SELinux / Firewall
    if selinux_state.strip() != 'Enforcing':
        add_issue(
            title=f'SELinux not enforcing ({selinux_state.strip()})',
            driver='selinux',
            hardware='OS security policy',
            error_text=selinux_state.strip(),
            severity='High',
            remediations=[
                'Immediate: If safe, enable SELinux enforcing (`setenforce 1`) and test critical services for denials.',
                'Short-term: Use `ausearch`/`audit2allow` to triage denials and create targeted policy exceptions rather than disabling SELinux.',
                'Mid-term: Integrate SELinux policy checks into CI/CD to avoid regressions.'
            ]
        )
    if firewall_state.strip() != 'active':
        add_issue(
            title=f'Firewall inactive ({firewall_state.strip()})',
            driver='firewalld',
            hardware='host firewall',
            error_text=firewall_state.strip(),
            severity='High',
            remediations=[
                'Immediate: Start/enable firewalld: `systemctl start firewalld && systemctl enable firewalld` and confirm rules.',
                'Short-term: Apply a minimal allowlist policy (open only required ports such as SSH) and block everything else.',
                'Mid-term: Manage firewall rules via configuration management (Ansible) and enforce across fleet.'
            ]
        )

    # Auditd
    if 'enabled' not in auditctl_status.lower():
        add_issue(
            title='Audit subsystem not enabled or not reporting',
            driver='auditd/auditctl',
            hardware='audit subsystem',
            error_text=auditctl_status.strip()[:800],
            severity='Warning',
            remediations=[
                'Immediate: Confirm auditd is running and recording events (`systemctl status auditd`); start it if needed: `systemctl enable --now auditd`.',
                'Short-term: Add persistent audit rules (auth, exec) and verify with `auditctl -l`.',
                'Mid-term: Forward audit logs to a central collector for retention and alerting.'
            ]
        )

    # Open/listening ports (informational)
    if ss_listen:
        add_issue(
            title='Listening TCP ports snapshot',
            driver='ss',
            hardware='network stack',
            error_text=ss_listen.strip()[:800],
            severity='Info',
            remediations=[
                'Immediate: Review listening services and close/unbind ports not needed.',
                'Short-term: Ensure services bound to 0.0.0.0 are intended and protected by firewall.',
                'Mid-term: Adopt host-based allowlisting for production systems.'
            ]
        )

    # Outdated package check (informational)
    if dnf_updates and 'ERR:' not in dnf_updates:
        add_issue(
            title='Available package updates',
            driver='dnf',
            hardware='installed packages',
            error_text='Updates available (run `dnf check-update` locally).',
            severity='Info',
            remediations=[
                'Short-term: Review security updates and apply in maintenance windows (`dnf -y update --security`).',
                'Mid-term: Automate regular patching and schedule vulnerability scanning.'
            ]
        )

    # Sort and render similar to hardware_diagnostics
    priority = {'Critical': 1, 'High': 2, 'Warning': 3, 'Info': 4}
    issues_sorted = sorted(issues, key=lambda x: priority.get(x.get('severity', 'Info'), 10))

    node = platform.node() or run_cmd(['hostname'])
    now = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()
    osrel = run_cmd(['cat', '/etc/os-release']) or ''
    ver = 'unknown'
    m = re.search(r'VERSION_ID="?([0-9.]+)"?', osrel)
    if m:
        ver = m.group(1)
    product = run_cmd(['cat', '/sys/class/dmi/id/product_name']) or 'unknown'
    serial = run_cmd(['cat', '/sys/class/dmi/id/product_serial']) or 'unknown'

    lines = []
    lines.append(f"The {node} {now} RHEL {ver} {product} {serial} is having these problems:")
    for idx, it in enumerate(issues_sorted, start=1):
        lines.append(f"{idx}. {it['title']} (driver: {it['driver']}; hardware: {it['hardware']}; Type: {it['severity']})")
        err_excerpt = (it['error_text'] or '').splitlines()
        if err_excerpt:
            lines.append(f"   Error: {err_excerpt[0][:300]}")
        lines.append('   Remediation (immediate-first):')
        for r in it['remediations']:
            lines.append(f"     - {r}")
        lines.append('')

    lines.append('\n--- ssh journal (excerpt) ---')
    lines.append('\n'.join(ssh_journal.splitlines()[:12]))
    lines.append('\n--- sudo journal (excerpt) ---')
    lines.append('\n'.join(sudo_journal.splitlines()[:12]))
    lines.append('\n--- listening ports (ss) ---')
    lines.append('\n'.join(ss_listen.splitlines()[:12]))

    return '\n'.join(lines)


@mcp.tool()
def full_diagnostics_json():
    """Return structured JSON containing hardware and security issues and short raw excerpts.

    This is intended for programmatic consumption by HAL: it returns a JSON string
    with `hardware` and `security` arrays where each entry contains title, driver,
    hardware, error_text (excerpt) severity, and remediations (immediate-first).
    """
    import json, re, datetime

    # Re-run the same probes and detection logic (keeps tool self-contained):
    # Hardware probes
    sensors_out = run_cmd(['sensors', '-u']) or ''
    dmesg_out = run_cmd(['dmesg', '-T', '--level=err,crit,alert,emerg']) or ''
    full_dmesg = run_cmd(['dmesg', '-T']) or ''
    lspci_out = run_cmd(['lspci', '-vvnn']) or ''
    journal_err = run_cmd(['journalctl', '-b', '-p', '3', '--no-pager']) or ''
    bridge_journal = run_cmd(['journalctl', '-u', 'mcp-bridge.service', '-n', '200', '--no-pager']) or ''
    flatpak_journal = run_cmd(['journalctl', '-t', 'flatpak', '-n', '200', '--no-pager']) or ''
    smart_out = 'NO_NVME'
    if os.path.exists('/dev/nvme0n1'):
        smart_out = run_cmd(['smartctl', '-a', '/dev/nvme0n1'], sudo=True) or ''

    hardware_issues = []

    def h_add(title, driver, hardware, error_text, severity, remediations):
        hardware_issues.append({
            'title': title,
            'driver': driver,
            'hardware': hardware,
            'error_text': (error_text or '')[:1200],
            'severity': severity,
            'remediations': remediations,
        })

    # Temperature detection (same refined thresholds)
    try:
        floats = [float(x) for x in re.findall(r'([0-9]+\.[0-9]+)', sensors_out)]
        temps = [t for t in floats if 0 < t < 200]
        max_temp = max(temps) if temps else None
        if max_temp and max_temp >= 95:
            h_add(f'CPU over-temperature: {max_temp}C', 'coretemp / acpitz', 'CPU package', f'Peak {max_temp}C', 'Critical', ['Reduce load: `ps -eo pid,ppid,cmd,%mem,%cpu --sort=-%cpu | head`; verify fans; migrate workloads.'])
        elif max_temp and max_temp >= 85:
            h_add(f'High CPU temperature: {max_temp}C', 'coretemp / acpitz', 'CPU package', f'Peak {max_temp}C', 'High', ['Reduce non-essential load; verify cooling; check dmesg for throttling.'])
    except Exception:
        pass

    # Bridge
    if 'Port 1776' in bridge_journal or 'Connection refused' in bridge_journal or 'already in use' in bridge_journal:
        h_add('mcp-bridge service failing / port conflict', 'ollama_mcp_bridge / systemd', 'mcp-bridge service', bridge_journal[:1200], 'Critical', ['Check Ollama; inspect `ss -ltnp | grep 1776` and `journalctl -u mcp-bridge.service`.'])

    # Audio
    if 'ASoC' in journal_err or 'sof-audio' in dmesg_out or 'snd_soc' in dmesg_out:
        h_add('Intel SOF ASoC audio errors', 'snd_sof_pci_intel_tgl / sof-audio', 'Intel Audio DSP', (dmesg_out + '\n' + journal_err)[:1200], 'Warning', ['Install `sof-firmware` and restart audio services.'])

    # Drivers
    if 'Unmaintained driver' in journal_err or 'cnic' in dmesg_out or 'bnx2i' in dmesg_out:
        h_add('Unmaintained kernel drivers detected', 'cnic / bnx2i', 'Network/iSCSI', (dmesg_out + '\n' + journal_err)[:800], 'Warning', ['Check usage with `lsmod`/`lspci -k` before blacklisting; update kernel/vendor packages.'])

    # NVMe
    if smart_out and smart_out != 'NO_NVME':
        us = re.search(r'Unsafe\s+Shutdowns:\s*(\d+)', smart_out)
        if us and int(us.group(1)) > 0:
            h_add('NVMe: unsafe shutdowns / SMART warnings', 'nvme / smartctl', 'NVMe device', smart_out[:800], 'Warning', ['Backup data; run `smartctl -a /dev/nvme0n1` and `smartctl -t short /dev/nvme0n1`.'])

    if 'Cannot change ownership to uid' in flatpak_journal:
        h_add('Flatpak extraction ownership failures', 'flatpak-system-helper', 'Flatpak cache', flatpak_journal[:800], 'Info', ['Run `flatpak repair` and check filesystem mount options.'])

    # Security probes (lightweight mirroring of security_diagnostics)
    ssh_journal = run_cmd(['journalctl', '-u', 'sshd', '--since', '24 hours', '--no-pager']) or ''
    sudo_journal = run_cmd(['journalctl', '-t', 'sudo', '--since', '24 hours', '--no-pager']) or ''
    ss_listen = run_cmd(['ss', '-ltnp']) or ''
    selinux_state = run_cmd(['getenforce']) or ''
    firewall_state = run_cmd(['systemctl', 'is-active', 'firewalld']) or ''
    auditctl_status = run_cmd(['auditctl', '-s']) or ''
    dnf_updates = run_cmd(['dnf', 'check-update']) or ''

    security_issues = []

    def s_add(title, driver, hardware, error_text, severity, remediations):
        security_issues.append({
            'title': title,
            'driver': driver,
            'hardware': hardware,
            'error_text': (error_text or '')[:1200],
            'severity': severity,
            'remediations': remediations,
        })

    failed = len(re.findall(r'Failed password', ssh_journal, re.I))
    root_accepted = len(re.findall(r'Accepted .* for root', ssh_journal, re.I))
    if failed > 20:
        s_add(f'Multiple SSH failed logins ({failed} in 24h)', 'sshd', 'SSH service', ssh_journal[:800], 'High', ['Block offending IPs; install/enable `fail2ban`; harden SSH config.'])
    if root_accepted:
        s_add('Remote root login(s) observed', 'sshd', 'SSH/root', ssh_journal[:800], 'Critical', ['Remove root SSH keys; set `PermitRootLogin no` and restart sshd; rotate credentials.'])
    sudo_fails = len(re.findall(r'authentication failure|FAILED', sudo_journal, re.I))
    if sudo_fails > 5:
        s_add(f'Sudo authentication failures ({sudo_fails} in 24h)', 'sudo', 'sudo subsystem', sudo_journal[:800], 'Warning', ['Investigate failed sudo attempts and review sudoers.'])
    if selinux_state.strip() != 'Enforcing':
        s_add(f'SELinux not enforcing ({selinux_state.strip()})', 'selinux', 'OS policy', selinux_state.strip(), 'High', ['If safe, enable enforcing: `setenforce 1`; triage denials with `ausearch`/`audit2allow`.'])
    if firewall_state.strip() != 'active':
        s_add(f'Firewall inactive ({firewall_state.strip()})', 'firewalld', 'host firewall', firewall_state.strip(), 'High', ['Start/enable firewalld and apply minimal allowlist for required services.'])
    if 'enabled' not in auditctl_status.lower():
        s_add('Audit subsystem not enabled or not reporting', 'auditd', 'audit subsystem', auditctl_status[:800], 'Warning', ['Start/enable auditd and configure persistent audit rules.'])
    if dnf_updates and 'ERR:' not in dnf_updates:
        s_add('Available package updates', 'dnf', 'installed packages', 'Updates available', 'Info', ['Review and apply security updates in maintenance windows.'])

    node = platform.node() or run_cmd(['hostname'])
    now = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc).isoformat()
    osrel = run_cmd(['cat', '/etc/os-release']) or ''
    ver = 'unknown'
    m = re.search(r'VERSION_ID="?([0-9.]+)"?', osrel)
    if m:
        ver = m.group(1)
    product = run_cmd(['cat', '/sys/class/dmi/id/product_name']) or 'unknown'
    serial = run_cmd(['cat', '/sys/class/dmi/id/product_serial']) or 'unknown'

    report = {
        'host': node,
        'timestamp': now,
        'os_version': ver,
        'product': product,
        'serial': serial,
        'hardware': hardware_issues,
        'security': security_issues,
        'excerpts': {
            'sensors': (sensors_out or '')[:2000],
            'dmesg': (full_dmesg or '')[:2000],
            'lspci': (lspci_out or '')[:2000],
            'ssh_journal': (ssh_journal or '')[:2000],
        }
    }

    return json.dumps(report, indent=2)

if __name__ == "__main__":
    mcp.run()
