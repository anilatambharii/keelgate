"""OPA over HTTP: the default production policy backend.

Every decision is stamped with a hash of the policy sources *the server actually
has loaded*, read back from ``GET /v1/policies``, not with a hash of files on
the client's disk. So the audit log records which policy decided, even if the
server was reloaded underneath us.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import httpx

from keelgate.policy._engine import decision_from_result, deny, hash_sources

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from keelgate.policy._types import PolicyDecision, PolicyInput

DEFAULT_DECISION_PATH = "keelgate/finance_basic/decision"


class OpaHttpEngine:
    """Policy engine that asks an OPA server over HTTP (the production default).

    Fails closed: any error, timeout or malformed answer is a DENY.
    """

    name = "opa-http"

    def __init__(
        self,
        base_url: str,
        *,
        decision_path: str = DEFAULT_DECISION_PATH,
        pack_name: str = "finance_basic",
        timeout: float = 2.0,
        bearer_token: str | None = None,
        client: httpx.AsyncClient | None = None,
        version_ttl: float = 5.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._decision_path = decision_path.strip("/")
        self._pack_name = pack_name
        self._timeout = timeout
        self._version_ttl = version_ttl
        self._monotonic = monotonic
        headers = {"Authorization": f"Bearer {bearer_token}"} if bearer_token else {}
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(headers=headers, follow_redirects=False)
        self._version: tuple[str, float] | None = None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def policy_version(self) -> str:
        """Hash of the loaded pack sources, cached for ``version_ttl`` seconds."""
        now = self._monotonic()
        if self._version is not None and now - self._version[1] < self._version_ttl:
            return self._version[0]
        response = await self._client.get(f"{self._base_url}/v1/policies", timeout=self._timeout)
        response.raise_for_status()
        sources: dict[str, str] = {}
        for entry in response.json().get("result", []):
            policy_id = str(entry.get("id", ""))
            filename = policy_id.rsplit("/", 1)[-1]
            if (
                f"/{self._pack_name}/" in policy_id
                and filename.endswith(".rego")
                and not filename.endswith("_test.rego")
            ):
                sources[filename] = str(entry.get("raw", ""))
        if not sources:
            raise LookupError(f"OPA has no policies loaded for pack {self._pack_name!r}")
        version = hash_sources(sources)
        self._version = (version, now)
        return version

    async def decide(self, policy_input: PolicyInput) -> PolicyDecision:
        try:
            document = policy_input.to_document()
        except Exception as exc:  # NaN, non-JSON values, ...: refuse rather than guess
            return deny(f"policy input rejected: {type(exc).__name__}", engine=self.name)
        return await self.decide_document(document)

    async def decide_document(self, document: Mapping[str, Any]) -> PolicyDecision:
        """Evaluate a raw input document. Used by conformance tests and adapters."""
        try:
            response = await self._client.post(
                f"{self._base_url}/v1/data/{self._decision_path}",
                json={"input": dict(document)},
                timeout=self._timeout,
            )
            response.raise_for_status()
            body: Any = response.json()
            raw = body.get("result") if isinstance(body, dict) else None
            version = await self.policy_version()
        except Exception as exc:  # unreachable, slow, 5xx, bad JSON: all fail closed
            return deny(f"policy evaluation failed: {type(exc).__name__}", engine=self.name)
        return decision_from_result(raw, policy_version=version, engine=self.name)
