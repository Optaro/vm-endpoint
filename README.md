# Huginn VM Endpoint

Debian 12 raw QEMU disk image — a remote network scanner endpoint for the Huginn platform.

## Download

**Latest image:** [huginn-vm-endpoint.raw.xz](https://github.com/Optaro/vm-endpoint/releases/latest/download/huginn-vm-endpoint.raw.xz)

Checksum: [huginn-vm-endpoint.raw.xz.sha256](https://github.com/Optaro/vm-endpoint/releases/latest/download/huginn-vm-endpoint.raw.xz.sha256)

All releases: [Releases](https://github.com/Optaro/vm-endpoint/releases)

## Requirements

- Any QEMU/KVM-compatible hypervisor (ESXi, Proxmox, libvirt, Hyper-V)
- 512 MB RAM, 2 vCPUs, ~4 GB disk
- Two NICs: one WAN (NAT/DHCP), one 802.1Q trunk

## Quick start

```sh
# Download and verify
curl -fLO https://github.com/Optaro/vm-endpoint/releases/latest/download/huginn-vm-endpoint.raw.xz
curl -fLO https://github.com/Optaro/vm-endpoint/releases/latest/download/huginn-vm-endpoint.raw.xz.sha256
sha256sum -c huginn-vm-endpoint.raw.xz.sha256

# Decompress
xz -d huginn-vm-endpoint.raw.xz
```

## VM configuration

Create a VM with two NICs and inject the host MAC via SMBIOS:

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
    <interface type='network'>
      <source network='default'/>
      <model type='virtio'/>
    </interface>
    <interface type='direct'>
      <source dev='eth1' mode='bridge'/>
      <model type='virtio'/>
    </interface>
  </devices>
</domain>
```

- Replace `AA:BB:CC:DD:EE:FF` with the **hypervisor host's** primary NIC MAC (must match what's entered in the Huginn admin UI)
- Replace `eth1` with the host NIC carrying the 802.1Q trunk (`ip -br link` to identify)

## API endpoint

The agent defaults to `https://sensor.optaro.io`. Override via:
- Environment variable: `HUGINN_VM_ENDPOINT_API_URL`
- Kernel cmdline: `huginn.api_url=https://your-server.example.com`

## OTA updates

v0.3.0+ images support over-the-air updates. The disk has an A/B root partition layout — updates write to the standby slot and reboot. If the new image fails to connect back to the platform within 5 minutes, GRUB automatically falls back to the previous working image. No manual intervention required.

Updates are triggered server-side via the heartbeat channel and rolled out in canary phases across the fleet.

## License

Proprietary — © Optaro IT
