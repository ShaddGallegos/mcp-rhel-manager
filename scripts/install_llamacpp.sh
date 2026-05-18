#!/usr/bin/env bash
set -euo pipefail

# install_llamacpp.sh
# Installs/builds llama.cpp into /opt/llama.cpp and places a small wrapper
# binary at /usr/local/bin/llama.cpp-run (no model files are downloaded).
# On RHEL/Fedora systems it prefers microdnf/dnf.

MODEL_DIR=${1:-/var/lib/mcp-llms}
LLAMA_DIR=${2:-/opt/llama.cpp}

echo "Installing build deps and building llama.cpp (no models will be downloaded)"

if command -v microdnf >/dev/null 2>&1; then
  PKG="microdnf -y"
elif command -v dnf >/dev/null 2>&1; then
  PKG="dnf -y"
else
  echo "No supported package manager found (microdnf/dnf). Install dependencies manually." >&2
  exit 1
fi

$PKG install git cmake make gcc-c++ wget xz tar || true

mkdir -p "$LLAMA_DIR"
cd /tmp
if [ -d "$LLAMA_DIR/.git" ]; then
  echo "llama.cpp already cloned in $LLAMA_DIR"
else
  git clone https://github.com/ggerganov/llama.cpp.git "$LLAMA_DIR"
fi

cd "$LLAMA_DIR"
make -j"$(nproc)"

# Install a small wrapper script
cat > /usr/local/bin/llama-cpp-run <<'EOF'
#!/usr/bin/env bash
# Wrapper to run llama.cpp binaries. Usage: llama-cpp-run <model-file> [args...]
LLAMA_DIR=${LLAMA_DIR:-/opt/llama.cpp}
if [ ! -x "$LLAMA_DIR/main" ]; then
  echo "llama.cpp binary not found in $LLAMA_DIR; please build it with scripts/install_llamacpp.sh" >&2
  exit 1
fi
exec "$LLAMA_DIR/main" "$@"
EOF
chmod 0755 /usr/local/bin/llama-cpp-run

mkdir -p "$MODEL_DIR"
chown --no-dereference "$SUDO_USER" "$MODEL_DIR" 2>/dev/null || true

cat <<EOF
Done. llama.cpp built at: $LLAMA_DIR
Wrapper installed at: /usr/local/bin/llama-cpp-run
Models directory (host): $MODEL_DIR

You must NOT place large model files into the container image. Instead, download models to $MODEL_DIR on the host and bind-mount that path into the container at runtime (for example: -v $MODEL_DIR:/var/lib/mcp-llms).
EOF
