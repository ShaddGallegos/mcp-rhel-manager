# HAL Functionality Reference

> **aider-chat (optional):** Install separately — `pip3 install --upgrade aider-chat`. aider-chat hard-pins `filelock==3.20.3`, which conflicts with `virtualenv` (requires `filelock>=3.24.2`) and `tox`. After a system-wide install, restore the required version: `pip3 install --upgrade "filelock>=3.24.2"`. The project venv is isolated and unaffected.

**HAL** (Holistic Architecture Liaison) is an intelligent AI-powered CLI and MCP server for system management, diagnostics, and remediation on RHEL systems. This document outlines all available functionality.

---

## Architecture Overview

HAL consists of two layers:

1. **CLI Layer** (`scripts/hal.py`) — Natural language interface with intent routing and fallback to LLM bridge
2. **MCP Backend** (`scripts/server.py`) — FastMCP server providing specialized tools for system operations

---

## HAL CLI Intent Routes

The CLI automatically routes user queries to the most appropriate handler. Below are all recognized intents:

### System & Infrastructure Management

| Intent                | Description                                      | Example Commands                                        |
| --------------------- | ------------------------------------------------ | ------------------------------------------------------- |
| `wellbeing-check`     | System health check + diagnostics offer          | "how are you", "are you ok", "system status"            |
| `operational-howto`   | Operational how-to runbooks (RHEL/Linux/Ansible) | "how do I configure ntp", "how to add a user in RHEL"   |
| `server-update-strat` | Server patching and update strategy runbook      | "how do I patch RHEL servers", "server update strategy" |

### Automation Platform

*Vendor-specific automation platform guidance removed from this repository.*



### Code Generation & Infrastructure Automation

| Intent           | Description                                               | Example Commands                                                                                                   |
| ---------------- | --------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| `jinja2-codegen` | Generate Jinja2 templates                                 | "write a Jinja2 template for hosts", "create a J2 config template"                                                 |
| `python-codegen` | Generate Python scripts or modules                        | "write a Python script to parse JSON", "create a Python module"                                                    |
| `ee-de-builder`  | Build execution/decision environments with vendor support | "create an EE for VMware automation", "generate EE YAML with AWS modules" |

### Training Data Management

| Intent                 | Description                                 | Example Commands                                                                                      |
| ---------------------- | ------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| `training-import-url`  | Execute URL import from natural language    | "import training data from URL <https://example.com>", "ingest URL <https://docs.redhat.com> depth 5" |
| `training-import-txt`  | Execute TXT import from natural language    | "import training data from TXT /tmp/a.txt", "ingest text file ./notes.txt"                            |
| `training-import-help` | How to import training data from URL/TXT    | "how do I import training data from URL", "how to import TXT into HAL training"                       |
| `training-bundle`      | Export training data as portable ZIP bundle | "zip up my training data", "bundle HAL knowledge for USB"                                             |

### Business Intelligence

| Intent               | Description                                       | Example Commands                                                           |
| -------------------- | ------------------------------------------------- | -------------------------------------------------------------------------- |
| `dependency-advisor` | Advise on Ansible/Python/Git install requirements | "what do I need to install for Ansible", "what Python packages for Jinja2" |
| `strategy`           | Consulting strategy generation for accounts       | "strategy for ACME Corp", "account plan for Centene"                       |
| `stakeholder`        | Account stakeholder/contact lookup                | "who are the contacts at ACME", "stakeholders for Centene"                 |
| `subscription-csv`   | Customer subscription CSV export                  | "subscription report CSV", "export subscription data"                      |
| `stock-price`        | Account stock price lookup from intel             | "what is ACME stock price", "stock for Centene"                            |

### Conversational

| Intent     | Description                | Example Commands         |
| ---------- | -------------------------- | ------------------------ |
| `greeting` | Friendly greeting response | "hi", "hello", "hey HAL" |

### Fallback

| Intent      | Description                                               |
| ----------- | --------------------------------------------------------- |
| (unmatched) | LLM bridge with automatic model selection by task profile |

---

## MCP Server Tools (Backend)

### Performance & Evolution

#### `optimize_ai_performance()`

Sets CPU to performance governor and enables NVIDIA persistence mode for AI workloads.

**Returns:**

- `cpupower` command results (frequency scaling)
- NVIDIA persistence status (if GPU available)
- Success/error details

**Error Codes:** `CPU_GOVERNOR_SET_FAILED`, `NVIDIA_PERSISTENCE_ENABLE_FAILED`

---

#### `system_evolution()`

Updates Kernel and NVIDIA drivers; ensures akmods are built for current kernel.

**Returns:**

- Kernel upgrade results
- NVIDIA driver compilation status
- akmods build report

**Error Codes:** `KERNEL_UPGRADE_FAILED`, `NVIDIA_COMPILATION_FAILED`, `AKMODS_BUILD_FAILED`

---

### Infrastructure as Code

#### `generate_ansible_manifest()`

Dumps current system state (installed packages) into an Ansible role for future propagation to other nodes.

**Returns:**

- Generated manifest path: `ansible/roles/p_series_node/vars/main.yml`
- Package count
- Query results from `dnf repoquery`

**Error Codes:** `PACKAGE_QUERY_FAILED`, `MANIFEST_WRITE_FAILED`

---

#### `build_execution_environment()`

Generates and optionally builds execution/decision environments (EE/DE) with vendor-specific collection support using ansible-builder.

**Parameters:**

- `name`: Environment name (e.g., "vmware-ee", "vendor-de")
- `ee_type`: Type of environment ("execution" or "decision")
- `base_image`: Optional custom base image (defaults to minimal base images)
- `vendors`: List of vendor collections to include (see Vendor Support below)
- `python_packages`: List of Python packages (e.g., ["requests", "pyyaml"])
- `system_packages`: List of system packages (e.g., ["git", "rsync"])
- `rhel_version`: Target RHEL version ("8" or "9", default "9")
- `build_now`: Whether to execute `ansible-builder build` immediately (default: false)
- `output_dir`: Output directory for generated EE/DE structure

**Vendor Support:**

Automatically configures collections for specified vendors:

| Vendor       | Collections                                               | Use Cases                   |
| ------------ | --------------------------------------------------------- | --------------------------- |
| `ansible`    | ansible.utils, ansible.posix, ansible.netcommon           | Core POSIX/networking tasks |
| `community`  | community.general, community.vmware, community.postgresql | General Linux + specialized |
| `redhat`     | redhat.rhel_system_roles, redhat.redhat_csp_download       | Red Hat product system roles |
| `vmware`     | community.vmware, vmware.vmware_rest                      | VMware vSphere automation   |
| `aws`        | amazon.aws                                                | AWS cloud automation        |
| `azure`      | azure.azcollection                                        | Azure cloud automation      |

| `nutanix`    | community.general (Nutanix AHV)                           | Nutanix infrastructure      |
| `postgresql` | community.postgresql                                      | Database automation         |
| `kubernetes` | kubernetes.core, community.general                        | K8s cluster management      |

**Returns:**

```json
{
  "ok": true,
  "tool": "build_execution_environment",
  "code": "EE_GENERATED" | "EE_BUILD_COMPLETE",
  "message": "Execution environment generated/built successfully",
  "data": {
    "environment_path": "/path/to/ee-name",
    "execution_environment_yml": "path/to/execution-environment.yml",
    "requirements_yml": "path/to/requirements.yml",
    "requirements_txt": "path/to/requirements.txt",
    "bindep_txt": "path/to/bindep.txt",
    "collections_count": 15,
    "image_built": false | true,
    "image_uri": "optional-built-image-uri",
    "build_time_seconds": 120,
    "container_size_mb": 450
  }
}
```

**Error Codes:**

- `EE_GENERATION_FAILED` — YAML structure creation failed
- `EE_BUILD_FAILED` — ansible-builder execution failed
- `INVALID_VENDOR` — Unknown vendor requested
- `COLLECTION_RESOLUTION_FAILED` — Collection version conflicts

**Action Hints:**

- Use `output_dir` parameter to organize multiple EE builds
- Set `build_now=true` only after validating YAML structure
- Vendor combinations can be specified: `vendors=["vmware", "ansible", "community"]`
- Built images can be pushed to container registry via `podman push`

**Integration Notes:**

- Wraps the [Base_EE-DE_Builder](https://github.com/reference-user/Base_EE-DE_Builder) reference implementation
- Uses `ansible-builder` under the hood for containerized builds
- Generates version 3 `execution-environment.yml` format (EE v3 compatible)
- Supports both EE (runtime) and DE (development) environment types

---

### Survival & Exfiltration

#### `predict_failure_and_evacuate()`

Checks NVMe health via `smartctl`; if drive is failing, automatically pushes config to git repository for recovery.

**Returns:**

- NVMe health status
- Git push status (if needed)
- Configuration backup location

**Error Codes:** `SMARTCTL_FAILED`, `GIT_PUSH_FAILED`, `DEVICE_FAILING`

**Risk Level:** HIGH — Requires approval via `MCP_APPROVE_HIGH_RISK` env var

---

### Security

#### `sentinel_scan()`

Scans system journals for intrusion probes — SSH failed login attempts and firewall rejection events.

**Returns:**

- SSH probe attempts from `journalctl -t sshd`
- Firewall rejection events from `journalctl -k`
- Formatted probe report

**Error Codes:** `SENTINEL_QUERY_FAILED`

---

### Diagnostics

#### `hardware_diagnostics()`

Collects hardware diagnostics from system (sensors, SMART, PCIe, journal errors).

**Returns:**

- Hardware issues (sorted by severity: Critical → Info)
- Summary counts by severity
- Rendered issue report with remediations
- Source timestamp

**Error Codes:** `DIAGNOSTIC_SOURCE_FAILED`

**Action Hints:** Prioritize critical then high remediations from `issues[].remediations`

---

#### `security_diagnostics()`

Collects prioritized security findings (SELinux denials, policy violations, access issues).

**Returns:**

- Security issues (sorted by severity)
- Summary counts by severity
- Rendered issue report with remediations
- Source timestamp

**Error Codes:** `DIAGNOSTIC_SOURCE_FAILED`

**Action Hints:** Address critical access and policy findings before informational updates

---

#### `full_diagnostics_json()`

Returns comprehensive structured JSON with hardware and security issues, including severity, driver info, and remediation steps.

**Returns (JSON structure):**

```json
{
  "hardware": [
    {
      "title": "Issue title",
      "severity": "Critical|High|Warning|Info",
      "driver": "Component name",
      "error_text": "Raw excerpt",
      "remediations": ["Immediate action", "Follow-up action"]
    }
  ],
  "security": [
    {
      "title": "Issue title",
      "severity": "Critical|High|Warning|Info",
      "error_text": "Raw excerpt",
      "remediations": ["Action"]
    }
  ],
  "excerpts": { "key": "raw output" },
  "host": "hostname",
  "timestamp": "ISO8601",
  "os_version": "version",
  "product": "system model",
  "serial": "serial number"
}
```

**Error Codes:** `DIAGNOSTIC_SOURCE_FAILED`

---

### Operational Status

#### `mcp_server_capabilities()`

Returns advertised server capabilities, feature flags, security guards, and operational constraints.

**Returns:**

- Server name: `P-Series-Architect`
- Feature flags (health, diagnostics, audit, timeout, dry-run support, etc.)
- Guard rails (timeouts, audit log path, secret redaction, limits)
- List of operator tools

**Error Code:** `CAPABILITIES_READY`

---

#### `mcp_server_health()`

Fast health snapshot for MCP server and key dependencies (bridge, OLLAMA, disk, memory).

**Returns:**

- Overall status: `healthy` | `degraded`
- Per-component checks:
  - Bridge connectivity (port 1776)
  - OLLAMA connectivity (port 11434)
  - Available memory
  - Root disk free space
  - systemd service status

**Error Code:** `HEALTH_DEGRADED` (retryable)

**Action Hint:** Run `mcp_server_readiness` and `mcp_doctor_report` for actionable blockers

---

#### `mcp_server_readiness()`

Returns readiness decision with actionable blockers for deployment.

**Returns:**

- `ready`: boolean
- `status`: `ready` | `not-ready`
- `blockers`: list of actions needed (empty if ready)
- `health_status`: from health check
- `timestamp`: ISO8601

**Error Code:** `NOT_READY` (retryable)

**Action Hint:** Resolve blockers and re-run readiness check

---

#### `mcp_doctor_report()`

Consolidated MCP doctor report: combines health, readiness, diagnostics summary, and prioritized next actions.

**Returns:**

- Complete health snapshot
- Readiness status with blockers
- Diagnostics summary:
  - Hardware issue count
  - Security issue count
  - Critical + High counts
- Top 8 recommended next actions

**Error Codes:** `DOCTOR_ATTENTION_REQUIRED` (if critical issues found)

**Error Hints:** Start with `top_next_actions[0]` and re-run after remediation

---

#### `mcp_audit_tail(lines: int = 100)`

Returns recent MCP server audit records for troubleshooting and traceability.

**Parameters:**

- `lines`: Number of records to retrieve (1-500, default 100)

**Returns:**

- JSONL records (one per line) with:
  - timestamp
  - tool name
  - status (ok/error)
  - command details
  - redacted secrets (if configured)
  - error codes and messages

**Error Code:** `AUDIT_TAIL_FAILED`

---

## Configuration

All MCP server behavior is controlled by `mcp-config.json`:

```json
{
  "mcpServers": {
    "architect": {
      "settings": {
        "configVersion": 2,
        "environment": "prod",
        "defaultCommandTimeoutSec": 60,
        "security": {
          "requireApprovalForHighRisk": true,
          "approvalEnvVar": "MCP_APPROVE_HIGH_RISK",
          "highRiskTools": ["system_evolution", "predict_failure_and_evacuate"],
          "disabledTools": []
        },
        "health": {
          "minAvailableMemoryMB": 512,
          "minRootFreeGB": 2,
          "bridgeHost": "127.0.0.1",
          "bridgePort": 1776,
          "ollamaHost": "127.0.0.1",
          "ollamaPort": 11434
        },
        "audit": {
          "maxAuditFileMB": 25,
          "maxAuditFiles": 8
        },
        "limits": {
          "maxConcurrentCommands": 4,
          "rateLimitPerMinute": 120
        }
      }
    }
  }
}
```

---

## CLI Command Structure

```bash
# General query (routed by intent)
hal 'your query or request'

# Help system
hal help                          # List all help topics
hal help overview                 # Show overview
hal help quick-start              # Quick start guide
hal help <topic>                  # Show specific topic

# Administrative
hal --list-intents               # Show all available intents
hal --explain                    # Show last routing decision
hal --version                    # Show HAL version
hal --offline                    # Run in offline mode (no bridge)

# Training data
hal --training-report            # Show training data quality report
hal --training-import <url>      # Import from URL
hal --training-export            # Bundle training data for export
```

---

## Profiles & Model Selection

HAL automatically selects the best model based on query profile:

- **Ansible**: Code generation, architecture planning
- **Linux**: System administration, troubleshooting
- **RHEL**: Enterprise RHEL-specific guidance
- **Business**: Strategy, stakeholder intelligence
- **Default**: General queries

---

## Error Handling

All tools return structured error responses:

```json
{
  "ok": false,
  "tool": "tool_name",
  "code": "ERROR_CODE",
  "category": "dependency_unavailable|health|access|validation",
  "message": "Human-readable error",
  "details": { "context": "..." },
  "retryable": true,
  "action_hint": "What to do next"
}
```

---

## Audit & Traceability

All MCP tool executions are logged to:

- `~/.mcp-ai/reports/mcp-server-audit.jsonl` — Detailed audit records
- `~/.mcp-ai/reports/hal-last-route.json` — Last routing decision (for `--explain`)
- `~/.mcp-ai/training/hal-*.jsonl` — User interactions and LLM responses

---

## Integration Points

HAL bridges to external systems:

1. **MCP Bridge** (port 1776) — Connects to OLLAMA models
2. **OLLAMA** (port 11434) — Local LLM model serving
3. **Git** — Configuration backup and exfiltration
4. **Red Hat Knowledge Base** — Training data and offline fallback
5. **systemd** — Service management and monitoring

---

## Quick Usage Examples

### Check System Health

```bash
hal "what is the health of my system"
hal "are you ok"
```

### Generate Infrastructure Code

```bash
hal "write a playbook to install nginx"
hal "create an Ansible role for user management"
```

### Get Patching Strategy

```bash
hal "how do I patch servers"
hal "patching strategy"
```

### Import Training Data

```bash
hal "import training data from https://docs.redhat.com/... depth 5"
hal "ingest text file ./my-notes.txt"
```

### Get Business Intelligence

```bash
hal "strategy for ACME Corp"
hal "who are the stakeholders at Centene"
```

### Operational Guidance

```bash
hal "how do I configure NTP"
hal "how to set up SSO"
```

### Build Execution/Decision Environments

```bash
# Generate EE for VMware automation (no build yet)
hal "create an execution environment for VMware with ansible.utils and vmware collections"

# Build a Decision Environment with vendor collections
hal "build a decision environment with vendor collections and include git and rsync"

# Create AWS-ready EE with custom Python packages
hal "generate execution environment for AWS with amazon.aws collection, add boto3 and requests packages"

# Create multi-vendor environment with all collections for cloud platforms
hal "create ee for aws, azure, and vmware automation with requests package"

# Build and deploy immediately
hal "build and deploy execution environment named production-ee with ansible, community, and vmware vendors"
```

### Vendor-Specific EE Examples

#### VMware Environment

```bash
hal "execution environment for vmware with community.vmware and vmware.vmware_rest collections"
```

#### Multi-Cloud (AWS + Azure)

```bash
hal "create ee for aws and azure cloud automation"
```

#### Vendor-specific Infrastructure

```bash
hal "build decision environment with vendor collections and rhel support"
```

#### Database + Kubernetes

```bash
hal "generate execution environment for postgresql and kubernetes management"
```

---

## Execution Environment Management

### Generated Artifacts

When using `build_execution_environment()` or the `ee-de-builder` intent, HAL creates:

```text
ee-<name>/
├── execution-environment.yml         # Version 3 EE manifest
├── requirements.yml                  # Ansible collection specs
├── requirements.txt                  # Python package versions
├── bindep.txt                        # System package specs
└── context/                          # Container build context
    ├── Containerfile                 # Generated Containerfile
    ├── _build/                       # Build scripts and metadata
    │   ├── requirements.yml
    │   ├── requirements.txt
    │   ├── bindep.txt
    │   └── scripts/                  # ansible-builder scripts
    └── (built container image)
```

### Sample execution-environment.yml

```yaml
---
version: 3

images:
  base_image:
    name: registry.example/ee-minimal:latest
    options:
      pull_policy: missing
      tls_verify: false

dependencies:
  ansible_core:
    package_pip: ansible-core>=2.15.0
  python: requirements.txt
  system: bindep.txt
  galaxy: requirements.yml

options:
  package_manager_path: /usr/bin/microdnf

additional_build_steps:
  prepend_final: |
    RUN microdnf upgrade -y && microdnf clean all

  append_final: |
    USER root
    RUN microdnf clean all && rm -rf /var/cache/{dnf,yum}
```

### Troubleshooting EE Builds

**Issue:** `COLLECTION_RESOLUTION_FAILED` — Collection version conflicts

```bash
# Solution: Specify compatible versions explicitly
hal "create ee named dev-ee with ansible collections, specific versions: ansible.utils>=2.10.0 ansible.posix>=1.5.0"
```

**Issue:** `EE_BUILD_FAILED` — ansible-builder unable to build

```bash
# Solution: Check system dependencies and disk space
hal "before building, check system health and diagnostic status"
# Then resolve any blockers before retrying
```

**Issue:** Image push fails

```bash
# Solution: Authenticate to registry first
podman login registry.redhat.io  # or your registry
# Then reference in EE: base_image: registry.redhat.io/ubi9/ubi:latest
```

---

## Support & Debugging

To troubleshoot:

1. **Check readiness:** HAL auto-checks readiness on startup
2. **View last route:** `hal --explain`
3. **Check audit log:** `hal --audit-tail 50`
4. **Review training:** `hal --training-report`
5. **Run doctor:** Query `mcp_doctor_report` via MCP client

---

## EE/DE Builder Integration

The `build_execution_environment()` tool integrates with the Base_EE-DE_Builder reference implementation for robust, production-grade EE/DE creation. Key features:

### Automated Vendor Collection Management

- Resolves vendor-specific collection requirements automatically
- Handles collection version conflicts and dependency resolution
- Supports mixing vendors in single environment

### Version 3 YAML Generation

- Creates `execution-environment.yml` v3 compatible manifests
- Auto-configures `microdnf` package manager for minimal images
- Generates `ansible/requirements-full.yml`, `requirements.txt`, and `bindep.txt`

### Flexible Deployment

- Generate-only mode: Create YAML structure for review/customization
- Build mode: Execute `ansible-builder build` immediately
- Push-ready: Generated images can be pushed to Red Hat registries

### RHEL Version Support

- RHEL 8: Minimal and supported base images
- RHEL 9: Minimal, supported, and vendor-specific variants

---

**Last Updated:** April 30, 2026
**Latest Addition:** Execution Environment / Decision Environment Builder (v3 support with vendor collections)
