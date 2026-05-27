"""WireGuard X25519 keypair generation and persistence."""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey


@dataclass
class WGKeys:
    private: str
    public: str


def _generate() -> WGKeys:
    priv = X25519PrivateKey.generate()
    priv_bytes = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_bytes = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return WGKeys(
        private=base64.b64encode(priv_bytes).decode(),
        public=base64.b64encode(pub_bytes).decode(),
    )


def _public_from_private(private_b64: str) -> str:
    raw = base64.b64decode(private_b64)
    priv = X25519PrivateKey.from_private_bytes(raw)
    pub_bytes = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(pub_bytes).decode()


def load_or_create(state_dir: Path) -> WGKeys:
    """Load the persistent keypair, creating it on first boot.

    Private key is mode 0600. Public key is rederived from the private key
    rather than stored separately to avoid drift if one of the files is
    edited by hand.
    """
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    priv_path = state_dir / "wg.key"
    if priv_path.exists():
        private = priv_path.read_text().strip()
        return WGKeys(private=private, public=_public_from_private(private))

    keys = _generate()
    priv_path.write_text(keys.private + "\n")
    os.chmod(priv_path, 0o600)
    return keys
