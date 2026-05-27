#!/bin/bash
# Huginn VM Image — one-shot installer for CentOS 6 (or RHEL 6/7/8) hypervisors.
#
# Downloads the VM Image raw QEMU image, verifies the SHA256, places it under
# /var/lib/libvirt/images, defines a 2-NIC libvirt VM with the host MAC
# injected via SMBIOS, and starts it.
#
# Usage:
#   sudo bash install-vm-image.sh \
#        --host-mac AA:BB:CC:DD:EE:FF \
#        --trunk eth1 \
#        [--wan-net default] \
#        [--version 0.1.0] \
#        [--name huginn-vm-endpoint] \
#        [--mirror https://monitoring.danmagi.io/downloads/vm-image]
#
# Required:
#   --host-mac   MAC of THIS server's primary NIC (the hypervisor itself —
#                NOT a VM port). Must match the MAC pre-paired in the Huginn
#                admin UI for the customer's site.
#   --trunk      Physical NIC on the host carrying the switch's 802.1Q trunk
#                (e.g. eth1, em2, enp3s0). The VM will macvtap-bridge onto it
#                so all VLAN-tagged frames pass through unchanged. No bridge
#                setup required on the host.
#
# Optional:
#   --wan-net    libvirt network name for the VM's WAN interface. Defaults
#                to libvirt's stock NAT network ("default"), which works on
#                any CentOS 6 host with libvirtd running. Override only if
#                that network has been removed or you need a specific path
#                to the internet.

set -euo pipefail

VERSION="0.1.1"
VM_NAME="huginn-vm-endpoint"
MIRROR="https://monitoring.danmagi.io/downloads/vm-image"
HOST_MAC=""
TRUNK_NIC=""
WAN_NET="default"
IMAGE_DIR="/var/lib/libvirt/images"

die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo "[vm-image-install] $*"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --host-mac)   HOST_MAC="$2"; shift 2 ;;
        --trunk)      TRUNK_NIC="$2"; shift 2 ;;
        --wan-net)    WAN_NET="$2"; shift 2 ;;
        --version)    VERSION="$2"; shift 2 ;;
        --name)       VM_NAME="$2"; shift 2 ;;
        --mirror)     MIRROR="$2"; shift 2 ;;
        --image-dir)  IMAGE_DIR="$2"; shift 2 ;;
        -h|--help)    sed -n '2,30p' "$0"; exit 0 ;;
        *)            die "unknown arg: $1" ;;
    esac
done

[ -n "$HOST_MAC" ]  || die "--host-mac is required (host hypervisor's primary NIC)"
[ -n "$TRUNK_NIC" ] || die "--trunk is required (physical NIC carrying the 802.1Q trunk, e.g. eth1)"

# Normalise + validate MAC
HOST_MAC=$(echo "$HOST_MAC" | tr 'A-Z' 'a-z' | tr '-' ':')
echo "$HOST_MAC" | grep -Eq '^[0-9a-f]{2}(:[0-9a-f]{2}){5}$' \
    || die "invalid MAC format: $HOST_MAC"

[ "$(id -u)" -eq 0 ] || die "must run as root"

for cmd in virsh xz curl sha256sum; do
    command -v "$cmd" >/dev/null 2>&1 || die "$cmd not in PATH (yum install libvirt xz curl coreutils)"
done

virsh list --all --name 2>/dev/null | grep -qx "$VM_NAME" \
    && die "a VM named '$VM_NAME' already exists — undefine it or pass --name"

ip link show "$TRUNK_NIC" >/dev/null 2>&1 \
    || die "trunk NIC '$TRUNK_NIC' does not exist (run \`ip -br link\` to list)"

virsh net-info "$WAN_NET" >/dev/null 2>&1 \
    || die "libvirt network '$WAN_NET' not found. Start it (\`virsh net-start default\`) or pass --wan-net <name>"

# Resolve the qemu binary. RHEL/CentOS ship it at /usr/libexec/qemu-kvm;
# Debian/Ubuntu at /usr/bin/qemu-system-x86_64. libvirt validates this
# path when it loads the domain XML, so we have to plant the right one.
QEMU_BIN=""
for candidate in /usr/libexec/qemu-kvm /usr/bin/qemu-kvm /usr/bin/qemu-system-x86_64; do
    if [ -x "$candidate" ]; then
        QEMU_BIN="$candidate"
        break
    fi
done
[ -n "$QEMU_BIN" ] \
    || die "no qemu binary found (tried /usr/libexec/qemu-kvm, /usr/bin/qemu-kvm, /usr/bin/qemu-system-x86_64). Install qemu-kvm (RHEL) or qemu-system-x86 (Debian/Ubuntu)."
log "using qemu: $QEMU_BIN"

mkdir -p "$IMAGE_DIR"

ARTIFACT="huginn-vm-endpoint-${VERSION}.raw.xz"
DEST_RAW="${IMAGE_DIR}/${VM_NAME}.raw"

if [ -f "$DEST_RAW" ]; then
    log "image already at $DEST_RAW — skipping download"
else
    log "downloading $MIRROR/$ARTIFACT"
    TMP=$(mktemp -d)
    trap 'rm -rf "$TMP"' EXIT
    curl -fsSL "$MIRROR/$ARTIFACT"         -o "$TMP/$ARTIFACT"
    curl -fsSL "$MIRROR/$ARTIFACT.sha256"  -o "$TMP/$ARTIFACT.sha256"

    log "verifying SHA256"
    (cd "$TMP" && sha256sum -c "$ARTIFACT.sha256") \
        || die "checksum mismatch — refusing to install"

    log "decompressing → $DEST_RAW"
    xz -dc "$TMP/$ARTIFACT" > "$DEST_RAW"
    chown root:root "$DEST_RAW"
    chmod 0600 "$DEST_RAW"
fi

log "writing libvirt domain XML"
DOMAIN_XML=$(mktemp)
cat > "$DOMAIN_XML" <<EOF
<domain type='kvm'>
  <name>${VM_NAME}</name>
  <memory unit='MiB'>512</memory>
  <vcpu>2</vcpu>
  <!-- SMBIOS injection: the agent reads /sys/class/dmi/id/product_serial.
       This is how the host MAC reaches the guest, since libvirt cmdline
       injection only works in direct-kernel boot, not disk boot. -->
  <os>
    <type arch='x86_64' machine='pc'>hvm</type>
    <boot dev='hd'/>
    <smbios mode='sysinfo'/>
  </os>
  <sysinfo type='smbios'>
    <system>
      <entry name='manufacturer'>Optaro</entry>
      <entry name='product'>Huginn VM Image</entry>
      <entry name='version'>${VERSION}</entry>
      <entry name='serial'>${HOST_MAC}</entry>
    </system>
  </sysinfo>
  <features>
    <acpi/>
    <apic/>
  </features>
  <clock offset='utc'/>
  <on_poweroff>destroy</on_poweroff>
  <on_reboot>restart</on_reboot>
  <on_crash>restart</on_crash>
  <devices>
    <emulator>${QEMU_BIN}</emulator>
    <disk type='file' device='disk'>
      <driver name='qemu' type='raw' cache='none'/>
      <source file='${DEST_RAW}'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <!-- WAN: libvirt's stock NAT network — internet via host. -->
    <interface type='network'>
      <source network='${WAN_NET}'/>
      <model type='virtio'/>
      <alias name='ua-wan'/>
    </interface>
    <!-- Trunk: macvtap-bridge directly onto the physical NIC. The kernel
         creates the macvtap shim at VM start; no Linux bridge needed on
         the host, and 802.1Q tags pass through unchanged. -->
    <interface type='direct'>
      <source dev='${TRUNK_NIC}' mode='bridge'/>
      <model type='virtio'/>
      <alias name='ua-trunk'/>
    </interface>
    <serial type='pty'><target port='0'/></serial>
    <console type='pty'><target type='serial' port='0'/></console>
    <graphics type='vnc' port='-1' autoport='yes' listen='127.0.0.1'/>
    <video><model type='cirrus'/></video>
    <memballoon model='virtio'/>
  </devices>
</domain>
EOF

log "defining and starting VM '$VM_NAME'"
virsh define "$DOMAIN_XML"   >/dev/null
virsh autostart "$VM_NAME"   >/dev/null
virsh start "$VM_NAME"       >/dev/null
rm -f "$DOMAIN_XML"

cat <<EOF

[vm-image-install] done.

VM:        $VM_NAME
Image:     $DEST_RAW
Host MAC:  $HOST_MAC   (injected via SMBIOS serial)
WAN:       libvirt network '$WAN_NET' (NAT)
Trunk:     macvtap-bridge on $TRUNK_NIC

Next:
  - Open the Huginn admin UI for this customer's site.
  - In Deploy Scanner, choose "VM Image (Debian 12 — any hypervisor)" and enter
    the same MAC: $HOST_MAC
  - Wait ~60s and the scanner row will flip to "Online" once the agent
    registers and completes its first heartbeat.

Logs (from this host):
  virsh console $VM_NAME           # live console (Ctrl+] to detach)
  virsh dumpxml $VM_NAME           # full domain XML

Logs (from inside the guest, once you've consoled in):
  journalctl -u huginn-vm-endpoint -f
  curl http://127.0.0.1:8645/status

EOF
