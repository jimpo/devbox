#!/usr/bin/env bash
#
# /// mod
# runas = ["root"]
# ///
#
# mod: kernel - moves an Amazon Linux 2023 box from its stock kernel (6.1) to
# 6.18, following https://docs.aws.amazon.com/linux/al2023/ug/kernel-update.html.
# A no-op on anything else (Fedora already boots a current kernel).
#
# BOX-LEVEL: the kernel is the machine's, so this runs once per box rather than
# once per attachment.
#
# TWO HALVES, ONE REBOOT APART. Until the box is running 6.18, this installs
# kernel6.18, makes it the grub default and drops /run/devbox-reboot-required
# (tmpfs, so the reboot itself clears it). Once it is running 6.18, it removes
# the old kernel and swaps the companion packages (perf, headers, tools, ...)
# over to their 6.18 builds. The old kernel has to go: otherwise a later
# `kernel` (6.1) update could become the default again.
#
# THE REBOOT IS HOST-DRIVEN. A mod body cannot reboot the machine it runs on
# without killing the driver's SSH session mid-run, and a box with attached
# workspaces may have agents running. `devbox box create` reboots when it sees the
# marker and re-runs the box mods; `devbox box sync` only says that a reboot is
# needed.
#
# Idempotency: with 6.18 installed and running and no old kernel left, every
# step is a no-op.
set -euo pipefail

TARGET=6.18

grep -qx 'ID="\?amzn"\?' "${DEVBOX_OS_RELEASE:-/etc/os-release}" || exit 0

case "$(uname -r)" in
  "$TARGET".*)
    for SOURCE_VERSION in "" 6.12; do
      if rpm -q "kernel${SOURCE_VERSION}" >/dev/null; then
        dnf remove -y "kernel${SOURCE_VERSION}"
      fi
      for pkg in bpftool kernel-debuginfo kernel-debuginfo-common kernel-headers \
                 kernel-modules-extra-common perf python3-perf; do
        case "$pkg" in
          kernel-*) src="kernel${SOURCE_VERSION}${pkg#kernel}"; tgt="kernel${TARGET}${pkg#kernel}" ;;
          *)        src="${pkg}${SOURCE_VERSION}";              tgt="${pkg}${TARGET}" ;;
        esac
        if rpm -q "$src" >/dev/null; then dnf -y swap "$src" "$tgt"; fi
      done
      if rpm -q "kernel${SOURCE_VERSION}-tools" >/dev/null; then
        dnf -y remove "kernel${SOURCE_VERSION}-tools" "kernel${SOURCE_VERSION}-tools-devel"
        dnf -y install "kernel${TARGET}-tools" "kernel${TARGET}-tools-devel"
      fi
      if rpm -q "kernel${SOURCE_VERSION}-devel" >/dev/null; then
        dnf remove -y "kernel${SOURCE_VERSION}-devel"
      fi
    done
    ;;
  *)
    if ! rpm -q "kernel${TARGET}" >/dev/null; then dnf install -y "kernel${TARGET}"; fi
    version=$(rpm -q --qf '%{version}-%{release}.%{arch}\n' "kernel${TARGET}" | sort -V | tail -1)
    grubby --set-default "/boot/vmlinuz-$version"
    touch /run/devbox-reboot-required
    echo "kernel: $TARGET installed; a reboot is needed to finish the upgrade."
    ;;
esac
