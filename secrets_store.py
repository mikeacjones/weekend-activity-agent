"""Encrypted secrets storage for dynamic tool API keys.

Keys are encrypted with Fernet (AES-128-CBC) using a master key derived
from the SECRETS_MASTER_KEY env var. Stored in the secrets/ directory
which is volume-mounted for persistence.
"""

import base64
import hashlib
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet

SECRETS_DIR = Path(__file__).parent / "secrets"


def _get_fernet() -> Fernet:
    master = os.environ.get("SECRETS_MASTER_KEY", "")
    if not master:
        raise RuntimeError(
            "SECRETS_MASTER_KEY env var is required for secrets storage. "
            "Set it to any strong passphrase."
        )
    # Derive a 32-byte key from the passphrase
    key = base64.urlsafe_b64encode(hashlib.sha256(master.encode()).digest())
    return Fernet(key)


def save_secret(name: str, value: str):
    """Encrypt and save a secret to disk."""
    SECRETS_DIR.mkdir(exist_ok=True)
    f = _get_fernet()
    encrypted = f.encrypt(value.encode())
    (SECRETS_DIR / f"{name}.enc").write_bytes(encrypted)


def get_secret(name: str) -> str | None:
    """Read and decrypt a secret. Returns None if not found."""
    path = SECRETS_DIR / f"{name}.enc"
    if not path.exists():
        # Fall back to env var
        return os.environ.get(name)
    f = _get_fernet()
    return f.decrypt(path.read_bytes()).decode()


def list_secrets() -> list[str]:
    """List all stored secret names."""
    if not SECRETS_DIR.exists():
        return []
    return [p.stem for p in SECRETS_DIR.glob("*.enc")]
