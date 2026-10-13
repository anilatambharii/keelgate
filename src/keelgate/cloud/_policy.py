"""A policy engine that evaluates a pack published in the control plane's registry.

The pack is **downloaded and evaluated in the caller's own process**. The decision never depends
on a network call, so a control-plane outage cannot turn into an allow (or into a hung agent), and
the version recorded with each decision is the hash of the exact sources that decided it.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from keelgate.policy import PolicyDecision, PolicyInput, RegoEngine, deny
from keelgate.policy._engine import hash_sources

if TYPE_CHECKING:
    from collections.abc import Mapping

    from keelgate.cloud._client import CloudClient

ENGINE_NAME: Final = "cloud-registry"


class PolicyPackError(Exception):
    """The registry returned a pack that is malformed or does not match its declared hash."""


class CloudPolicyEngine:
    """Fetch a tenant's deployed policy pack and decide with it, locally.

    Build it with :meth:`from_registry`. Call :meth:`refresh` to pick up a newly deployed version.
    Until a pack has been loaded successfully every decision is a DENY.
    """

    name = ENGINE_NAME

    def __init__(self, client: CloudClient, pack_name: str) -> None:
        self._client = client
        self._pack_name = pack_name
        self._engine: RegoEngine | None = None
        self.policy_version = "unavailable"

    @classmethod
    def from_registry(cls, client: CloudClient, pack_name: str) -> CloudPolicyEngine:
        """Load the active version of ``pack_name``; raises if it cannot be fetched or checked."""
        engine = cls(client, pack_name)
        engine.refresh()
        return engine

    def refresh(self) -> str:
        """Fetch the active version, verify its hash and switch to it. Returns the version."""
        bundle = self._client.request("GET", f"/v1/policies/{self._pack_name}/active")
        self._engine = self._load(bundle)
        self.policy_version = self._engine.policy_version
        return self.policy_version

    @staticmethod
    def _load(bundle: Mapping[str, Any]) -> RegoEngine:
        files = bundle.get("files")
        query = bundle.get("query")
        declared = bundle.get("source_hash")
        if not isinstance(files, dict) or not files or not isinstance(query, str):
            raise PolicyPackError("registry returned a malformed policy bundle")
        if declared != hash_sources({str(k): str(v) for k, v in files.items()}):
            raise PolicyPackError("policy bundle does not match its declared hash")
        with tempfile.TemporaryDirectory(prefix="keelgate-pack-") as tmp:
            for name, text in files.items():
                safe = Path(name).name  # a pack is flat; never trust a path from the network
                if not safe.endswith(".rego") or safe != name:
                    raise PolicyPackError("policy bundle contains an unexpected file name")
                (Path(tmp) / safe).write_text(str(text), encoding="utf-8")
            return RegoEngine(Path(tmp), query=query)

    async def decide(self, policy_input: PolicyInput) -> PolicyDecision:
        if self._engine is None:
            return deny("no policy pack is loaded", engine=self.name)
        return await self._engine.decide(policy_input)
