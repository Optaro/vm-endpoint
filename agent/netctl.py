"""Idempotent network state applier for the VM Image agent (trunk mode).

Shape this builds, exactly once and then leaves alone:

  wg0          : WireGuard to the cloud scanner. Address {vm_tunnel_ip}/30,
                 peer = cloud scanner pubkey, endpoint = scanner host:port.

  vxlan0       : One VXLAN id={VNI derived from site_id}, dstport 4789,
                 remote = scanner WG tunnel IP, dev=wg0 (so VXLAN UDP
                 rides over WireGuard). nolearning — bridges learn MACs.

  br-trunk     : Plain Linux bridge (no vlan_filtering). Two members:
                   - eth1 (or HUGINN_VM_ENDPOINT_TRUNK), the host-side 802.1Q
                     trunk reaching us via macvtap;
                   - vxlan0.
                 Tagged frames pass through unchanged in either direction.
                 The scanner side does all the per-VLAN demuxing
                 (br0.{vid} sub-interfaces on its vlan_filtering bridge).

That's the entire VM Image netctl surface in v2. There is no per-VLAN state
machine here anymore — VLAN list changes never touch this VM.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

WG_IFACE = "wg0"
VXLAN_IFACE = "vxlan0"
BRIDGE_IFACE = "br-trunk"
VXLAN_PORT = 4789
WG_CONFIG_DIR = Path("/etc/wireguard")


@dataclass(frozen=True)
class WGPeer:
    public_key: str
    endpoint: str          # "host:port"
    allowed_ips: str       # "{scanner_wg_ip}/32"
    tunnel_addr: str       # this VM's WG IP, e.g. "10.99.X.2/30"


def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    log.debug("$ %s", " ".join(args))
    return subprocess.run(args, check=check, capture_output=True, text=True)


def _link_exists(name: str) -> bool:
    return subprocess.run(("ip", "link", "show", name), capture_output=True).returncode == 0


# ---------- WireGuard ----------

def apply_wireguard(*, private_key: str, peer: WGPeer, listen_port: int = 0) -> None:
    """Bring wg0 up and apply the desired peer config (replacing any drift)."""
    WG_CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    key_path = WG_CONFIG_DIR / f"{WG_IFACE}.key"
    key_path.write_text(private_key + "\n")
    key_path.chmod(0o600)

    if not _link_exists(WG_IFACE):
        _run("ip", "link", "add", WG_IFACE, "type", "wireguard")

    _run("ip", "addr", "flush", "dev", WG_IFACE)
    _run("ip", "addr", "add", peer.tunnel_addr, "dev", WG_IFACE)

    listen_args = ["listen-port", str(listen_port)] if listen_port else []
    _run(
        "wg", "set", WG_IFACE,
        "private-key", str(key_path),
        *listen_args,
        "peer", peer.public_key,
        "endpoint", peer.endpoint,
        "allowed-ips", peer.allowed_ips,
        "persistent-keepalive", "25",
    )

    _run("ip", "link", "set", WG_IFACE, "up")


def wg_handshake_ok() -> bool:
    """True if the most-recent handshake on wg0 was within 3 minutes."""
    try:
        out = subprocess.run(
            ("wg", "show", WG_IFACE, "latest-handshakes"),
            check=True, capture_output=True, text=True,
        ).stdout
    except subprocess.CalledProcessError:
        return False
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                ts = int(parts[1])
            except ValueError:
                continue
            if ts > 0:
                import time
                return (time.time() - ts) < 180
    return False


# ---------- Trunk-mode VXLAN + bridge ----------

def apply_trunk(*, trunk_port: str, vni: int, scanner_tunnel_ip: str) -> None:
    """Bring up vxlan0 + br-trunk with vxlan0 and the host trunk as members.

    Idempotent: no-op if everything is already in the desired shape. We
    don't tear anything down — the trunk shape is static once built.
    """
    if not _link_exists(VXLAN_IFACE):
        _run(
            "ip", "link", "add", VXLAN_IFACE,
            "type", "vxlan",
            "id", str(vni),
            "remote", scanner_tunnel_ip,
            "dstport", str(VXLAN_PORT),
            "dev", WG_IFACE,
            "nolearning",
        )

    if not _link_exists(BRIDGE_IFACE):
        _run("ip", "link", "add", BRIDGE_IFACE, "type", "bridge")

    # Enslave members. `master` is idempotent — silently no-ops if already set.
    _run("ip", "link", "set", VXLAN_IFACE, "master", BRIDGE_IFACE)
    _run("ip", "link", "set", trunk_port, "master", BRIDGE_IFACE)

    for iface in (VXLAN_IFACE, BRIDGE_IFACE, trunk_port):
        _run("ip", "link", "set", iface, "up")


def trunk_bridge_ok(*, trunk_port: str) -> bool:
    """Quick health check: all three links exist and are up."""
    for iface in (VXLAN_IFACE, BRIDGE_IFACE, trunk_port):
        if not _link_exists(iface):
            return False
        try:
            out = subprocess.run(
                ("ip", "-br", "link", "show", iface),
                check=True, capture_output=True, text=True,
            ).stdout
        except subprocess.CalledProcessError:
            return False
        # "ip -br" shows state like "UP" or "UNKNOWN" (UNKNOWN is fine for
        # bridge/vxlan, which don't carrier-detect).
        parts = out.split()
        if len(parts) >= 2 and parts[1] not in ("UP", "UNKNOWN"):
            return False
    return True
