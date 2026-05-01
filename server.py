import sys, os, subprocess, psutil, platform, shutil, json, time, socket, hashlib
from functools import wraps
from threading import Lock
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("P-Series-Architect")

AI_HOME = os.path.join(os.path.expanduser('~'), '.mcp-ai')
REPORTS_DIR = os.path.join(AI_HOME, 'reports')
AUDIT_LOG = os.path.join(REPORTS_DIR, 'mcp-server-audit.jsonl')
BASE_DIR = os.path.dirname(os.path.realpath(__file__))


DEFAULT_SERVER_CONFIG = {
    'configVersion': 2,
    'environment': 'prod',
    'defaultCommandTimeoutSec': 60,
    'auditLogPath': AUDIT_LOG,
    'redactSecretsInAudit': True,
    'audit': {
        'maxAuditFileMB': 25,
        'maxAuditFiles': 8,
    },
    'limits': {
        'maxConcurrentCommands': 4,
        'rateLimitPerMinute': 120,
    },
    'security': {
        'requireApprovalForHighRisk': True,
        'approvalEnvVar': 'MCP_APPROVE_HIGH_RISK',
        'highRiskTools': ['system_evolution', 'predict_failure_and_evacuate'],
        'disabledTools': [],
    },
    'health': {
        'minAvailableMemoryMB': 512,
        'minRootFreeGB': 2,
        'bridgeHost': '127.0.0.1',
        'bridgePort': 1776,
        'ollamaHost': '127.0.0.1',
        'ollamaPort': 11434,
        'bridgeServiceName': 'mcp-bridge.service',
        'ollamaServiceName': 'ollama.service',
    },
    'toolCommandTimeoutSec': {
        'optimize_ai_performance': {
            'cpupower': 45,
            'nvidia': 30,
            'default': 60,
        },
        'system_evolution': {
            'upgrade': 2400,
            'akmods': 1800,
            'default': 2400,
        },
        'generate_ansible_manifest': {
            'repoquery': 120,
            'default': 120,
        },
        'predict_failure_and_evacuate': {
            'smartctl': 45,
            'git': 60,
            'default': 90,
        },
        'sentinel_scan': {
            'journal': 30,
            'default': 30,
        },
    },
}

_LIMITS_LOCK = Lock()
_ACTIVE_COMMANDS = 0
_RATE_LIMIT_TS = []


def _load_server_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_SERVER_CONFIG))
    cfg_path = os.path.join(BASE_DIR, 'mcp-config.json')
    try:
        with open(cfg_path, 'r', encoding='utf-8') as fh:
            raw = json.load(fh)
        settings = (
            raw.get('mcpServers', {})
               .get('architect', {})
               .get('settings', {})
        )
        if isinstance(settings, dict):
            cfg.update({k: v for k, v in settings.items() if k not in ('health', 'audit', 'limits', 'security')})
            if isinstance(settings.get('health'), dict):
                cfg['health'].update(settings['health'])
            if isinstance(settings.get('audit'), dict):
                cfg['audit'].update(settings['audit'])
            if isinstance(settings.get('limits'), dict):
                cfg['limits'].update(settings['limits'])
            if isinstance(settings.get('security'), dict):
                cfg['security'].update(settings['security'])
    except Exception:
        pass
    return cfg


SERVER_CONFIG = _load_server_config()


def _utc_now_iso() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _ensure_dirs() -> None:
    os.makedirs(REPORTS_DIR, exist_ok=True)


def _redact_text(value: str) -> str:
    if not SERVER_CONFIG.get('redactSecretsInAudit', True):
        return (value or '')[:2000]
    if not value:
        return value
    # Basic secret masking for logs.
    value = value.replace('\n', ' ')
    value = value[:2000]
    value = value.replace('password=', 'password=***')
    value = value.replace('--password', '--password ***')
    value = value.replace('token=', 'token=***')
    value = value.replace('Authorization:', 'Authorization: ***')
    return value


def _audit_event(event: str, details: dict) -> None:
    try:
        _ensure_dirs()
        audit_path = SERVER_CONFIG.get('auditLogPath') or AUDIT_LOG
        audit_dir = os.path.dirname(audit_path)
        if audit_dir:
            os.makedirs(audit_dir, exist_ok=True)
        payload = {
            'timestamp': _utc_now_iso(),
            'event': event,
            'details': details,
        }
        _rotate_audit_log_if_needed(audit_path)
        with open(audit_path, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(payload) + '\n')
    except Exception:
        pass


def _rotate_audit_log_if_needed(audit_path: str) -> None:
    try:
        audit_cfg = SERVER_CONFIG.get('audit', {}) or {}
        max_mb = int(audit_cfg.get('maxAuditFileMB', 25) or 25)
        max_files = int(audit_cfg.get('maxAuditFiles', 8) or 8)
        if max_files < 2:
            max_files = 2
        if not os.path.exists(audit_path):
            return
        size = os.path.getsize(audit_path)
        if size <= max_mb * 1024 * 1024:
            return
        oldest = f"{audit_path}.{max_files-1}"
        if os.path.exists(oldest):
            os.remove(oldest)
        for i in range(max_files - 2, 0, -1):
            src = f"{audit_path}.{i}"
            dst = f"{audit_path}.{i+1}"
            if os.path.exists(src):
                os.replace(src, dst)
        os.replace(audit_path, f"{audit_path}.1")
    except Exception:
        pass


def _acquire_command_slot() -> tuple[bool, str | None]:
    global _ACTIVE_COMMANDS, _RATE_LIMIT_TS
    limits = SERVER_CONFIG.get('limits', {}) or {}
    max_concurrent = int(limits.get('maxConcurrentCommands', 4) or 4)
    rate_per_min = int(limits.get('rateLimitPerMinute', 120) or 120)
    now = time.time()

    with _LIMITS_LOCK:
        _RATE_LIMIT_TS = [t for t in _RATE_LIMIT_TS if now - t <= 60]
        if rate_per_min > 0 and len(_RATE_LIMIT_TS) >= rate_per_min:
            return (False, f'ERR: rate limit exceeded ({rate_per_min}/min)')
        if max_concurrent > 0 and _ACTIVE_COMMANDS >= max_concurrent:
            return (False, f'ERR: max concurrent commands reached ({max_concurrent})')
        _ACTIVE_COMMANDS += 1
        _RATE_LIMIT_TS.append(now)
        return (True, None)


def _release_command_slot() -> None:
    global _ACTIVE_COMMANDS
    with _LIMITS_LOCK:
        _ACTIVE_COMMANDS = max(0, _ACTIVE_COMMANDS - 1)


def _tool_guard(tool_name: str) -> str | None:
    sec = SERVER_CONFIG.get('security', {}) or {}
    disabled = set(sec.get('disabledTools', []) or [])
    if tool_name in disabled:
        return f'Blocked by policy: tool {tool_name} is disabled in mcp-config.json.'

    high_risk = set(sec.get('highRiskTools', []) or [])
    require_approval = bool(sec.get('requireApprovalForHighRisk', True))
    if require_approval and tool_name in high_risk:
        env_var = str(sec.get('approvalEnvVar', 'MCP_APPROVE_HIGH_RISK'))
        if os.environ.get(env_var, '0') != '1':
            return (
                f'Blocked by policy: {tool_name} requires approval. '
                f'Set {env_var}=1 for this invocation.'
            )
    return None


def _guarded_tool(tool_name: str):
    """Apply policy checks consistently across all MCP tools."""
    def _decorator(func):
        @wraps(func)
        def _wrapped(*args, **kwargs):
            blocked = _tool_guard(tool_name)
            if blocked:
                _audit_event('policy-deny', {'tool': tool_name, 'reason': blocked})
                return _tool_err(
                    tool_name,
                    'POLICY_BLOCKED',
                    blocked,
                    details={'policy': 'security'},
                    retryable=False,
                    error_category='policy',
                    action_hint='Review mcp-config.json security settings or set required approval environment variable.',
                )
            return func(*args, **kwargs)
        return _wrapped
    return _decorator


def _resolve_timeout(tool_name: str, operation: str | None = None, fallback: int | None = None) -> int:
    def _to_positive_int(value) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return max(1, value)
        if isinstance(value, str):
            try:
                return max(1, int(value.strip()))
            except Exception:
                return None
        return None

    default_timeout = int(SERVER_CONFIG.get('defaultCommandTimeoutSec', 60) or 60)
    if fallback is not None:
        default_timeout = int(fallback)
    tool_cfg = (SERVER_CONFIG.get('toolCommandTimeoutSec', {}) or {}).get(tool_name)
    if isinstance(tool_cfg, dict):
        if operation:
            op_timeout = _to_positive_int(tool_cfg.get(operation))
            if op_timeout is not None:
                return op_timeout
        default_tool_timeout = _to_positive_int(tool_cfg.get('default'))
        if default_tool_timeout is not None:
            return default_tool_timeout
    else:
        tool_timeout = _to_positive_int(tool_cfg)
        if tool_timeout is not None:
            return tool_timeout
    return max(1, default_timeout)


def _tool_ok(tool_name: str, message: str, data: dict | None = None, code: str = 'OK', action_hint: str | None = None) -> str:
    payload = {
        'ok': True,
        'tool': tool_name,
        'code': code,
        'message': message,
        'timestamp': _utc_now_iso(),
    }
    if data is not None:
        payload['data'] = data
    if action_hint:
        payload['action_hint'] = action_hint
    return json.dumps(payload, indent=2)


def _tool_err(tool_name: str, code: str, message: str, details: dict | None = None, retryable: bool = False, error_category: str = 'runtime', action_hint: str | None = None) -> str:
    payload = {
        'ok': False,
        'tool': tool_name,
        'code': code,
        'message': message,
        'retryable': bool(retryable),
        'error_category': error_category,
        'timestamp': _utc_now_iso(),
    }
    if details is not None:
        payload['details'] = details
    if action_hint:
        payload['action_hint'] = action_hint
    return json.dumps(payload, indent=2)


def _cmd_details(step: str, result: dict) -> dict:
    stdout = str(result.get('stdout', '') or '')
    stderr = str(result.get('stderr', '') or '')
    return {
        'step': step,
        'command': result.get('command'),
        'rc': result.get('rc'),
        'error': result.get('error'),
        'stdout_head': '\n'.join(stdout.splitlines()[:8]),
        'stderr_head': '\n'.join(stderr.splitlines()[:8]),
        'duration_ms': result.get('duration_ms'),
    }


def run_cmd_result(cmd, sudo=False, timeout=None, dry_run=False, idempotency_key=None, accepted_rcs=None):
    """Run command and return structured execution result.

    Returns dict with keys: ok, rc, stdout, stderr, error, duration_ms, command.
    """
    if timeout is None:
        timeout = int(SERVER_CONFIG.get('defaultCommandTimeoutSec', 60) or 60)
    cmd = list(cmd)
    if sudo:
        cmd = ['sudo'] + cmd
    cmd_text = ' '.join(cmd)
    cmd_hash = hashlib.sha256(cmd_text.encode('utf-8')).hexdigest()[:12]
    t0 = time.time()
    ok, err = _acquire_command_slot()
    if not ok:
        _audit_event('command', {
            'command': _redact_text(cmd_text),
            'command_hash': cmd_hash,
            'sudo': bool(sudo),
            'timeout_sec': int(timeout),
            'dry_run': bool(dry_run),
            'idempotency_key': idempotency_key,
            'status': 'rejected',
            'reason': err,
            'duration_ms': 0,
        })
        return {
            'ok': False,
            'rc': None,
            'stdout': '',
            'stderr': '',
            'error': err or 'ERR: command rejected by limits',
            'duration_ms': 0,
            'command': cmd,
        }
    if dry_run:
        msg = f"DRY-RUN: {cmd_text}"
        _audit_event('command', {
            'command': _redact_text(cmd_text),
            'command_hash': cmd_hash,
            'sudo': bool(sudo),
            'timeout_sec': int(timeout),
            'dry_run': True,
            'idempotency_key': idempotency_key,
            'status': 'dry-run',
            'duration_ms': 0,
        })
        _release_command_slot()
        return {
            'ok': True,
            'rc': 0,
            'stdout': msg,
            'stderr': '',
            'error': None,
            'duration_ms': 0,
            'command': cmd,
        }
    if accepted_rcs is None:
        accepted_rcs = [0]
    accepted_rcs = {int(x) for x in accepted_rcs}

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)
        took_ms = int((time.time() - t0) * 1000)
        rc = int(res.returncode)
        success = rc in accepted_rcs
        _audit_event('command', {
            'command': _redact_text(cmd_text),
            'command_hash': cmd_hash,
            'sudo': bool(sudo),
            'timeout_sec': int(timeout),
            'dry_run': False,
            'idempotency_key': idempotency_key,
            'status': 'ok' if success else 'error',
            'rc': rc,
            'duration_ms': took_ms,
        })
        _release_command_slot()
        stderr_text = (res.stderr or '').strip()
        error_text = None if success else (f'ERR: command failed with exit code {rc}' + (f' ({stderr_text})' if stderr_text else ''))
        return {
            'ok': success,
            'rc': rc,
            'stdout': (res.stdout or '').strip(),
            'stderr': stderr_text,
            'error': error_text,
            'duration_ms': took_ms,
            'command': cmd,
        }
    except Exception as e:
        took_ms = int((time.time() - t0) * 1000)
        _audit_event('command', {
            'command': _redact_text(cmd_text),
            'command_hash': cmd_hash,
            'sudo': bool(sudo),
            'timeout_sec': int(timeout),
            'dry_run': False,
            'idempotency_key': idempotency_key,
            'status': 'error',
            'duration_ms': took_ms,
            'error': _redact_text(str(e)),
        })
        _release_command_slot()
        return {
            'ok': False,
            'rc': None,
            'stdout': '',
            'stderr': '',
            'error': f"ERR: {str(e)}",
            'duration_ms': took_ms,
            'command': cmd,
        }


def run_cmd(cmd, sudo=False, timeout=None, dry_run=False, idempotency_key=None):
    """Compatibility wrapper returning string output used by existing callers."""
    res = run_cmd_result(
        cmd,
        sudo=sudo,
        timeout=timeout,
        dry_run=dry_run,
        idempotency_key=idempotency_key,
        accepted_rcs=[0],
    )
    if res.get('ok', False):
        return (res.get('stdout') or '').strip()
    return res.get('error') or 'ERR: command failed'


def _check_local_port(host: str, port: int, timeout: float = 0.8) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _service_state(name: str) -> str:
    if not shutil.which('systemctl'):
        return 'unknown'
    state = run_cmd(['systemctl', 'is-active', name])
    return (state or 'unknown').strip()


def _mcp_health_snapshot() -> dict:
    health_cfg = SERVER_CONFIG.get('health', {}) or {}
    vm = psutil.virtual_memory()
    du = shutil.disk_usage('/')
    min_mem_mb = int(health_cfg.get('minAvailableMemoryMB', 512) or 512)
    min_root_free_gb = int(health_cfg.get('minRootFreeGB', 2) or 2)
    bridge_host = str(health_cfg.get('bridgeHost', '127.0.0.1'))
    bridge_port = int(health_cfg.get('bridgePort', 1776) or 1776)
    ollama_host = str(health_cfg.get('ollamaHost', '127.0.0.1'))
    ollama_port = int(health_cfg.get('ollamaPort', 11434) or 11434)
    bridge_service = str(health_cfg.get('bridgeServiceName', 'mcp-bridge.service'))
    ollama_service = str(health_cfg.get('ollamaServiceName', 'ollama.service'))

    bridge_up = _check_local_port(bridge_host, bridge_port)
    ollama_up = _check_local_port(ollama_host, ollama_port)

    checks = {
        'python': {
            'ok': True,
            'version': sys.version.split()[0],
        },
        'memory': {
            'ok': vm.available > min_mem_mb * 1024 * 1024,
            'available_mb': int(vm.available / (1024 * 1024)),
            'percent_used': float(vm.percent),
        },
        'disk_root': {
            'ok': du.free > min_root_free_gb * 1024 * 1024 * 1024,
            'free_gb': round(du.free / (1024 ** 3), 2),
            'total_gb': round(du.total / (1024 ** 3), 2),
        },
        f'bridge_port_{bridge_port}': {
            'ok': bridge_up,
        },
        f'ollama_port_{ollama_port}': {
            'ok': ollama_up,
        },
        'service_mcp_bridge': {
            'ok': _service_state(bridge_service) in ('active', 'activating'),
            'state': _service_state(bridge_service),
        },
        'service_ollama': {
            'ok': _service_state(ollama_service) in ('active', 'activating'),
            'state': _service_state(ollama_service),
        },
        'cmd_journalctl': {'ok': bool(shutil.which('journalctl'))},
        'cmd_smartctl': {'ok': bool(shutil.which('smartctl'))},
        'cmd_sensors': {'ok': bool(shutil.which('sensors'))},
        'cmd_ss': {'ok': bool(shutil.which('ss'))},
    }

    status = 'healthy' if all(v.get('ok', False) for v in checks.values()) else 'degraded'
    return {
        'status': status,
        'timestamp': _utc_now_iso(),
        'host': platform.node(),
        'platform': platform.platform(),
        'checks': checks,
    }


def _read_last_lines(path: str, max_lines: int) -> list[str]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as fh:
            lines = fh.readlines()
        return [ln.rstrip('\n') for ln in lines[-max_lines:]]
    except Exception:
        return []


def _issue_counts(issues: list[dict]) -> dict:
    counts = {'critical': 0, 'high': 0, 'warning': 0, 'info': 0}
    for issue in issues:
        sev = str(issue.get('severity', '')).strip().lower()
        if sev in counts:
            counts[sev] += 1
    counts['total'] = len(issues)
    return counts


def _load_full_diag_payload() -> dict:
    """Load canonical diagnostics payload generated by full_diagnostics_json."""
    try:
        payload = full_diagnostics_json()
        data = json.loads(payload)
        if not isinstance(data, dict):
            return {'_error': 'ERR: diagnostics payload is not a JSON object', 'hardware': [], 'security': [], 'excerpts': {}}
        # Handle structured tool envelopes (phase-2/phase-3 style).
        if 'ok' in data and 'tool' in data and not data.get('ok', True):
            message = str(data.get('message', 'ERR: diagnostics payload reported an error'))
            code = str(data.get('code', 'UNKNOWN'))
            return {'_error': f'{code}: {message}', 'hardware': [], 'security': [], 'excerpts': {}}
        data.setdefault('hardware', [])
        data.setdefault('security', [])
        data.setdefault('excerpts', {})
        return data
    except Exception as exc:
        return {'_error': f'ERR: failed to load diagnostics payload: {exc}', 'hardware': [], 'security': [], 'excerpts': {}}


def _render_issue_report(section_name: str, issues: list, excerpts: dict, host: str, timestamp: str, os_version: str, product: str, serial: str) -> str:
    lines = []
    lines.append(f"The {host} {timestamp} RHEL {os_version} {product} {serial} is having these problems:")
    for idx, it in enumerate(issues, start=1):
        lines.append(
            f"{idx}. {it.get('title', 'Unknown issue')} "
            f"(driver: {it.get('driver', 'unknown')}; hardware: {it.get('hardware', 'unknown')}; Type: {it.get('severity', 'Info')})"
        )
        err_text = str(it.get('error_text', '') or '')
        err_excerpt = err_text.splitlines()
        if err_excerpt:
            lines.append(f"   Error: {err_excerpt[0][:300]}")
        lines.append('   Remediation (immediate-first):')
        for r in (it.get('remediations') or []):
            lines.append(f"     - {r}")
        lines.append('')

    if section_name == 'hardware':
        lines.append('\n--- sensors (top) ---')
        lines.append('\n'.join(str(excerpts.get('sensors', '')).splitlines()[:8]))
        lines.append('\n--- dmesg (full, excerpt) ---')
        lines.append('\n'.join(str(excerpts.get('dmesg', '')).splitlines()[:12]))
        lines.append('\n--- lspci (excerpt) ---')
        lines.append('\n'.join(str(excerpts.get('lspci', '')).splitlines()[:12]))
    else:
        lines.append('\n--- ssh journal (excerpt) ---')
        lines.append('\n'.join(str(excerpts.get('ssh_journal', '')).splitlines()[:12]))

    return '\n'.join(lines)

# -- EVOLUTION & PERFORMANCE --
@mcp.tool()
@_guarded_tool('optimize_ai_performance')
def optimize_ai_performance():
    """Sets CPU to performance and enables NVIDIA persistence."""
    tool_name = 'optimize_ai_performance'
    cpu_res = run_cmd_result(
        ['cpupower', 'frequency-set', '-g', 'performance'],
        sudo=True,
        timeout=_resolve_timeout(tool_name, 'cpupower', 45),
    )
    if not cpu_res.get('ok'):
        return _tool_err(
            tool_name,
            'CPU_GOVERNOR_SET_FAILED',
            'Failed to set CPU governor to performance mode.',
            details=_cmd_details('cpupower', cpu_res),
            retryable=True,
        )

    nvidia_attempted = False
    nvidia_result = None
    if os.path.exists('/dev/nvidia0'):
        nvidia_attempted = True
        nvidia_res = run_cmd_result(
            ['nvidia-smi', '-pm', '1'],
            sudo=True,
            timeout=_resolve_timeout(tool_name, 'nvidia', 30),
        )
        nvidia_result = _cmd_details('nvidia-smi', nvidia_res)
        if not nvidia_res.get('ok'):
            return _tool_err(
                tool_name,
                'NVIDIA_PERSISTENCE_ENABLE_FAILED',
                'CPU governor was set, but enabling NVIDIA persistence mode failed.',
                details={
                    'cpupower': _cmd_details('cpupower', cpu_res),
                    'nvidia': nvidia_result,
                },
                retryable=True,
            )

    return _tool_ok(
        tool_name,
        'Performance optimized for AI workloads.',
        data={
            'cpupower': _cmd_details('cpupower', cpu_res),
            'nvidia_attempted': nvidia_attempted,
            'nvidia': nvidia_result,
        },
    )

@mcp.tool()
@_guarded_tool('system_evolution')
def system_evolution():
    """Updates Kernel/Drivers and ensures akmods are built."""
    tool_name = 'system_evolution'
    update_res = run_cmd_result(
        ['dnf', 'upgrade', '-y', 'kernel*', '*nvidia*'],
        sudo=True,
        timeout=_resolve_timeout(tool_name, 'upgrade', 2400),
    )
    if not update_res.get('ok'):
        return _tool_err(
            tool_name,
            'DNF_UPGRADE_FAILED',
            'Kernel/driver upgrade failed.',
            details=_cmd_details('dnf-upgrade', update_res),
            retryable=True,
        )

    akmods_res = run_cmd_result(
        ['akmods', '--force'],
        sudo=True,
        timeout=_resolve_timeout(tool_name, 'akmods', 1800),
    )
    if not akmods_res.get('ok'):
        return _tool_err(
            tool_name,
            'AKMODS_BUILD_FAILED',
            'Kernel/driver upgrade completed, but akmods build failed.',
            details={
                'upgrade': _cmd_details('dnf-upgrade', update_res),
                'akmods': _cmd_details('akmods', akmods_res),
            },
            retryable=True,
        )

    return _tool_ok(
        tool_name,
        'Kernel/driver upgrade and akmods build completed.',
        data={
            'upgrade': _cmd_details('dnf-upgrade', update_res),
            'akmods': _cmd_details('akmods', akmods_res),
        },
    )

# -- INFRASTRUCTURE AS CODE (CaC) --
@mcp.tool()
@_guarded_tool('generate_ansible_manifest')
def generate_ansible_manifest():
    """Dumps system state into an Ansible role for future propagation."""
    tool_name = 'generate_ansible_manifest'
    pkgs_res = run_cmd_result(
        ['dnf', 'repoquery', '--installed', '--queryformat', '%{name}'],
        timeout=_resolve_timeout(tool_name, 'repoquery', 120),
    )
    if not pkgs_res.get('ok'):
        return _tool_err(
            tool_name,
            'PACKAGE_QUERY_FAILED',
            'Failed to collect installed package list for manifest generation.',
            details=_cmd_details('dnf-repoquery', pkgs_res),
            retryable=True,
        )

    pkgs = str(pkgs_res.get('stdout', '') or '')
    path = os.path.join(BASE_DIR, 'ansible', 'roles', 'p_series_node', 'vars', 'main.yml')
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write("---\ninstalled_packages:\n")
            for p in pkgs.split('\n'):
                if p.strip():
                    f.write(f"  - {p.strip()}\n")
    except Exception as exc:
        return _tool_err(
            tool_name,
            'MANIFEST_WRITE_FAILED',
            'Failed to write Ansible manifest.',
            details={'path': path, 'error': str(exc)},
            retryable=False,
        )

    return _tool_ok(
        tool_name,
        'Ansible manifest generated.',
        data={
            'path': path,
            'package_count': len([p for p in pkgs.split('\n') if p.strip()]),
            'query': _cmd_details('dnf-repoquery', pkgs_res),
        },
    )

# -- SURVIVAL & EXFILTRATION --
@mcp.tool()
@_guarded_tool('predict_failure_and_evacuate')
def predict_failure_and_evacuate():
    """Checks NVMe health; if failing, pushes config to git."""
    tool_name = 'predict_failure_and_evacuate'
    smart_res = run_cmd_result(
        ['smartctl', '-H', '/dev/nvme0n1'],
        sudo=True,
        timeout=_resolve_timeout(tool_name, 'smartctl', 45),
    )
    if not smart_res.get('ok'):
        return _tool_err(
            tool_name,
            'SMARTCTL_FAILED',
            'Unable to evaluate NVMe health status.',
            details=_cmd_details('smartctl-health', smart_res),
            retryable=True,
        )

    smart_out = str(smart_res.get('stdout', '') or '')
    if 'PASSED' not in smart_out:
        git_steps = [
            ('git-add', ['git', 'add', '.']),
            ('git-commit', ['git', 'commit', '-m', 'EMERGENCY EVACUATION']),
            ('git-push', ['git', 'push', 'origin', 'main']),
        ]
        git_results = []
        for step_name, cmd in git_steps:
            res = run_cmd_result(
                cmd,
                timeout=_resolve_timeout(tool_name, 'git', 60),
            )
            git_results.append(_cmd_details(step_name, res))
            if not res.get('ok'):
                return _tool_err(
                    tool_name,
                    'EVACUATION_GIT_FAILED',
                    'Drive failure detected, but emergency git evacuation failed.',
                    details={
                        'smartctl': _cmd_details('smartctl-health', smart_res),
                        'git': git_results,
                    },
                    retryable=True,
                )

        return _tool_err(
            tool_name,
            'DRIVE_FAILURE_DETECTED',
            'Critical drive failure detected; configuration evacuated to Git.',
            details={
                'smartctl': _cmd_details('smartctl-health', smart_res),
                'git': git_results,
            },
            retryable=False,
        )

    return _tool_ok(
        tool_name,
        'Hardware integrity stable.',
        data={'smartctl': _cmd_details('smartctl-health', smart_res)},
    )

# -- SECURITY --
@mcp.tool()
@_guarded_tool('sentinel_scan')
def sentinel_scan():
    """Scans for intrusion probes."""
    tool_name = 'sentinel_scan'
    ssh_res = run_cmd_result(
        ['journalctl', '-t', 'sshd', '--since', '1h ago', '-g', 'Failed|Invalid'],
        timeout=_resolve_timeout(tool_name, 'journal', 30),
        accepted_rcs=[0, 1],
    )
    fw_res = run_cmd_result(
        ['journalctl', '-k', '--since', '1h ago', '-g', 'FINAL_REJECT|FINAL_DROP'],
        timeout=_resolve_timeout(tool_name, 'journal', 30),
        accepted_rcs=[0, 1],
    )

    if not ssh_res.get('ok') and not fw_res.get('ok'):
        return _tool_err(
            tool_name,
            'SENTINEL_QUERY_FAILED',
            'Failed to collect both SSH and firewall journal probe data.',
            details={
                'ssh': _cmd_details('ssh-journal-probe', ssh_res),
                'firewall': _cmd_details('firewall-journal-probe', fw_res),
            },
            retryable=True,
        )

    return _tool_ok(
        tool_name,
        'Sentinel scan completed.',
        data={
            'ssh': _cmd_details('ssh-journal-probe', ssh_res),
            'firewall': _cmd_details('firewall-journal-probe', fw_res),
        },
    )


@mcp.tool()
@_guarded_tool('hardware_diagnostics')
def hardware_diagnostics():
    """Collect hardware diagnostics from canonical full diagnostics payload."""
    tool_name = 'hardware_diagnostics'
    diag = _load_full_diag_payload()
    if diag.get('_error'):
        return _tool_err(
            tool_name,
            'DIAGNOSTIC_SOURCE_FAILED',
            str(diag.get('_error')),
            retryable=True,
            error_category='dependency_unavailable',
            action_hint='Check full_diagnostics_json output and core probe command availability.',
        )

    issues = diag.get('hardware', []) or []
    priority = {'Critical': 1, 'High': 2, 'Warning': 3, 'Info': 4}
    issues_sorted = sorted(issues, key=lambda x: priority.get(str(x.get('severity', 'Info')), 10))
    report_text = _render_issue_report(
        section_name='hardware',
        issues=issues_sorted,
        excerpts=diag.get('excerpts', {}) or {},
        host=str(diag.get('host', platform.node())),
        timestamp=str(diag.get('timestamp', _utc_now_iso())),
        os_version=str(diag.get('os_version', 'unknown')),
        product=str(diag.get('product', 'unknown')),
        serial=str(diag.get('serial', 'unknown')),
    )
    return _tool_ok(
        tool_name,
        'Hardware diagnostics completed.',
        data={
            'summary': _issue_counts(issues_sorted),
            'report_text': report_text,
            'issues': issues_sorted,
            'source_timestamp': str(diag.get('timestamp', _utc_now_iso())),
        },
        code='DIAGNOSTICS_READY',
        action_hint='Prioritize critical then high remediations from issues[].remediations.',
    )


@mcp.tool()
@_guarded_tool('security_diagnostics')
def security_diagnostics():
    """Collect prioritized security findings from canonical full diagnostics payload."""
    tool_name = 'security_diagnostics'
    diag = _load_full_diag_payload()
    if diag.get('_error'):
        return _tool_err(
            tool_name,
            'DIAGNOSTIC_SOURCE_FAILED',
            str(diag.get('_error')),
            retryable=True,
            error_category='dependency_unavailable',
            action_hint='Check full_diagnostics_json output and security probe command availability.',
        )

    issues = diag.get('security', []) or []
    priority = {'Critical': 1, 'High': 2, 'Warning': 3, 'Info': 4}
    issues_sorted = sorted(issues, key=lambda x: priority.get(str(x.get('severity', 'Info')), 10))
    report_text = _render_issue_report(
        section_name='security',
        issues=issues_sorted,
        excerpts=diag.get('excerpts', {}) or {},
        host=str(diag.get('host', platform.node())),
        timestamp=str(diag.get('timestamp', _utc_now_iso())),
        os_version=str(diag.get('os_version', 'unknown')),
        product=str(diag.get('product', 'unknown')),
        serial=str(diag.get('serial', 'unknown')),
    )
    return _tool_ok(
        tool_name,
        'Security diagnostics completed.',
        data={
            'summary': _issue_counts(issues_sorted),
            'report_text': report_text,
            'issues': issues_sorted,
            'source_timestamp': str(diag.get('timestamp', _utc_now_iso())),
        },
        code='DIAGNOSTICS_READY',
        action_hint='Address critical access and policy findings before informational updates.',
    )


@mcp.tool()
@_guarded_tool('full_diagnostics_json')
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
    if not dmesg_out or dmesg_out.startswith('ERR:'):
        dmesg_out = run_cmd(['journalctl', '-k', '-p', '3', '--no-pager']) or ''
    full_dmesg = run_cmd(['dmesg', '-T']) or ''
    if not full_dmesg or full_dmesg.startswith('ERR:'):
        full_dmesg = run_cmd(['journalctl', '-k', '--no-pager']) or ''
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
    ssh_journal = run_cmd(['journalctl', '-u', 'sshd', '--since', '24 hours ago', '--no-pager']) or ''
    sudo_journal = run_cmd(['journalctl', '-t', 'sudo', '--since', '24 hours ago', '--no-pager']) or ''
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
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
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


@mcp.tool()
@_guarded_tool('mcp_server_capabilities')
def mcp_server_capabilities():
    """Return advertised server capabilities and operational guardrails."""
    capabilities = {
        'server': 'P-Series-Architect',
        'timestamp': _utc_now_iso(),
        'config_source': os.path.join(BASE_DIR, 'mcp-config.json'),
        'features': {
            'health_and_readiness': True,
            'diagnostics': True,
            'audit_logging': True,
            'command_timeout': True,
            'dry_run_support': True,
            'structured_tool_responses': True,
            'structured_error_codes': True,
            'structured_error_category': True,
            'structured_action_hint': True,
        },
        'guards': {
            'default_command_timeout_sec': int(SERVER_CONFIG.get('defaultCommandTimeoutSec', 60) or 60),
            'tool_command_timeout_sec': SERVER_CONFIG.get('toolCommandTimeoutSec', {}),
            'audit_log_path': SERVER_CONFIG.get('auditLogPath') or AUDIT_LOG,
            'secret_redaction': 'basic' if SERVER_CONFIG.get('redactSecretsInAudit', True) else 'disabled',
            'audit_rotation': SERVER_CONFIG.get('audit', {}),
            'limits': SERVER_CONFIG.get('limits', {}),
            'security': {
                'requireApprovalForHighRisk': bool((SERVER_CONFIG.get('security', {}) or {}).get('requireApprovalForHighRisk', True)),
                'approvalEnvVar': str((SERVER_CONFIG.get('security', {}) or {}).get('approvalEnvVar', 'MCP_APPROVE_HIGH_RISK')),
                'highRiskTools': (SERVER_CONFIG.get('security', {}) or {}).get('highRiskTools', []),
                'disabledTools': (SERVER_CONFIG.get('security', {}) or {}).get('disabledTools', []),
            },
        },
        'operator_tools': [
            'mcp_server_health',
            'mcp_server_readiness',
            'mcp_doctor_report',
            'mcp_server_capabilities',
            'mcp_audit_tail',
            'hardware_diagnostics',
            'security_diagnostics',
            'full_diagnostics_json',
        ],
    }
    return _tool_ok(
        'mcp_server_capabilities',
        'Server capabilities collected.',
        data=capabilities,
        code='CAPABILITIES_READY',
    )


@mcp.tool()
@_guarded_tool('mcp_server_health')
def mcp_server_health():
    """Return fast health snapshot for MCP and key dependencies."""
    snap = _mcp_health_snapshot()
    _audit_event('health-check', {'status': snap.get('status')})
    if snap.get('status') == 'healthy':
        return _tool_ok(
            'mcp_server_health',
            'Health snapshot is healthy.',
            data=snap,
            code='HEALTHY',
        )
    return _tool_err(
        'mcp_server_health',
        'HEALTH_DEGRADED',
        'Health snapshot is degraded.',
        details=snap,
        retryable=True,
        error_category='dependency_unavailable',
        action_hint='Run mcp_server_readiness and mcp_doctor_report for actionable blockers.',
    )


@mcp.tool()
@_guarded_tool('mcp_server_readiness')
def mcp_server_readiness():
    """Return readiness decision with actionable blockers."""
    snap = _mcp_health_snapshot()
    blockers = []
    checks = snap.get('checks', {})
    health_cfg = SERVER_CONFIG.get('health', {}) or {}
    bridge_port = int(health_cfg.get('bridgePort', 1776) or 1776)
    ollama_port = int(health_cfg.get('ollamaPort', 11434) or 11434)
    bridge_key = f'bridge_port_{bridge_port}'
    ollama_key = f'ollama_port_{ollama_port}'
    bridge_service = str(health_cfg.get('bridgeServiceName', 'mcp-bridge.service'))
    ollama_service = str(health_cfg.get('ollamaServiceName', 'ollama.service'))
    min_root_free_gb = int(health_cfg.get('minRootFreeGB', 2) or 2)
    min_mem_mb = int(health_cfg.get('minAvailableMemoryMB', 512) or 512)
    if not checks.get(bridge_key, {}).get('ok', False):
        blockers.append(f'Bridge port {bridge_port} is unreachable; start or fix {bridge_service}.')
    if not checks.get(ollama_key, {}).get('ok', False):
        blockers.append(f'Ollama port {ollama_port} is unreachable; start or fix {ollama_service}.')
    if not checks.get('disk_root', {}).get('ok', False):
        blockers.append(f'Low root disk free space; keep at least {min_root_free_gb} GB free for stable operation.')
    if not checks.get('memory', {}).get('ok', False):
        blockers.append(f'Low available memory; keep at least {min_mem_mb} MB available for MCP tools.')

    readiness = {
        'timestamp': _utc_now_iso(),
        'ready': len(blockers) == 0,
        'status': 'ready' if len(blockers) == 0 else 'not-ready',
        'blockers': blockers,
        'health_status': snap.get('status'),
    }
    _audit_event('readiness-check', {'ready': readiness['ready'], 'blocker_count': len(blockers)})
    if readiness['ready']:
        return _tool_ok(
            'mcp_server_readiness',
            'Server is ready.',
            data=readiness,
            code='READY',
        )
    return _tool_err(
        'mcp_server_readiness',
        'NOT_READY',
        'Server is not ready.',
        details=readiness,
        retryable=True,
        error_category='dependency_unavailable',
        action_hint='Resolve blockers in details.blockers and re-run readiness.',
    )


@mcp.tool()
@_guarded_tool('mcp_doctor_report')
def mcp_doctor_report():
    """Run consolidated MCP doctor report: health, readiness, diagnostics summary, and actions."""
    health = _mcp_health_snapshot()
    readiness_env = json.loads(mcp_server_readiness())
    readiness = readiness_env.get('data') if isinstance(readiness_env, dict) else None
    if not isinstance(readiness, dict):
        readiness = {
            'ready': False,
            'status': 'not-ready',
            'blockers': ['Unable to parse readiness payload.'],
            'health_status': health.get('status'),
            'timestamp': _utc_now_iso(),
        }

    diag = {'hardware': [], 'security': []}
    try:
        diag = json.loads(full_diagnostics_json())
    except Exception:
        pass

    hw = diag.get('hardware', []) or []
    sec = diag.get('security', []) or []
    crit_count = sum(1 for i in (hw + sec) if str(i.get('severity', '')).lower() == 'critical')
    high_count = sum(1 for i in (hw + sec) if str(i.get('severity', '')).lower() == 'high')

    next_actions = []
    if not readiness.get('ready', False):
        next_actions.extend(readiness.get('blockers', []))
    for issue in (hw + sec):
        rem = issue.get('remediations') or []
        if rem:
            next_actions.append(rem[0])
        if len(next_actions) >= 8:
            break

    report = {
        'timestamp': _utc_now_iso(),
        'host': platform.node(),
        'health': health,
        'readiness': readiness,
        'diagnostics_summary': {
            'hardware_issue_count': len(hw),
            'security_issue_count': len(sec),
            'critical_count': crit_count,
            'high_count': high_count,
        },
        'top_next_actions': next_actions,
    }
    _audit_event('doctor-report', {
        'ready': readiness.get('ready', False),
        'critical_count': crit_count,
        'high_count': high_count,
    })
    if crit_count > 0 or not readiness.get('ready', False):
        return _tool_err(
            'mcp_doctor_report',
            'DOCTOR_ATTENTION_REQUIRED',
            'Doctor report found critical issues or readiness blockers.',
            details=report,
            retryable=True,
            error_category='health',
            action_hint='Start with top_next_actions[0] and re-run doctor report after remediation.',
        )
    return _tool_ok(
        'mcp_doctor_report',
        'Doctor report completed with no critical blockers.',
        data=report,
        code='DOCTOR_OK',
    )


@mcp.tool()
@_guarded_tool('mcp_audit_tail')
def mcp_audit_tail(lines: int = 100):
    """Return recent MCP server audit records for troubleshooting and traceability."""
    n = int(lines) if int(lines) > 0 else 100
    n = min(n, 500)
    audit_path = SERVER_CONFIG.get('auditLogPath') or AUDIT_LOG
    records = _read_last_lines(audit_path, n)
    return '\n'.join(records) if records else 'No audit events yet.'

if __name__ == "__main__":
    mcp.run()
