#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<EOF
Usage: $0 -f EXECUTION_ENV_YML -n IMAGE_NAME [-k] [-F] [-T TAG]

Creates a temporary build context under /tmp/<image>_<datetime>/context
and builds the execution environment image. By default the image is tagged
as IMAGE_NAME:<datetime>. Use `-F` to flatten the final image (single-layer)
and `-T` to specify a final tag (for example `latest`). The temporary
context is removed unless -k is passed.

Options:
  -f  path to execution-environment.yml
  -n  image name (no tag)
  -k  keep the temporary context directory
  -F  flatten the resulting image (use --squash if supported, else export/import)
  -T  final tag to apply to the flattened image (defaults to the timestamp)
  -h  show this help
EOF
  exit 2
}

EE_FILE=""
IMAGE_NAME=""
KEEP=0
FLATTEN=0
FINAL_TAG=""
RUN_TESTS=0

while getopts ":f:n:khFTr" opt; do
  case "$opt" in
    f) EE_FILE="$OPTARG" ;;
    n) IMAGE_NAME="$OPTARG" ;;
    k) KEEP=1 ;;
    F) FLATTEN=1 ;;
    T) FINAL_TAG="$OPTARG" ;;
    r) RUN_TESTS=1 ;;
    h) usage ;;
    :) echo "Missing arg for -$OPTARG" >&2; usage ;;
    \?) echo "Invalid option: -$OPTARG" >&2; usage ;;
  esac
done

if [[ -z "$EE_FILE" || -z "$IMAGE_NAME" ]]; then
  usage
fi

if ! command -v ansible-builder >/dev/null 2>&1; then
  echo "ansible-builder not found in PATH" >&2
  exit 1
fi
if ! command -v podman >/dev/null 2>&1; then
  echo "podman not found in PATH" >&2
  exit 1
fi

# Check whether rootless podman is usable. If not, and we're not already root,
# try to re-run this script under sudo (preserving PATH) so the build can proceed.
if [[ $(id -u) -ne 0 ]]; then
  if ! podman info >/dev/null 2>&1; then
    if command -v sudo >/dev/null 2>&1; then
      echo "Rootless podman appears unusable; retrying under sudo to perform build"
      exec sudo env PATH="$PATH" "$0" "$@"
    else
      echo "Rootless podman appears unusable and sudo is not available; aborting." >&2
      exit 1
    fi
  fi
fi

# HOSTNAME etc.
HOSTNAME=$(hostname)
# Make a filesystem/container-safe name from the provided image name
IMAGE_SAFE="${IMAGE_NAME//\//_}"
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
TMP_BASE="/tmp/${IMAGE_SAFE}_${TIMESTAMP}"
CONTEXT_DIR="$TMP_BASE/context"

mkdir -p "$CONTEXT_DIR"
echo "Creating ansible-builder context in: $CONTEXT_DIR"
ansible-builder create -f "$EE_FILE" -c "$CONTEXT_DIR"

# Ensure the generated Containerfile installs python3-pip in the base stage
# before any "$PYCMD -m pip ..." invocations. Some base images lack pip
# and ansible-builder may emit pip calls in the base stage.
CONTAINERFILE="$CONTEXT_DIR/Containerfile"
if [[ -f "$CONTAINERFILE" ]]; then
  if grep -q 'RUN \$PYCMD -m pip install --no-cache-dir \$ANSIBLE_INSTALL_REFS' "$CONTAINERFILE"; then
    echo "Patching Containerfile to ensure python3-pip is installed in base stage"
    sed -i '/RUN \$PYCMD -m pip install --no-cache-dir \$ANSIBLE_INSTALL_REFS/i RUN /bin/bash -c '\''if command -v microdnf >/dev/null 2>&1; then microdnf -y install python3-pip || true; elif command -v dnf >/dev/null 2>&1; then dnf -y install python3-pip || true; fi'\''' "$CONTAINERFILE" || true
    # Replace any literal 'python -m pip' calls with '$PYCMD -m pip'
    sed -i "s/\bpython -m pip\b/\$PYCMD -m pip/g" "$CONTAINERFILE" || true
    # If ansible-builder placed pkgmgr.sh under _build/scripts/pkgmgr.sh/pkgmgr.sh,
    # adjust the COPY instruction to use that path so the build can find it.
    if [[ -f "$CONTEXT_DIR/_build/scripts/pkgmgr.sh/pkgmgr.sh" ]]; then
      sed -i "s|COPY scripts/pkgmgr.sh /build/scripts/pkgmgr.sh|COPY _build/scripts/pkgmgr.sh/pkgmgr.sh /build/scripts/pkgmgr.sh|g" "$CONTAINERFILE" || true
    fi
  fi
fi

echo "Pruning podman to free caches before build (podman system prune -a -f)"
podman system prune -a -f || true

IMAGE_PREFIX="${HOSTNAME}/${IMAGE_NAME}"
IMAGE_TAG="${IMAGE_PREFIX}:${TIMESTAMP}"
# Determine desired output tag (defaults to timestamp tag)
if [[ -n "$FINAL_TAG" ]]; then
  OUTPUT_TAG="${IMAGE_PREFIX}:${FINAL_TAG}"
else
  OUTPUT_TAG="$IMAGE_TAG"
fi

echo "Building image $IMAGE_TAG from context $CONTEXT_DIR"

# Check whether podman supports --squash
SUPPORTS_SQUASH=0
if podman build --help 2>&1 | grep -q -- "--squash"; then
  SUPPORTS_SQUASH=1
fi

if [[ $FLATTEN -eq 1 && $SUPPORTS_SQUASH -eq 1 ]]; then
  echo "podman supports --squash; building with --squash into $OUTPUT_TAG"
  podman build --squash -f "$CONTEXT_DIR/Containerfile" -t "$OUTPUT_TAG" "$CONTEXT_DIR"
  BUILD_EXIT=$?
elif [[ $FLATTEN -eq 1 && $SUPPORTS_SQUASH -eq 0 ]]; then
  # build normally to IMAGE_TAG, we'll flatten via export/import later
  podman build -f "$CONTEXT_DIR/Containerfile" -t "$IMAGE_TAG" "$CONTEXT_DIR"
  BUILD_EXIT=$?
else
  podman build -f "$CONTEXT_DIR/Containerfile" -t "$IMAGE_TAG" "$CONTEXT_DIR"
  BUILD_EXIT=$?
fi

if [[ $BUILD_EXIT -eq 0 ]]; then
  echo "Build succeeded"
else
  echo "Build failed" >&2
  exit 1
fi

if [[ $KEEP -eq 0 ]]; then
  echo "Removing temporary context: $TMP_BASE"
  rm -rf "$TMP_BASE"
else
  echo "Keeping temporary context: $TMP_BASE"
fi

# If flatten was requested but podman did not support --squash, perform export/import flattening
  if [[ $FLATTEN -eq 1 && $SUPPORTS_SQUASH -eq 0 ]]; then
  # If OUTPUT_TAG equal to IMAGE_TAG and podman lacked --squash, we still want to flatten IMAGE_TAG -> OUTPUT_TAG
  TEMP_CNTR="temp_flat_${IMAGE_SAFE}_${TIMESTAMP}"
  echo "Creating temporary container $TEMP_CNTR from $IMAGE_TAG to flatten"
  podman create --name "$TEMP_CNTR" "$IMAGE_TAG"
  echo "Exporting and importing to create flattened image $OUTPUT_TAG"
  podman export "$TEMP_CNTR" | podman import - "$OUTPUT_TAG"
  podman rm "$TEMP_CNTR"
  # Remove the original built image to save space
  podman rmi "$IMAGE_TAG" || true
  echo "Flattened image created: $OUTPUT_TAG"
    if [[ $RUN_TESTS -eq 1 ]]; then
      echo "Running smoke tests against $OUTPUT_TAG"
      /usr/bin/env bash "$(dirname "$0")/ee_smoke_test.sh" "$OUTPUT_TAG" || echo "Smoke tests failed" >&2
    fi
    echo "$OUTPUT_TAG"
    exit 0
fi

if [[ $RUN_TESTS -eq 1 ]]; then
  echo "Running smoke tests against $OUTPUT_TAG"
  /usr/bin/env bash "$(dirname "$0")/ee_smoke_test.sh" "$OUTPUT_TAG" || echo "Smoke tests failed" >&2
fi

echo "$OUTPUT_TAG"
