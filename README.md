# Huginn VM Endpoint

Debian 12 raw QEMU disk image that acts as a remote network scanner
endpoint. Drop it onto any hypervisor (ESXi, Proxmox, KVM, Hyper-V),
define a 2-NIC VM, and the agent self-registers with the Huginn
platform using the **host machine's** primary NIC MAC.

## Download

Pre-built images are available on the
[Releases](https://github.com/Optaro/vm-endpoint/releases) page.

## Requirements

- Any QEMU/KVM-compatible hypervisor
- 512 MB RAM, 2 vCPUs, ~3 GB disk
- Two NICs: one WAN (NAT/DHCP), one 802.1Q trunk

## Installing on a hypervisor

### Quick start (libvirt)

```sh
# Download and decompress
xz -dc huginn-vm-endpoint-<version>.raw.xz > /var/lib/libvirt/images/huginn-vm-endpoint.raw

# Define, autostart, and launch
virsh define   /etc/libvirt/qemu/huginn-vm-endpoint.xml
virsh autostart huginn-vm-endpoint
virsh start    huginn-vm-endpoint
```

### VM definition

```xml
<domain type='kvm'>
  <name>huginn-vm-endpoint</name>
  <memory unit='MiB'>512</memory>
  <vcpu>2</vcpu>
  <os>
    <type arch='x86_64' machine='pc'>hvm</type>
    <boot dev='hd'/>
    <smbios mode='sysinfo'/>
  </os>
  <sysinfo type='smbios'>
    <system>
      <entry name='serial'>AA:BB:CC:DD:EE:FF</entry>
    </system>
  </sysinfo>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='raw'/>
      <source file='/var/lib/libvirt/images/huginn-vm-endpoint.raw'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <!-- WAN via libvirt's default NAT network -->
    <interface type='network'>
      <source network='default'/>
      <model type='virtio'/>
    </interface>
    <!-- Trunk: macvtap-bridge directly onto the trunk NIC -->
    <interface type='direct'>
      <source dev='eth1' mode='bridge'/>
      <model type='virtio'/>
    </interface>
  </devices>
</domain>
```

Replace `AA:BB:CC:DD:EE:FF` with the **hypervisor host's** primary NIC
MAC (not the VM's virtual NIC). This must match the MAC entered in the
Huginn admin UI when provisioning the scanner.

Replace `eth1` with the host NIC carrying the switch's 802.1Q trunk
(`ip -br link` to identify it).

### Why SMBIOS for the host MAC

libvirt's `<cmdline>` only works with direct-kernel boot. Since this is
a regular disk image booting via GRUB, the host MAC is injected via
SMBIOS serial instead. The in-VM agent reads it from
`/sys/class/dmi/id/product_serial`.

### API endpoint

The agent defaults to `https://sensor.optaro.io`. Override via:
- Environment variable: `HUGINN_VM_ENDPOINT_API_URL`
- Kernel cmdline: `huginn.api_url=https://your-server.example.com`

## Building from source

Requires a Linux host with `libguestfs-tools` and `systemd-container`:

```sh
sudo apt install -y libguestfs-tools systemd-container xz-utils curl
bash build.sh 0.2.0
```

Output: `output/huginn-vm-endpoint-0.2.0.raw.xz` (+ `.sha256`).

## Status from inside the VM

```
journalctl -u huginn-vm-endpoint -f
curl http://127.0.0.1:8645/status
```

## License

Proprietary — © Optaro IT
