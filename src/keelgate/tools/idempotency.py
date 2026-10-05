"""Idempotency for WRITE tools.

A key is claimed before the tool runs. Three outcomes matter beyond "new":

* **DONE** - the same key and the same arguments already ran; return the stored
  result instead of repeating the side effect.
* **CONFLICT** - the key was used with *different* arguments. Refused outright.
* **UNKNOWN** - a previous attempt may or may not have taken effect (it timed
  out, or its result could not be validated). It is never retried automatically:
  repeating a possibly-executed money movement is worse than stopping for a human.

This in-memory store is process-local. A deployment with several processes, or
one that must survive a restart, needs a shared durable implementation of the
same protocol; that is a known K1 gap.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class ClaimState(StrEnum):
    NEW = "NEW"
    DONE = "DONE"
    CONFLICT = "CONFLICT"
    UNKNOWN = "UNKNOWN"
    IN_FLIGHT = "IN_FLIGHT"


@dataclass(frozen=True)
class Claim:
    state: ClaimState
    output: dict[str, Any] | None = None


class IdempotencyStore(Protocol):
    def peek(self, tenant_id: str, tool: str, key: str, args_hash: str) -> Claim: ...

    def claim(self, tenant_id: str, tool: str, key: str, args_hash: str) -> Claim: ...

    def complete(self, tenant_id: str, tool: str, key: str, output: dict[str, Any]) -> None: ...

    def mark_unknown(self, tenant_id: str, tool: str, key: str) -> None: ...

    def release(self, tenant_id: str, tool: str, key: str) -> None: ...


@dataclass
class _Entry:
    args_hash: str
    state: ClaimState
    output: dict[str, Any] | None = None


class InMemoryIdempotencyStore:
    def __init__(self) -> None:
        self._entries: dict[tuple[str, str, str], _Entry] = {}
        self._lock = threading.Lock()

    def peek(self, tenant_id: str, tool: str, key: str, args_hash: str) -> Claim:
        """Read the state of a key without claiming it."""
        with self._lock:
            return self._classify(self._entries.get((tenant_id, tool, key)), args_hash)

    def claim(self, tenant_id: str, tool: str, key: str, args_hash: str) -> Claim:
        """Atomically claim a key for execution; NEW means the caller may run."""
        with self._lock:
            entry = self._entries.get((tenant_id, tool, key))
            if entry is None:
                self._entries[(tenant_id, tool, key)] = _Entry(args_hash, ClaimState.IN_FLIGHT)
                return Claim(ClaimState.NEW)
            return self._classify(entry, args_hash)

    @staticmethod
    def _classify(entry: _Entry | None, args_hash: str) -> Claim:
        if entry is None:
            return Claim(ClaimState.NEW)
        if entry.args_hash != args_hash:
            return Claim(ClaimState.CONFLICT)
        if entry.state is ClaimState.DONE:
            return Claim(ClaimState.DONE, entry.output)
        return Claim(entry.state)

    def complete(self, tenant_id: str, tool: str, key: str, output: dict[str, Any]) -> None:
        with self._lock:
            entry = self._entries[(tenant_id, tool, key)]
            entry.state = ClaimState.DONE
            entry.output = output

    def mark_unknown(self, tenant_id: str, tool: str, key: str) -> None:
        with self._lock:
            self._entries[(tenant_id, tool, key)].state = ClaimState.UNKNOWN

    def release(self, tenant_id: str, tool: str, key: str) -> None:
        """Forget a claim that provably did not execute, so a retry is allowed."""
        with self._lock:
            self._entries.pop((tenant_id, tool, key), None)
