#!/bin/bash

# Ensure script is run as root
if [ "$EUID" -ne 0 ]; then
  echo "[FAIL] This script must be run as root or with sudo."
  exit 1
fi

echo "========================================================"
echo " Starting Kernel Boot Log Warning Remediation Suite    "
echo "========================================================"

# --- STEP 1: BLACKLIST UNMAINTAINED ISCSI DRIVERS ---
echo "Step 1: Creating kernel module blacklist rules..."
MODPROBE_CONF="/etc/modprobe.d/blacklist-unmaintained-iscsi.conf"

cat << EOF > "$MODPROBE_CONF"
# Disable unmaintained Broadcom iSCSI offload drivers to suppress boot warnings
blacklist bnx2i
blacklist cnic
EOF
echo "[PASS] Module blacklists registered in /etc/modprobe.d/"

# --- STEP 2: OMIT MODULES FROM INITRAMFS CCACHE ---
echo "Step 2: Instructing Dracut to drop drivers from initial ramdisk images..."
DRACUT_CONF="/etc/dracut.conf.d/omit-iscsi.conf"

cat << EOF > "$DRACUT_CONF"
# Prevents unmaintained network storage components from sneaking into boot image
omit_dracutmodules+=" bnx2i cnic "
EOF
echo "[PASS] Dracut omission configuration saved."

# --- STEP 3: REBUILD ACTIVE INITRAMFS IMAGES ---
echo "Step 3: Rebuilding active system initramfs images (this may take a moment)..."
if command -v dracut &> /dev/null; then
  dracut -f --regenerate-all > /dev/null 2>&1
  echo "[PASS] Initial boot RAM storage trees rebuilt successfully."
else
  echo "[WARN] Dracut tool utility not found. Skipping image rebuild."
fi

# --- STEP 4: SUPPRESS COSMETIC ACPI BLUETOOTH NOISE ---
echo "Step 4: Tuning kernel boot log level threshold parameter..."
if [ -f /etc/default/grub ]; then
  # Check if a loglevel parameter is already configured
  if grep -q "loglevel=" /etc/default/grub; then
    # Adjust any existing loglevel parameters safely to level 3 (Errors only, skips warnings)
    sed -i 's/loglevel=[0-9]/loglevel=3/g' /etc/default/grub
  else
    # Append loglevel=3 to the default kernel boot command line options
    sed -i 's/GRUB_CMDLINE_LINUX="/GRUB_CMDLINE_LINUX="loglevel=3 /g' /etc/default/grub
  fi
  echo "[PASS] GRUB template target settings adjusted."

  # Apply modifications out to the active grub master configuration blocks
  echo "Step 5: Updating bootloader configurations..."
  if [ -f /boot/efi/EFI/fedora/grub.cfg ]; then
    grub2-mkconfig -o /boot/efi/EFI/fedora/grub.cfg > /dev/null 2>&1
  elif [ -f /boot/efi/EFI/redhat/grub.cfg ]; then
    grub2-mkconfig -o /boot/efi/EFI/redhat/grub.cfg > /dev/null 2>&1
  else
    grub2-mkconfig -o /boot/grub2/grub.cfg > /dev/null 2>&1
  fi
  echo "[PASS] Bootloader target environment files synced perfectly."
else
  echo "[WARN] GRUB template map paths not found. Skipping boot configurations."
fi

echo "========================================================"
echo " Remediation Complete! Please restart your machine      "
echo " to verify clean execution logs during boot.           "
echo "========================================================"
