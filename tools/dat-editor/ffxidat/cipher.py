"""Byte-rotation cipher used by FFXI item DAT files.

The retail client stores every byte of an item record rotated right by
5 bits. To read the file, rotate each byte left by 5 (which is the same
as rotating right by 3). To write it, do the inverse.

The transform is purely per-byte, so it's fully reversible with no key.
"""

from __future__ import annotations


def _rotl(byte: int, n: int) -> int:
    n &= 7
    return ((byte << n) | (byte >> (8 - n))) & 0xFF if n else byte & 0xFF


def decode_bytes(data: bytes) -> bytes:
    """Decode a raw item DAT record into a plaintext view."""
    return bytes(_rotl(b, 5) for b in data)


def encode_bytes(data: bytes) -> bytes:
    """Encode a plaintext record back into DAT form."""
    return bytes(_rotl(b, 3) for b in data)
