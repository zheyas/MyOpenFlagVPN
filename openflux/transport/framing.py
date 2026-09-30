"""Batched wire format, a direct port of Go's transport/framing.go.

One transport message can carry many mux packets. The frame is:

    [0]   version byte (0x02)
    [1]   flags (bit0 = payload is zstd-compressed)
    [2:]  payload: a sequence of [2-byte big-endian length][packet] records,
          optionally zstd-compressed as a whole.

Only wire-v2 is accepted. This layer is symmetric: both peers wrap their
transport with :class:`BatchedTransport`, which uses these functions.
"""

from __future__ import annotations

import struct
from typing import List

import zstandard as zstd

BATCH_FORMAT_VERSION = 0x02
BATCH_FLAG_ZSTD = 0x01
MAX_FRAME_BYTES = 1 << 20
MAX_FRAME_RECORDS = 1024

# Single-shot compressors are safe to share; level 3 (~SpeedDefault) matches Go.
_enc = zstd.ZstdCompressor(level=3)
_dec = zstd.ZstdDecompressor()
_MAX_DECOMPRESSED = 8 << 20


def frame_batch(pkts: List[bytes]) -> bytes:
    out = bytearray()
    for p in pkts:
        out += struct.pack(">H", len(p))
        out += p
    return bytes(out)


def encode_batch(pkts: List[bytes]) -> bytes:
    framed = frame_batch(pkts)
    compressed = _enc.compress(framed)
    if len(compressed) < len(framed):
        return bytes([BATCH_FORMAT_VERSION, BATCH_FLAG_ZSTD]) + compressed
    return bytes([BATCH_FORMAT_VERSION, 0]) + framed


def decode_batch(data: bytes) -> List[bytes]:
    if len(data) > MAX_FRAME_BYTES + 2:
        raise ValueError("batch frame exceeds size limit")
    if len(data) < 2:
        raise ValueError(f"batch frame too short: {len(data)} bytes")
    if data[0] != BATCH_FORMAT_VERSION:
        raise ValueError(f"unknown batch version 0x{data[0]:02x}")
    flags = data[1]
    if flags & ~BATCH_FLAG_ZSTD != 0:
        raise ValueError(f"unknown batch flags 0x{flags:02x}")
    payload = data[2:]

    framed = payload
    if flags & BATCH_FLAG_ZSTD:
        framed = _dec.decompress(payload, max_output_size=_MAX_DECOMPRESSED)
    if len(framed) > MAX_FRAME_BYTES:
        raise ValueError("decoded batch exceeds size limit")

    pkts: List[bytes] = []
    off = 0
    while off < len(framed):
        if len(pkts) >= MAX_FRAME_RECORDS:
            raise ValueError("batch exceeds record limit")
        if len(framed) - off < 2:
            raise ValueError("truncated length prefix")
        (n,) = struct.unpack_from(">H", framed, off)
        off += 2
        if len(framed) - off < n:
            raise ValueError(f"truncated packet: need {n}, have {len(framed) - off}")
        pkts.append(framed[off:off + n])
        off += n
    return pkts
