#!/bin/bash
# Build the VM Image raw QEMU image by customising the Debian 12 generic cloud
# image. Pivoted from mkosi (which insisted on UEFI in v24) to virt-customize
# because BIOS boot is what CentOS 6 + SeaBIOS actually expects.
#
# Network-touching steps (apt-get update / install) run via systemd-nspawn,
# NOT virt-customize's libguestfs appliance. The appliance's qemu user-mode
# slirp DNS forwarding is broken on at least Ubuntu 25.04 / kernel 6.14 /
# qemu 9.2.1, which makes apt fail with "Temporary failure resolving
# deb.debian.org" even when the host network is fully working. nspawn boots
# the target rootfs directly with normal Linux container networking and
# bind-mounts the host's /etc/resolv.conf, so it Just Works.
#
# Run on a Linux host with libguestfs-tools + systemd-container installed.
#
# Usage:
#     bash build.sh [version]       # version defaults to 0.1.1

set -euo pipefail

VERSION="${1:-0.1.1}"
BASE_URL="https://cloud.debian.org/images/cloud/bookworm/latest"
BASE_FILENAME="debian-12-genericcloud-amd64.raw"
WORK="${PWD}/output"
RAW="${WORK}/huginn-vm-endpoint-${VERSION}.raw"
ARTIFACT="${WORK}/huginn-vm-endpoint-${VERSION}.raw.xz"

command -v virt-customize >/dev/null \
    || { echo "virt-customize not found (apt install libguestfs-tools)"; exit 1; }
command -v systemd-nspawn >/dev/null \
    || { echo "systemd-nspawn not found (apt install systemd-container)"; exit 1; }

mkdir -p "$WORK"

if [ ! -f "${WORK}/${BASE_FILENAME}" ]; then
    echo "[build] downloading $BASE_FILENAME"
    curl -fsSL --output "${WORK}/${BASE_FILENAME}" "${BASE_URL}/${BASE_FILENAME}"
fi

# Fresh copy each build so we can re-run safely.
echo "[build] copying base image → ${RAW}"
cp "${WORK}/${BASE_FILENAME}" "${RAW}"

# The Debian generic cloud image is ~3 GiB sparse already. We keep the
# size as-is — sparse blocks compress to nothing under xz, and shrinking
# would risk corrupting the filesystem.

# Stage agent code into a tempdir tree so virt-customize --copy-in can
# pick it up at the right rootfs path. We copy `agent/` (the package dir)
# whole — NOT its contents — so the in-image layout is
# /usr/local/lib/huginn-vm-endpoint/agent/<files>, which is what `python -m agent`
# expects given PYTHONPATH=/usr/local/lib/huginn-vm-endpoint in the unit file.
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "${STAGE}/huginn-vm-endpoint/agent"
cp -a agent/. "${STAGE}/huginn-vm-endpoint/agent/"
find "${STAGE}/huginn-vm-endpoint/agent" -name __pycache__ -prune -exec rm -rf {} +

# ─── Phase A: virt-customize (offline, file ops + grub) ───────────────────
echo "[build] phase A: virt-customize (offline file ops)"
sudo virt-customize \
    --quiet \
    -a "${RAW}" \
    --hostname huginn-vm-endpoint \
    --root-password disabled \
    --mkdir /etc/systemd/network \
    --mkdir /etc/huginn-vm-endpoint \
    --copy-in "${STAGE}/huginn-vm-endpoint:/usr/local/lib/" \
    --run-command 'install -d /usr/local/lib/huginn-vm-endpoint/venv/bin && ln -sf /usr/bin/python3 /usr/local/lib/huginn-vm-endpoint/venv/bin/python' \
    --copy-in mkosi.extra/etc/systemd/system/huginn-vm-endpoint.service:/etc/systemd/system/ \
    --copy-in mkosi.extra/etc/systemd/network/10-eth0.network:/etc/systemd/network/ \
    --copy-in mkosi.extra/etc/systemd/network/20-eth1.network:/etc/systemd/network/ \
    --copy-in mkosi.extra/etc/huginn-vm-endpoint/README:/etc/huginn-vm-endpoint/ \
    --run-command 'systemctl enable huginn-vm-endpoint.service' \
    --run-command 'systemctl enable systemd-networkd.service' \
    --run-command 'systemctl enable systemd-resolved.service' \
    --run-command 'systemctl disable cloud-init.service cloud-init-local.service cloud-config.service cloud-final.service 2>/dev/null || true' \
    --run-command "sed -i 's|^GRUB_CMDLINE_LINUX_DEFAULT=.*|GRUB_CMDLINE_LINUX_DEFAULT=\"console=ttyS0,115200 console=tty0 net.ifnames=0 biosdevname=0\"|' /etc/default/grub" \
    --run-command "sed -i 's|^GRUB_CMDLINE_LINUX=.*|GRUB_CMDLINE_LINUX=\"net.ifnames=0 biosdevname=0\"|' /etc/default/grub" \
    --run-command 'update-grub'

# ─── Phase B: systemd-nspawn (online, apt install) ────────────────────────
echo "[build] phase B: systemd-nspawn apt install"
PKGS="wireguard-tools iproute2 bridge-utils iputils-ping ca-certificates python3 python3-cryptography python3-flask python3-requests curl less"
sudo systemd-nspawn -q -i "${RAW}" \
    --bind-ro=/etc/resolv.conf:/etc/resolv.conf \
    --as-pid2 \
    /bin/bash -c "
        set -e
        export DEBIAN_FRONTEND=noninteractive
        apt-get -q -y update
        apt-get -q -y install ${PKGS}
        apt-get -q -y clean
        rm -rf /var/lib/apt/lists/*
    "

# ─── Phase C: virt-customize (offline final cleanup) ──────────────────────
echo "[build] phase C: virt-customize (final cleanup)"
sudo virt-customize \
    --quiet \
    -a "${RAW}" \
    --truncate /etc/machine-id

# Compress + sha
echo "[build] compressing"
xz -T0 -9 -k -f "${RAW}"
# xz -k produces ${RAW}.xz directly — no rename needed since ARTIFACT == ${RAW}.xz.
( cd "${WORK}" && sha256sum "$(basename "$ARTIFACT")" > "$(basename "$ARTIFACT").sha256" )

echo
echo "Built: $ARTIFACT"
ls -lh "${RAW}" "${ARTIFACT}" "${ARTIFACT}.sha256"
