#!/bin/sh
set -e

# Idempotent PipeWire/Pulse reset script for runner execution (assumes root).
# - Skip UID 0
# - Use XDG_RUNTIME_DIR when invoking `systemctl --user` so the user bus is
#   correctly targeted for each non-root session.

killall -9 pipewire pipewire-pulse wireplumber 2>/dev/null || true

for uid in $(ls -1 /run/user 2>/dev/null | grep -E '^[0-9]+$' | sort -n); do
  # skip root session
  if [ "$uid" -eq 0 ] 2>/dev/null; then
    continue
  fi

  RDIR="/run/user/$uid/pulse"
  # Remove existing dir or dangling symlink
  if [ -e "$RDIR" ] || [ -L "$RDIR" ]; then
    rm -rf "$RDIR" || true
  fi
  mkdir -p "$RDIR" || true
  user=$(getent passwd "$uid" | cut -d: -f1)
  if [ -n "$user" ]; then
    chown -R "$user:$user" "$RDIR" || true
  fi
  chmod 700 "$RDIR" || true

  if [ -n "$user" ]; then
    XRUN="/run/user/$uid"
    if [ -d "$XRUN" ]; then
      # Export XDG_RUNTIME_DIR for the invoked systemctl --user so it connects
      # to the right bus for that user session.
      XDG_RUNTIME_DIR="$XRUN" su -s /bin/sh "$user" -c \
        "XDG_RUNTIME_DIR='$XRUN' systemctl --user reset-failed pipewire-pulse.socket pipewire-pulse.service || true; XDG_RUNTIME_DIR='$XRUN' systemctl --user start pipewire-pulse.socket || true"
    fi
  fi
done

# Status checks for non-root users
for uid in $(ls -1 /run/user 2>/dev/null | grep -E '^[0-9]+$' | sort -n); do
  if [ "$uid" -eq 0 ] 2>/dev/null; then
    continue
  fi
  user=$(getent passwd "$uid" | cut -d: -f1)
  if [ -n "$user" ]; then
    echo "Checking pipewire-pulse.socket status for user $user (uid $uid)"
    XRUN="/run/user/$uid"
    if [ -d "$XRUN" ]; then
      XDG_RUNTIME_DIR="$XRUN" su -s /bin/sh "$user" -c "XDG_RUNTIME_DIR='$XRUN' systemctl --user status pipewire-pulse.socket --no-pager || true"
    fi
  fi
done
