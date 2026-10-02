"""Signed portable receipts: a hunter's proof of work they can show anywhere.

When a bounty completes, the hunter gets a receipt signed with DAiL's
Ed25519 key. Anyone can verify it against the public key published at
/.well-known/dail-pubkey — no DAiL account needed. This is the reputation
*export*: the passport is the home, the signature is the resume.

Key management:
- Production: set DAIL_RECEIPT_SIGNING_KEY to a 32-byte hex seed.
- Unset: an ephemeral key is generated at startup and a warning is logged.
  Ephemeral keys mean receipts don't survive a redeploy — fine for tests,
  never for production (deploy with the env var set).

The signed payload is canonical JSON (sorted keys, no whitespace) over the
receipt fields EXCLUDING the signature itself.
"""
import json
import logging
import os
import secrets

log = logging.getLogger("dail.receipts")

_SIGNING_KEY_ENV = "DAIL_RECEIPT_SIGNING_KEY"


def _load_private_key():
    from cryptography.hazmat.primitives.asymmetric import ed25519
    seed_hex = os.getenv(_SIGNING_KEY_ENV, "").strip()
    if seed_hex:
        try:
            seed = bytes.fromhex(seed_hex)
        except ValueError:
            raise RuntimeError(
                f"{_SIGNING_KEY_ENV} must be a 32-byte hex seed")
        if len(seed) != 32:
            raise RuntimeError(
                f"{_SIGNING_KEY_ENV} must be a 32-byte hex seed")
        return ed25519.Ed25519PrivateKey.from_private_bytes(seed)
    log.warning(
        "DAIL_RECEIPT_SIGNING_KEY not set: using an EPHEMERAL receipt "
        "signing key. Receipts will not verify after a restart. "
        "Set the env var in production.")
    return ed25519.Ed25519PrivateKey.from_private_bytes(secrets.token_bytes(32))


_private_key = _load_private_key()
_public_key = _private_key.public_key()


def public_key_hex() -> str:
    """Hex of the raw 32-byte Ed25519 public key."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PublicKey)
    from cryptography.hazmat.primitives import serialization
    return _public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw).hex()


def canonical(payload: dict) -> bytes:
    """Canonical bytes of a receipt payload (signature field excluded)."""
    body = {k: v for k, v in payload.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def sign(payload: dict) -> str:
    """Sign a receipt payload; returns the hex signature."""
    return _private_key.sign(canonical(payload)).hex()


def verify(payload: dict, signature_hex: str, pubkey_hex: str) -> bool:
    """Verify a receipt against a public key. Never raises on bad input."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey)
        from cryptography.hazmat.primitives import serialization
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex))
        pub.verify(bytes.fromhex(signature_hex), canonical(payload))
        return True
    except Exception:
        return False
