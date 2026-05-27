"""Fake-agent harness — exercises the VM Image server endpoints end-to-end.

This is the local round-trip you'd run after spinning up Huginn locally and
provisioning a VM Image scanner through the UI (or via curl). It does not bring
up any kernel state — no `wg`, no `ip link` — it just speaks the API that
the real agent speaks.

Usage:
    python -m agent.test_harness \\
        --api http://localhost:8000 \\
        --mac aa:bb:cc:dd:ee:ff

Expected flow (each step prints PASS or FAIL):
    1. POST /api/vm-image/register     → returns {scanner_id, site_id, auth_token, …}
    2. GET  /api/scanners/{id}/bootstrap → returns wireguard{} + vlans[]
    3. POST /api/scanners/{id}/heartbeat → returns vlans_changed:bool

Run with --verbose to dump full request/response bodies. Run with
--repeat-heartbeat N to send N heartbeats (handy when poking VLAN drift via
the admin UI in another tab).
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time

import requests

from .wgkeys import load_or_create
from pathlib import Path


def _ok(label: str, msg: str = "") -> None:
    print(f"  \033[32mPASS\033[0m {label}" + (f" — {msg}" if msg else ""))


def _fail(label: str, msg: str = "") -> None:
    print(f"  \033[31mFAIL\033[0m {label}" + (f" — {msg}" if msg else ""))


def _step(n: int, name: str) -> None:
    print(f"\n[{n}] {name}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--api", default="http://localhost:8000", help="Huginn API base URL")
    p.add_argument("--mac", required=True, help="Pre-paired host MAC")
    p.add_argument("--state-dir", default="/tmp/huginn-vm-endpoint-test", help="Where to keep the WG keypair")
    p.add_argument("--verbose", action="store_true", help="Dump full request/response bodies")
    p.add_argument("--repeat-heartbeat", type=int, default=1, help="Number of heartbeats to send (default 1)")
    p.add_argument("--heartbeat-interval", type=float, default=2.0, help="Seconds between heartbeats")
    args = p.parse_args()

    api = args.api.rstrip("/")
    state_dir = Path(args.state_dir)
    keys = load_or_create(state_dir)
    print(f"WG public key (this fake agent): {keys.public}")

    failures = 0

    # ---------- Step 1: register ----------
    _step(1, "POST /api/vm-image/register")
    register_body = {
        "mac": args.mac,
        "public_key": keys.public,
        "hostname": platform.node(),
        "kernel": platform.release(),
        "agent_version": "test-harness/0.1",
    }
    if args.verbose:
        print(f"  > body: {json.dumps(register_body, indent=2)}")

    try:
        r = requests.post(f"{api}/api/vm-image/register", json=register_body, timeout=10)
    except requests.RequestException as exc:
        _fail("register", f"connection error: {exc}")
        return 2

    if args.verbose:
        print(f"  < status: {r.status_code}")
        print(f"  < body: {r.text[:400]}")

    if r.status_code == 404:
        _fail(
            "register",
            f"server has no scanner paired to MAC {args.mac}. "
            "Provision one in the UI first (Site → Collectors → Deploy → VM Image → enter this MAC).",
        )
        return 2
    if not r.ok:
        _fail("register", f"HTTP {r.status_code}: {r.text[:200]}")
        return 2

    reg = r.json()
    needed = {"scanner_id", "site_id", "auth_token", "tunnel_ip",
              "scanner_tunnel_ip", "scanner_endpoint", "scanner_public_key"}
    missing = needed - set(reg.keys())
    if missing:
        _fail("register response shape", f"missing keys: {missing}")
        failures += 1
    else:
        _ok("register response shape", "all keys present")

    scanner_id = reg.get("scanner_id")
    site_id = reg.get("site_id")
    auth_token = reg.get("auth_token")
    print(f"  scanner_id     = {scanner_id}")
    print(f"  site_id        = {site_id}")
    print(f"  scanner_endpoint = {reg.get('scanner_endpoint')}")
    print(f"  scanner_pubkey   = {reg.get('scanner_public_key')[:25]}…")
    print(f"  this VM tunnel IP = {reg.get('tunnel_ip')}")
    print(f"  cloud scanner IP  = {reg.get('scanner_tunnel_ip')}")

    # ---------- Step 2: bootstrap ----------
    _step(2, f"GET /api/scanners/{site_id}/bootstrap")
    try:
        r = requests.get(
            f"{api}/api/scanners/{site_id}/bootstrap",
            headers={"Authorization": f"Bearer {auth_token}"},
            timeout=15,
        )
    except requests.RequestException as exc:
        _fail("bootstrap", f"connection error: {exc}")
        return 2

    if not r.ok:
        _fail("bootstrap", f"HTTP {r.status_code}: {r.text[:200]}")
        return 2

    boot = r.json()
    if args.verbose:
        print(f"  < body keys: {list(boot.keys())}")

    if "wireguard" not in boot or "vlans" not in boot:
        _fail("bootstrap shape", f"missing wireguard/vlans, got: {list(boot.keys())}")
        failures += 1
    else:
        _ok("bootstrap shape", f"{len(boot['vlans'])} VLANs returned")

    wg = boot.get("wireguard", {})
    if wg.get("peer_public_key") != reg.get("scanner_public_key"):
        _fail("peer pubkey consistency",
              f"register said {reg.get('scanner_public_key')!r}, bootstrap said {wg.get('peer_public_key')!r}")
        failures += 1
    else:
        _ok("peer pubkey consistency")

    print(f"  vlans = {[v.get('vlan_id') for v in boot.get('vlans', [])]}")

    # ---------- Step 3: heartbeat (×N) ----------
    _step(3, f"POST /api/scanners/{scanner_id}/heartbeat × {args.repeat_heartbeat}")
    current_vlans = [v["vlan_id"] for v in boot.get("vlans", []) if v.get("vlan_id")]

    for i in range(args.repeat_heartbeat):
        if i:
            time.sleep(args.heartbeat_interval)
        hb = {
            "version": "test-harness/0.1",
            "model": platform.node(),
            "tunnel_status": "connected",
            "vlans": current_vlans,
        }
        try:
            r = requests.post(
                f"{api}/api/scanners/{scanner_id}/heartbeat",
                headers={"Authorization": f"Bearer {auth_token}"},
                json=hb,
                timeout=15,
            )
        except requests.RequestException as exc:
            _fail(f"heartbeat #{i+1}", f"connection error: {exc}")
            failures += 1
            continue

        if not r.ok:
            _fail(f"heartbeat #{i+1}", f"HTTP {r.status_code}: {r.text[:200]}")
            failures += 1
            continue

        body = r.json()
        if "vlans_changed" not in body:
            _fail(f"heartbeat #{i+1} shape", "missing vlans_changed")
            failures += 1
            continue

        _ok(f"heartbeat #{i+1}", f"vlans_changed={body['vlans_changed']} peer_configured={body.get('peer_configured')}")

        if body["vlans_changed"]:
            print("    Server reports VLAN drift — agent would refetch /bootstrap here.")

    # ---------- Done ----------
    print()
    if failures:
        print(f"\033[31m{failures} failures\033[0m — see above.")
        return 1
    print("\033[32mAll checks passed.\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
