"""
credential_id.py
================
Encoding and decoding for the on-chain ``credential_id`` (Solidity ``bytes32``).

A credential's on-chain identifier is laid out as::

    | bytes  0 .. 15 | bytes 16 .. 31 |
    | UUID4 (16 B)   | zero padding   |   (left-aligned)

The returned hex string is the *full* 32-byte representation prefixed with
``0x``. This is important: ``blockchain_service.verify_credential`` calls
``zfill(64)`` on the incoming hex, which pads on the *left*. If we returned
only the 32-char UUID hex, the reconstructed bytes32 would be right-aligned
and would never match the left-aligned record stored at issuance — every
``verifyCredential`` call would silently return "credential not found".

Keeping the encode/decode pair in one module guarantees the two sides
cannot drift apart again.
"""

from __future__ import annotations

import uuid


def make_credential_id() -> tuple[bytes, str]:
    """
    Generate a fresh credential identifier.

    Returns:
        (bytes32, hex_string) — the raw 32-byte value to pass to the
        Solidity contract, and the ``0x``-prefixed 66-character hex string
        to return to the API caller.
    """
    cred_uuid = uuid.uuid4()
    cred_id_bytes = cred_uuid.bytes.ljust(32, b"\x00")[:32]
    return cred_id_bytes, "0x" + cred_id_bytes.hex()


def decode_credential_id(credential_id_hex: str) -> bytes:
    """
    Parse an incoming ``credential_id`` hex string into a 32-byte value.

    Accepts both the full 32-byte hex returned by :func:`make_credential_id`
    and shorter hex strings (left-padded with zeros), although callers
    should always pass the full form.
    """
    return bytes.fromhex(credential_id_hex.removeprefix("0x").zfill(64))
