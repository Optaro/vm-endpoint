"""Resolve the host hypervisor's primary NIC MAC.

A QEMU guest cannot see the host's physical MAC directly, so the admin
injects it into the VM via one of three channels (priority order):

  1. Kernel cmdline:   `huginn.host_mac=AA:BB:CC:DD:EE:FF`
                       set in libvirt <os><cmdline> or GRUB.
  2. SMBIOS:           `<sysinfo type='smbios'><system><entry name='serial'>`
                       in libvirt domain XML; read via /sys/class/dmi/id/.
  3. Manual fallback:  `/etc/huginn-vm-endpoint/host_mac` (one line, lowercase MAC).

The resolved MAC is the row key for /api/vm-image/register on the Huginn server.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
CMDLINE_KEY = "huginn.host_mac"
SMBIOS_PATHS = (
    Path("/sys/class/dmi/id/product_serial"),
    Path("/sys/class/dmi/id/chassis_serial"),
    Path("/sys/class/dmi/id/board_serial"),
)
FALLBACK_FILE = Path("/etc/huginn-vm-endpoint/host_mac")


def _normalize(raw: str | None) -> str | None:
    if not raw:
        return None
    candidate = raw.strip().lower().replace("-", ":")
    if MAC_RE.match(candidate):
        return candidate
    return None


def _from_cmdline() -> str | None:
    try:
        cmdline = Path("/proc/cmdline").read_text()
    except OSError:
        return None
    for token in cmdline.split():
        if token.startswith(f"{CMDLINE_KEY}="):
            return _normalize(token.split("=", 1)[1])
    return None


def _from_smbios() -> str | None:
    for path in SMBIOS_PATHS:
        try:
            value = path.read_text()
        except OSError:
            continue
        mac = _normalize(value)
        if mac:
            return mac
    return None


def _from_file() -> str | None:
    try:
        return _normalize(FALLBACK_FILE.read_text())
    except OSError:
        return None


def resolve_host_mac() -> str | None:
    """Return the host MAC, or None if no source had a valid value."""
    for source, fn in (("cmdline", _from_cmdline), ("smbios", _from_smbios), ("file", _from_file)):
        mac = fn()
        if mac:
            log.info("Resolved host MAC %s via %s", mac, source)
            return mac
    log.error(
        "No host MAC found. Set kernel cmdline %s=..., SMBIOS serial, or write %s",
        CMDLINE_KEY, FALLBACK_FILE,
    )
    return None
