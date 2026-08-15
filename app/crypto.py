"""Encryption at rest for OAuth tokens.

`MultiFernet` from the first line of code, even with a single key configured. The
alternative — plain `Fernet` now, rotation "later" — is a trap: once ciphertext
exists that only one key can read, rotating means a bespoke migration and a window
where half the rows are unreadable. With `MultiFernet` the head key encrypts, every
key decrypts, and rotation becomes "prepend a key, restart, call
``MultiFernet.rotate`` over the table". That endpoint is deliberately not built
yet; the *shape that makes it a twenty-line endpoint* is.

``encrypted_at`` is stamped on every write for the same reason: when rotation does
arrive, it needs to know which rows are stale, and that answer cannot be
reconstructed after the fact.

**Failure is loud and selective.** A missing key with connections already stored is
fatal at startup — quietly serving with unreadable credentials would surface later
as a run failing for no visible reason. A missing key with *no* connections stored
is fine: the OAuth routes report 503 and everything else works, so a fresh install
runs before you have generated anything.

Nothing here logs a token, a key, or a ciphertext.
"""

from __future__ import annotations

import logging

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


class EncryptionUnavailable(RuntimeError):
    """No usable Fernet key is configured."""


class UndecryptableToken(RuntimeError):
    """Stored ciphertext no key can read — almost always a rotated-away key."""


def _keys(settings: Settings) -> list[str]:
    return [k.strip() for k in (settings.fernet_keys or "").split(",") if k.strip()]


def build_cipher(settings: Settings | None = None) -> MultiFernet:
    """Build the cipher, newest key first.

    Order is the contract: `MultiFernet` encrypts with ``keys[0]`` and tries all of
    them on decrypt, so putting the new key at the head is what makes a rotation
    forward-compatible while old ciphertext stays readable.
    """
    settings = settings or get_settings()
    raw = _keys(settings)
    if not raw:
        raise EncryptionUnavailable(
            "No BLOOM_FERNET_KEYS configured. Generate one with: python -c "
            '"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )
    try:
        return MultiFernet([Fernet(k) for k in raw])
    except (ValueError, TypeError) as exc:
        # Deliberately does not echo the value: a malformed key is still a secret.
        raise EncryptionUnavailable(
            f"BLOOM_FERNET_KEYS is not a valid list of Fernet keys ({len(raw)} entries): {exc}"
        ) from exc


def encrypt(value: str, settings: Settings | None = None) -> bytes:
    """Encrypt a token for storage. An empty value stores as empty, not as noise."""
    if not value:
        return b""
    return build_cipher(settings).encrypt(value.encode("utf-8"))


def decrypt(blob: bytes | None, settings: Settings | None = None) -> str:
    """Decrypt a stored token, or return "" when the column is empty."""
    if not blob:
        return ""
    try:
        return build_cipher(settings).decrypt(bytes(blob)).decode("utf-8")
    except InvalidToken as exc:
        raise UndecryptableToken(
            "Stored token cannot be decrypted with any configured key. If a key was "
            "rotated out before the rows were re-encrypted, restore it at the tail "
            "of BLOOM_FERNET_KEYS."
        ) from exc


def assert_usable(settings: Settings | None = None, *, stored_connections: int = 0) -> None:
    """Startup check. Fatal only when there is something we can no longer read.

    Called from the lifespan so the failure lands at boot, next to the
    configuration that caused it, rather than inside a run days later.
    """
    settings = settings or get_settings()
    try:
        build_cipher(settings)
    except EncryptionUnavailable:
        if stored_connections:
            raise
        logger.info(
            "No BLOOM_FERNET_KEYS configured and no connections stored; OAuth "
            "routes will report 503 and everything else runs normally."
        )
