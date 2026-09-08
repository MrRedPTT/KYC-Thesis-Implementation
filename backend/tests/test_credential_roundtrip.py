"""
Regression tests for ``credential_id`` round-tripping.

Background
----------
The on-chain ``credential_id`` is a Solidity ``bytes32``. Earlier, the issuance
path stored a 16-byte UUID *left-aligned* in that bytes32 but returned only the
32-char UUID hex to the caller, while the verification path hex-decoded the
incoming string and zero-padded it on the *left* (i.e. produced a right-aligned
bytes32). The two layouts never matched, so ``verifyCredential`` returned
"credential not found" for every issued credential — silently breaking the
Bank-B reuse flow that is central to the thesis proposal.

The fix centralises both halves of the encoding in
``app.utils.credential_id``: :func:`make_credential_id` returns the full
32-byte hex prefixed with ``0x``, and :func:`decode_credential_id` parses it
back into the same 32-byte value. These tests pin that contract.
"""

from app.utils.credential_id import decode_credential_id, make_credential_id


def test_roundtrip_recovers_stored_bytes():
    """The verifier must reconstruct the exact bytes32 stored on-chain."""
    for _ in range(50):
        on_chain_bytes, returned_hex = make_credential_id()
        decoded = decode_credential_id(returned_hex)
        assert decoded == on_chain_bytes, (
            "credential_id round-trip failed — verifier would not find the credential "
            f"(stored={on_chain_bytes.hex()}, decoded={decoded.hex()})"
        )


def test_returned_hex_is_full_bytes32():
    """The hex returned to the API caller must be the complete bytes32 form."""
    _, returned_hex = make_credential_id()
    assert returned_hex.startswith("0x")
    assert len(returned_hex) == 66, "expected '0x' + 64 hex chars"
    assert all(c in "0123456789abcdef" for c in returned_hex[2:])


def test_decoded_value_is_32_bytes():
    _, returned_hex = make_credential_id()
    assert len(decode_credential_id(returned_hex)) == 32


def test_uuid_is_left_aligned_within_bytes32():
    """The non-zero UUID octets must occupy the first 16 bytes; the rest is padding."""
    on_chain_bytes, _ = make_credential_id()
    assert len(on_chain_bytes) == 32
    assert on_chain_bytes[16:] == b"\x00" * 16


def test_decoder_accepts_short_hex_via_zfill():
    """A bare 32-char hex is zero-padded on the left to recover a valid bytes32."""
    short = "deadbeef" * 4          # 32 hex chars (16 bytes)
    decoded = decode_credential_id(short)
    assert len(decoded) == 32
    assert decoded[:16] == b"\x00" * 16
    assert decoded[16:].hex() == short
