HAL Privileged Actions
======================

Overview
--------

HAL can propose and execute privileged (root) commands using the `--priv` flag. The flow is intentionally conservative:

- HAL asks the LLM to propose a single shell command and a short snippet describing it.
- HAL shows the snippet and proposed command to the user.
- The user must explicitly confirm and will be prompted for their sudo password.
- All privileged interactions are recorded to `~/.mcp-ai/reports/privileged_actions.log` and to the HAL interaction journal.

Allowlist
---------

To reduce risk, you may configure a privileged allowlist at `~/.mcp-ai/privileged_allowlist.json`. Copy and edit the example provided in `mcp-ai/privileged_allowlist.example.json`.

Each entry is an object with `name`, `pattern`, and `description`. `pattern` is a regular expression matched against the full command string. If a proposed command matches any pattern, HAL treats it as allowlisted and requires regular confirmation. If not matched, HAL requires a stronger confirmation token: you must type `ALLOW` (uppercase) to proceed.

Examples
--------

Run HAL to propose and restart a service:

```
python3 scripts/hal.py --priv "Restart the mcp bridge service"
```

Security notes
--------------

- HAL does not store sudo passwords. It uses the system `sudo` prompt and relies on the OS credential cache.
- For fully automated headless privilege, consider tightly-scoped `sudoers` entries (NOT recommended broadly).
- Use allowlist entries with minimal patterns to avoid accidental dangerous command execution.
