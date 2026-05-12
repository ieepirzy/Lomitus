#!/usr/bin/env python3
"""
bloom.py — counting bloom filter with SQLite persistence.

4-bit counters (two per byte) support add and remove without a full rebuild.
Persisted across short-lived hook process invocations via the coordinator DB.
"""

from __future__ import annotations

import hashlib
import math
import sqlite3
import struct

_CAPACITY   = 10_000
_ERROR_RATE = 0.01


class CountingBloomFilter:
    def __init__(self, capacity: int = _CAPACITY, error_rate: float = _ERROR_RATE):
        self.capacity   = capacity
        self.error_rate = error_rate
        self.m = math.ceil(-capacity * math.log(error_rate) / (math.log(2) ** 2))
        self.k = max(1, round((self.m / capacity) * math.log(2)))
        self._data = bytearray((self.m + 1) // 2)  # ceil(m/2) bytes, two 4-bit counters each

    def _positions(self, item: str) -> list[int]:
        b = item.encode()
        return [
            int.from_bytes(hashlib.sha256(b + struct.pack(">I", i)).digest()[:8], "big") % self.m
            for i in range(self.k)
        ]

    def _get(self, pos: int) -> int:
        byte, half = divmod(pos, 2)
        return (self._data[byte] >> 4) & 0xF if half == 0 else self._data[byte] & 0xF

    def _set(self, pos: int, val: int) -> None:
        val = max(0, min(15, val))
        byte, half = divmod(pos, 2)
        if half == 0:
            self._data[byte] = (val << 4) | (self._data[byte] & 0x0F)
        else:
            self._data[byte] = (self._data[byte] & 0xF0) | val

    def add(self, item: str) -> None:
        for pos in self._positions(item):
            self._set(pos, self._get(pos) + 1)

    def remove(self, item: str) -> None:
        for pos in self._positions(item):
            cnt = self._get(pos)
            if cnt > 0:
                self._set(pos, cnt - 1)

    def might_contain(self, item: str) -> bool:
        return all(self._get(pos) > 0 for pos in self._positions(item))

    def to_bytes(self) -> bytes:
        # Header: capacity, m, k — avoids floating-point round-trip issues
        return struct.pack(">III", self.capacity, self.m, self.k) + bytes(self._data)

    @classmethod
    def from_bytes(cls, data: bytes) -> CountingBloomFilter:
        capacity, m, k = struct.unpack(">III", data[:12])
        obj = cls.__new__(cls)
        obj.capacity   = capacity
        obj.m          = m
        obj.k          = k
        obj.error_rate = _ERROR_RATE
        obj._data      = bytearray(data[12:])
        return obj


def load_bloom(conn: sqlite3.Connection) -> CountingBloomFilter:
    row = conn.execute("SELECT bitarray FROM bloom_state WHERE id = 1").fetchone()
    if row:
        return CountingBloomFilter.from_bytes(bytes(row[0]))
    return CountingBloomFilter()


def save_bloom(conn: sqlite3.Connection, bf: CountingBloomFilter) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO bloom_state (id, bitarray) VALUES (1, ?)",
            (bf.to_bytes(),),
        )
