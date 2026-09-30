"""EncryptedTransport — AES-256-GCM wrapper, a port of transport/encrypted.go.

Both peers share a secret; keys are derived with scrypt (N=32768, r=8, p=1) and
split into two directional keys via HMAC-SHA256, so each direction has its own
key. Exactly one peer passes ``exit_node=True`` so the two sides pick opposite
send/receive keys. Every message carries a random 12-byte nonce and is checked
against a bounded replay window.

Wire per message:
    magic 'OFX' | version(1) | direction(1) | nonce(12) | ciphertext+tag

The header is used as GCM associated data, exactly as in Go.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from collections import deque
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .base import ReceiveCallback, Transport, TransportStats

ENCRYPTED_VERSION = 1
ENCRYPTED_HEADER = 5
MAX_SEEN_NONCES = 4096
NONCE_SIZE = 12
GCM_TAG = 16
MAGIC = b"OFX"


def _derive_directional_key(master: bytes, label: str) -> bytes:
    return hmac.new(master, b"OpenFlux direction v1\x00" + label.encode(), hashlib.sha256).digest()


class EncryptedTransport(Transport):
    def __init__(self, inner: Transport, secret: str, context: str, exit_node: bool) -> None:
        if inner is None:
            raise ValueError("inner transport is nil")
        if len(secret) < 16:
            raise ValueError("encryption secret must contain at least 16 characters")

        salt = hashlib.sha256(b"OpenFlux encrypted transport v1\x00" + context.encode()).digest()
        # N=32768,r=8 needs ~34 MiB (128*N*r); raise maxmem above OpenSSL's
        # default 32 MiB or hashlib refuses. Must match Go's scrypt params.
        master = hashlib.scrypt(secret.encode(), salt=salt, n=32768, r=8, p=1,
                                dklen=32, maxmem=64 * 1024 * 1024)
        client_to_exit = _derive_directional_key(master, "client-to-exit")
        exit_to_client = _derive_directional_key(master, "exit-to-client")

        if exit_node:
            send_key, recv_key = exit_to_client, client_to_exit
            self._send_dir, self._recv_dir = 1, 0
        else:
            send_key, recv_key = client_to_exit, exit_to_client
            self._send_dir, self._recv_dir = 0, 1

        self._send_aead = AESGCM(send_key)
        self._recv_aead = AESGCM(recv_key)
        self._inner = inner
        self._seen: set = set()
        self._seen_order: deque = deque()
        self._user_cb: Optional[ReceiveCallback] = None

    async def start(self) -> None:
        await self._inner.start()

    async def stop(self) -> None:
        await self._inner.stop()

    def is_connected(self) -> bool:
        return self._inner.is_connected()

    def stats(self) -> TransportStats:
        return self._inner.stats()

    async def send(self, data: bytes) -> None:
        header = bytes([MAGIC[0], MAGIC[1], MAGIC[2], ENCRYPTED_VERSION, self._send_dir])
        nonce = os.urandom(NONCE_SIZE)
        ct = self._send_aead.encrypt(nonce, data, header)
        await self._inner.send(header + nonce + ct)

    def set_receive(self, cb: ReceiveCallback) -> None:
        self._user_cb = cb

        def on_message(packet: bytes) -> None:
            if len(packet) < ENCRYPTED_HEADER + NONCE_SIZE + GCM_TAG:
                return
            header = packet[:ENCRYPTED_HEADER]
            if (header[0] != MAGIC[0] or header[1] != MAGIC[1] or header[2] != MAGIC[2]
                    or header[3] != ENCRYPTED_VERSION or header[4] != self._recv_dir):
                return
            nonce = packet[ENCRYPTED_HEADER:ENCRYPTED_HEADER + NONCE_SIZE]
            ct = packet[ENCRYPTED_HEADER + NONCE_SIZE:]
            try:
                plaintext = self._recv_aead.decrypt(nonce, ct, header)
            except Exception:  # noqa: BLE001
                return
            if not self._remember_nonce(nonce):
                return
            cb2 = self._user_cb
            if cb2 is not None:
                cb2(plaintext)

        self._inner.set_receive(on_message)

    def _remember_nonce(self, nonce: bytes) -> bool:
        if nonce in self._seen:
            return False
        self._seen.add(nonce)
        self._seen_order.append(nonce)
        if len(self._seen_order) > MAX_SEEN_NONCES:
            oldest = self._seen_order.popleft()
            self._seen.discard(oldest)
        return True
