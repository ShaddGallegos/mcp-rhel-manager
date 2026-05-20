#!/usr/bin/env bash
set -euo pipefail

usage(){
	cat <<EOF
Usage: $0 [--prometheus] [--grafana] [--monitoring] [--target HOST] [--yes]

Installs MCP model management and optionally monitoring (Prometheus/Grafana).
If --target is provided, the monitoring playbook is applied to that host via Ansible.
EOF
}

PROMETHEUS=0
GRAFANA=0
TARGET=""
NONINTERACTIVE=0
DOCKER=0

while [[ $# -gt 0 ]]; do
	case "$1" in
		--prometheus) PROMETHEUS=1; shift;;
		--grafana) GRAFANA=1; shift;;
		--monitoring|--monitor) PROMETHEUS=1; GRAFANA=1; shift;;
		--docker) DOCKER=1; shift;;
		--target) TARGET="$2"; shift 2;;
		--yes|-y) NONINTERACTIVE=1; shift;;
		-h|--help) usage; exit 0;;
		*) echo "Unknown arg: $1"; usage; exit 2;;
	esac
done

echo "Installing MCP model management tools (requires sudo)..."
sudo mkdir -p /etc/tmpfiles.d /etc/systemd/system /var/lib/mcp-llms || true

# ensure system user exists
if ! id -u mcp-ai >/dev/null 2>&1; then
	echo "Creating system user mcp-ai"
	sudo useradd --system --create-home --shell /sbin/nologin mcp-ai || true
fi

sudo mkdir -p /home/mcp-ai/.mcp-ai || true
sudo chown -R mcp-ai:mcp-ai /var/lib/mcp-llms /home/mcp-ai/.mcp-ai || true
sudo cp -v packaging/tmpfiles.d/mcp-llms.conf /etc/tmpfiles.d/ || true
sudo cp -v packaging/systemd/mcp-model-cleanup.service packaging/systemd/mcp-model-cleanup.timer /etc/systemd/system/ || true
sudo cp -v scripts/manage_models.py /usr/local/bin/manage_models.py || true
sudo chmod +x /usr/local/bin/manage_models.py || true

echo "Reloading systemd and creating tmpfiles..."
sudo systemctl daemon-reload || true
sudo systemd-tmpfiles --create /etc/tmpfiles.d/mcp-llms.conf || true

echo "Enabling and starting model-cleanup timer..."
sudo systemctl enable --now mcp-model-cleanup.timer || true

echo "Done. To inspect status:"
echo "  sudo systemctl status mcp-model-cleanup.timer mcp-model-cleanup.service"
echo "Index file: /var/lib/mcp-llms/models.json"

# Offer monitoring installation (if requested via flags or interactively)
if [[ ${PROMETHEUS} -eq 0 && ${GRAFANA} -eq 0 && ${NONINTERACTIVE} -eq 0 ]]; then
	read -p "Install Prometheus and Grafana locally? (y/N): " yn || true
	case "$yn" in
		[Yy]*) PROMETHEUS=1; GRAFANA=1;;
		*) ;;
	esac
fi

if [[ ${PROMETHEUS} -eq 1 || ${GRAFANA} -eq 1 ]]; then
	echo "Preparing monitoring installation (prometheus=${PROMETHEUS} grafana=${GRAFANA})"
	if [[ -n "$TARGET" ]]; then
		if ! command -v ansible-playbook >/dev/null 2>&1; then
			echo "ansible-playbook not found; please install Ansible to deploy to remote hosts." >&2
			exit 1
		fi
		echo "Running Ansible playbook against target: $TARGET"
			ansible-playbook -i "${TARGET}," ansible/playbooks/install_monitoring.yml -e "install_prometheus=${PROMETHEUS} install_grafana=${GRAFANA} install_docker=${DOCKER}" || true
	else
		# local install: prefer docker compose if available
		if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
			echo "Using docker compose to start monitoring stack (monitoring/docker-compose.yml)"
			(cd monitoring && docker compose up -d) || true
		elif command -v ansible-playbook >/dev/null 2>&1; then
			echo "Docker compose not found; using Ansible local connection to deploy monitoring"
			ansible-playbook -c local ansible/playbooks/install_monitoring.yml -e "install_prometheus=${PROMETHEUS} install_grafana=${GRAFANA} install_docker=${DOCKER}" || true
		else
			echo "Unable to start monitoring automatically: install Docker (with compose) or Ansible, then re-run this script or start monitoring via: cd monitoring && docker compose up -d" >&2
		fi
	fi
fi

exit 0
