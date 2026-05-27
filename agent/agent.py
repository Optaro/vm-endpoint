"""VM Image agent — main loop.

Boot sequence:
  1. Resolve host MAC (cmdline → SMBIOS → file).
  2. Load or generate WG keypair.
  3. POST /api/vm-image/register (idempotent — same MAC always claims same row).
  4. GET  /api/scanners/{site_id}/bootstrap → JSON describing WG peer + VLANs.
  5. Apply WG + VXLAN/VLAN/bridge state via netctl.
  6. Heartbeat every 60s. On vlans_changed=True, refetch /bootstrap.

Local status: 127.0.0.1:8645 serves a JSON snapshot for `journalctl`-shy
debugging from the VM console (tools like `links` / `curl localhost:8645`).
"""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import signal
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import requests
from flask import Flask, jsonify

from . import netctl
from .identity import resolve_host_mac
from .wgkeys import load_or_create

VERSION = "0.2.0"
STATE_DIR = Path(os.environ.get("HUGINN_VM_ENDPOINT_STATE_DIR", "/var/lib/huginn-vm-endpoint"))
TRUNK_IFACE = os.environ.get("HUGINN_VM_ENDPOINT_TRUNK", "eth1")
HEARTBEAT_SECONDS = int(os.environ.get("HUGINN_VM_ENDPOINT_HEARTBEAT", "60"))
STATUS_BIND = os.environ.get("HUGINN_VM_ENDPOINT_STATUS_BIND", "127.0.0.1:8645")


def vni_for_site(site_id: str) -> int:
    """Mirror of scanner-v2/src/reconciler.py:vni_for_site. Both sides
    independently derive the VNI; sha256 keeps the result stable across
    processes (Python's builtin hash() is per-process randomised)."""
    digest = hashlib.sha256(site_id.encode()).digest()
    h = int.from_bytes(digest[:3], "big") & 0x7FFFFF
    return 100_000 + (h % (0x7FFFFF - 100_000))

DEFAULT_API_URL = "https://sensor.optaro.io"


def _api_url() -> str:
    """Resolve API URL: env override → kernel cmdline → default."""
    if "HUGINN_VM_ENDPOINT_API_URL" in os.environ:
        return os.environ["HUGINN_VM_ENDPOINT_API_URL"].rstrip("/")
    try:
        cmdline = Path("/proc/cmdline").read_text()
        for token in cmdline.split():
            if token.startswith("huginn.api_url="):
                return token.split("=", 1)[1].rstrip("/")
    except OSError:
        pass
    return DEFAULT_API_URL


@dataclass
class AgentState:
    started_at: float = field(default_factory=time.time)
    host_mac: str | None = None
    public_key: str | None = None
    api_url: str = ""
    scanner_id: str | None = None
    site_id: str | None = None
    last_heartbeat_at: float | None = None
    last_heartbeat_ok: bool = False
    last_bootstrap_at: float | None = None
    vni: int | None = None
    last_error: str | None = None
    wg_handshake_ok: bool = False
    trunk_bridge_ok: bool = False


STATE = AgentState()


# ---------- API client ----------

def _post_register(api_url: str, mac: str, public_key: str) -> dict | None:
    body = {
        "mac": mac,
        "public_key": public_key,
        "hostname": platform.node(),
        "kernel": platform.release(),
        "agent_version": VERSION,
    }
    try:
        r = requests.post(f"{api_url}/api/vm-image/register", json=body, timeout=10)
    except requests.RequestException as exc:
        logging.warning("register: %s", exc)
        return None
    if r.status_code == 404:
        logging.error("register: no pairing for MAC %s on server (admin must pre-pair)", mac)
        return None
    if not r.ok:
        logging.warning("register: HTTP %s — %s", r.status_code, r.text[:200])
        return None
    return r.json()


def _get_bootstrap(api_url: str, site_id: str, auth_token: str) -> dict | None:
    try:
        r = requests.get(
            f"{api_url}/api/scanners/{site_id}/bootstrap",
            headers={"Authorization": f"Bearer {auth_token}"},
            timeout=20,
        )
    except requests.RequestException as exc:
        logging.warning("bootstrap: %s", exc)
        return None
    if not r.ok:
        logging.warning("bootstrap: HTTP %s — %s", r.status_code, r.text[:200])
        return None
    return r.json()


def _post_heartbeat(api_url: str, scanner_id: str, auth_token: str) -> dict | None:
    # Trunk-mode endpoint: VLAN list lives entirely scanner-side now, so
    # we no longer report it. Heartbeat just signals "tunnel + bridge up".
    body = {
        "version": VERSION,
        "model": platform.node(),
        "tunnel_status": "connected" if STATE.wg_handshake_ok else "waiting_handshake",
        "trunk_bridge_ok": STATE.trunk_bridge_ok,
    }
    try:
        r = requests.post(
            f"{api_url}/api/scanners/{scanner_id}/heartbeat",
            headers={"Authorization": f"Bearer {auth_token}"},
            json=body,
            timeout=15,
        )
    except requests.RequestException as exc:
        logging.warning("heartbeat: %s", exc)
        return None
    if not r.ok:
        logging.warning("heartbeat: HTTP %s — %s", r.status_code, r.text[:200])
        return None
    return r.json()


# ---------- State application ----------

def _apply_bootstrap(bootstrap: dict, *, private_key: str, scanner_endpoint: str, scanner_pubkey: str = "", scanner_tunnel_ip_override: str = "") -> None:
    """Apply WG + trunk-bridge state from a /bootstrap response.

    Trunk-mode only. v2 scanner-paired VM Image refuses to run against a
    per-VLAN-mode site — that endpoint flavour stays on the v1 agent.

    scanner_pubkey and scanner_tunnel_ip_override both come from /register,
    not /bootstrap. /bootstrap's `peer_public_key` is written for the
    cloud-side caller (where it means "the VM Image's pubkey"), and its
    `peer_allowed_ips` is the VM Image's own IP — using either would point us
    at ourselves.
    """
    vlan_mode = bootstrap.get("vlan_mode", "per_vlan")
    transport = bootstrap.get("transport", "wireguard")

    if vlan_mode != "trunk":
        raise RuntimeError(
            f"Bootstrap says vlan_mode={vlan_mode!r}; VM Image is trunk-only. "
            "Flip site_scanners.vlan_mode='trunk' or run the v1 agent."
        )
    if transport != "wireguard":
        raise RuntimeError(
            f"Bootstrap says transport={transport!r}; VM Image uses WireGuard."
        )

    wg = bootstrap.get("wireguard") or {}
    peer_pubkey = scanner_pubkey or wg.get("peer_public_key")
    # mikrotik_tunnel_ip is the legacy field name — for VM Image it's "this VM's WG IP"
    my_tunnel_ip = bootstrap.get("mikrotik_tunnel_ip") or "10.99.1.2"
    # /register provides scanner_tunnel_ip authoritatively. /bootstrap's
    # peer_allowed_ips is the VM Image's own IP so we can't fall back to it.
    scanner_tunnel_ip = scanner_tunnel_ip_override
    if not scanner_tunnel_ip:
        raise RuntimeError(
            "Missing scanner_tunnel_ip — /register did not return it. "
            "Old server? Bootstrap a fresh registration."
        )

    if not peer_pubkey:
        raise RuntimeError("Bootstrap response missing wireguard.peer_public_key")

    netctl.apply_wireguard(
        private_key=private_key,
        peer=netctl.WGPeer(
            public_key=peer_pubkey,
            endpoint=scanner_endpoint,
            allowed_ips=f"{scanner_tunnel_ip}/32",
            tunnel_addr=f"{my_tunnel_ip}/30",
        ),
    )

    vni = vni_for_site(STATE.site_id)
    netctl.apply_trunk(
        trunk_port=TRUNK_IFACE,
        vni=vni,
        scanner_tunnel_ip=scanner_tunnel_ip,
    )
    STATE.vni = vni
    logging.info(
        "Trunk applied: wg %s -> %s, vxlan0 vni=%d remote=%s, bridge=%s trunk=%s",
        my_tunnel_ip, scanner_tunnel_ip, vni, scanner_tunnel_ip,
        netctl.BRIDGE_IFACE, TRUNK_IFACE,
    )


# ---------- Status server ----------

def make_status_app() -> Flask:
    app = Flask("huginn-vm-endpoint-status")

    @app.get("/")
    @app.get("/status")
    def status():
        return jsonify({
            "version": VERSION,
            "uptime_s": round(time.time() - STATE.started_at, 1),
            **{k: v for k, v in asdict(STATE).items() if not k.startswith("_")},
        })

    return app


def _serve_status() -> None:
    app = make_status_app()
    host, _, port = STATUS_BIND.partition(":")
    app.run(host=host or "127.0.0.1", port=int(port or 8645), debug=False, use_reloader=False)


# ---------- Main loop ----------

def _bootstrap_loop() -> None:
    api_url = _api_url()
    STATE.api_url = api_url
    logging.info("VM Image agent %s — API %s", VERSION, api_url)

    # 1. Identity
    mac = resolve_host_mac()
    if not mac:
        STATE.last_error = "no host MAC available"
        # Keep polling — admin may write the file later.
        while not mac:
            time.sleep(30)
            mac = resolve_host_mac()
    STATE.host_mac = mac

    # 2. Keys
    keys = load_or_create(STATE_DIR)
    STATE.public_key = keys.public

    # 3. Register (retry forever — admin may not have pre-paired yet)
    auth_token: str | None = None
    scanner_endpoint = ""
    scanner_tunnel_ip = ""
    while auth_token is None:
        result = _post_register(api_url, mac, keys.public)
        if result and result.get("auth_token"):
            STATE.scanner_id = result["scanner_id"]
            STATE.site_id = result["site_id"]
            scanner_endpoint = result["scanner_endpoint"]
            scanner_pubkey = result.get("scanner_public_key") or ""
            # /register returns scanner_tunnel_ip directly. /bootstrap's
            # peer_allowed_ips is the *VM Image's* IP (it's written for the
            # cloud-side caller), so deriving the scanner's tunnel IP from
            # that field would put both endpoints on the same address.
            scanner_tunnel_ip = result.get("scanner_tunnel_ip") or ""
            auth_token = result["auth_token"]
            STATE.last_error = None
            logging.info("Registered: scanner_id=%s site_id=%s endpoint=%s",
                         STATE.scanner_id, STATE.site_id, scanner_endpoint)
        else:
            STATE.last_error = "registration pending — admin must pair host MAC"
            time.sleep(30)

    # 4. Initial bootstrap + apply
    _refresh(auth_token, keys.private, scanner_endpoint, scanner_pubkey, scanner_tunnel_ip)

    # 5. Heartbeat loop — trunk mode has no VLAN drift on the endpoint
    # side, so we don't refetch bootstrap on `vlans_changed`. The shape
    # is static once applied; the scanner does its own per-VLAN reconcile.
    while True:
        time.sleep(HEARTBEAT_SECONDS)
        STATE.wg_handshake_ok = netctl.wg_handshake_ok()
        STATE.trunk_bridge_ok = netctl.trunk_bridge_ok(trunk_port=TRUNK_IFACE)
        resp = _post_heartbeat(api_url, STATE.scanner_id, auth_token)
        STATE.last_heartbeat_at = time.time()
        STATE.last_heartbeat_ok = bool(resp)


def _refresh(auth_token: str, private_key: str, scanner_endpoint: str, scanner_pubkey: str = "", scanner_tunnel_ip: str = "") -> None:
    bootstrap = _get_bootstrap(STATE.api_url, STATE.site_id, auth_token)
    if not bootstrap:
        STATE.last_error = "bootstrap fetch failed"
        return
    STATE.last_bootstrap_at = time.time()
    try:
        _apply_bootstrap(
            bootstrap, private_key=private_key, scanner_endpoint=scanner_endpoint,
            scanner_pubkey=scanner_pubkey, scanner_tunnel_ip_override=scanner_tunnel_ip,
        )
    except Exception as exc:
        STATE.last_error = f"apply: {exc}"
        logging.exception("Failed to apply bootstrap")
        return
    STATE.last_error = None


def _install_signal_handlers() -> None:
    def _quit(_sig, _frame):
        logging.info("Signal received — exiting")
        os._exit(0)
    signal.signal(signal.SIGTERM, _quit)
    signal.signal(signal.SIGINT, _quit)


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("HUGINN_VM_ENDPOINT_LOG", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _install_signal_handlers()

    threading.Thread(target=_serve_status, name="status", daemon=True).start()

    try:
        _bootstrap_loop()
    except Exception as exc:
        logging.exception("Fatal error in bootstrap loop: %s", exc)
        STATE.last_error = str(exc)
        # Don't exit — let systemd Restart=on-failure handle it after a backoff.
        time.sleep(30)
        raise

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
