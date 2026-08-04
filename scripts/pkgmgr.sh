#!/bin/sh
# pkgmgr wrapper for RHEL/Fedora-based images: prefer microdnf, then dnf
if command -v microdnf >/dev/null 2>&1; then
  microdnf "$@"
elif command -v dnf >/dev/null 2>&1; then
  dnf "$@"
else
  echo "No supported RHEL/Fedora package manager (microdnf/dnf) found inside container" >&2
  exit 127
fi
