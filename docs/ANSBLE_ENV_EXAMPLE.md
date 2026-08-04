ANSBLE environment example
==========================

This file is an example of the YAML file you should keep out of git and place
at `~/.ansble/conf/env.yml` (per project convention). Do NOT commit real
secrets; keep them only in your home folder.

Example (replace the placeholder values):

```yaml
# Path to a file containing the ansible-vault password (optional)
ANSIBLE_VAULT_PASSWORD_FILE: "~/.ansible/conf/.vaultpass.txt"

# LLM bridge / Ollama endpoint (optional)
OLLAMA_URL: "http://localhost:1776/api/chat"

# API keys used by optional integrations
BRAVE_API_KEY: "your-brave-api-key-here"
TAVILY_API_KEY: "your-tavily-api-key-here"

# Training encryption key (used by training_crypto.py if you prefer env var)
HAL_TRAINING_KEY: "a-strong-password-for-training-encryption"

# Container registry credentials for optional pushes (if needed)
REDHAT_REGISTRY_USER: "your-redhat-username"
REDHAT_REGISTRY_PASSWORD: "your-redhat-password"

# Any other secrets or tokens used by local integrations
EXTRA_SECRET: "replace-me"
```

How to use:

- Create the directory and file with secure permissions:

```bash
mkdir -p ~/.ansble/conf
cp docs/ANSBLE_ENV_EXAMPLE.md ~/.ansble/conf/env.yml    # edit the values
chmod 600 ~/.ansble/conf/env.yml
```

- When scripts or services need these variables they should read them from
  the environment (export) or your runtime should load the YAML and export
  values into the environment. Do NOT commit `~/.ansble/conf/env.yml` to git.
