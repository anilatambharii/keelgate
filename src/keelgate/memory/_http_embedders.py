"""Real embedding models over plain ``httpx`` (no extra dependency).

:class:`~keelgate.memory._embedder.HashEmbedder` only matches shared words. These call an actual
embedding model so semantic memory can retrieve by meaning:

* :class:`OllamaEmbedder`: a local Ollama server (``/api/embed``).
* :class:`OpenAICompatibleEmbedder`: OpenAI, vLLM, LM Studio and any server that speaks the
  OpenAI ``/embeddings`` wire format.

The vector size is fixed by the memory backend (the pgvector column), so ``dim`` is required and
every response is checked against it. A mismatch is an error, never a silent truncation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from collections.abc import Sequence

HTTP_ERROR = 400


class EmbeddingError(Exception):
    """The embedding service failed or returned something unusable."""


def _check(vectors: Any, expected: int, dim: int) -> list[list[float]]:
    if not isinstance(vectors, list) or len(vectors) != expected:
        raise EmbeddingError("the service returned the wrong number of embeddings")
    out: list[list[float]] = []
    for vector in vectors:
        if (
            not isinstance(vector, list)
            or len(vector) != dim
            or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in vector)
        ):
            raise EmbeddingError(f"an embedding does not have the configured {dim} dimensions")
        out.append([float(x) for x in vector])
    return out


class _HttpEmbedder:
    def __init__(self, *, dim: int, client: httpx.Client | None, timeout: float) -> None:
        if dim <= 0:
            raise ValueError("dim must be positive")
        self.dim = dim
        self._client = client or httpx.Client(timeout=timeout)

    def _post(self, url: str, body: dict[str, Any], headers: dict[str, str]) -> Any:
        try:
            response = self._client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"embedding request failed: {type(exc).__name__}") from exc
        if response.status_code >= HTTP_ERROR:
            raise EmbeddingError(f"embedding service returned HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise EmbeddingError("the service returned invalid JSON") from exc

    def close(self) -> None:
        self._client.close()


class OllamaEmbedder(_HttpEmbedder):
    def __init__(
        self,
        *,
        model: str,
        dim: int,
        base_url: str = "http://localhost:11434",
        client: httpx.Client | None = None,
        timeout: float = 60.0,
    ) -> None:
        super().__init__(dim=dim, client=client, timeout=timeout)
        self._model = model
        self._url = f"{base_url.rstrip('/')}/api/embed"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        data = self._post(self._url, {"model": self._model, "input": list(texts)}, {})
        vectors = data.get("embeddings") if isinstance(data, dict) else None
        return _check(vectors, len(texts), self.dim)


class OpenAICompatibleEmbedder(_HttpEmbedder):
    def __init__(
        self,
        *,
        model: str,
        dim: int,
        base_url: str = "https://api.openai.com/v1",
        api_key: str | None = None,
        send_dimensions: bool = False,
        client: httpx.Client | None = None,
        timeout: float = 60.0,
    ) -> None:
        """``send_dimensions`` asks the model for ``dim`` outputs (OpenAI text-embedding-3 only)."""
        super().__init__(dim=dim, client=client, timeout=timeout)
        self._model = model
        self._send_dimensions = send_dimensions
        self._url = f"{base_url.rstrip('/')}/embeddings"
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        body: dict[str, Any] = {"model": self._model, "input": list(texts)}
        if self._send_dimensions:
            body["dimensions"] = self.dim
        data = self._post(self._url, body, self._headers)
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            raise EmbeddingError("the service returned no embedding data")
        ordered = sorted(items, key=lambda i: i.get("index", 0))
        return _check([i.get("embedding") for i in ordered], len(texts), self.dim)
