from __future__ import annotations

import hashlib
import re

from .cache import ResilientCache
from .contracts import PolicyChunk
from .providers import ModelProvider
from .repository import Repository


class RetrievalService:
    def __init__(
        self,
        repository: Repository,
        provider: ModelProvider,
        cache: ResilientCache,
        cache_ttl_seconds: int,
    ) -> None:
        self.repository = repository
        self.provider = provider
        self.cache = cache
        self.cache_ttl_seconds = cache_ttl_seconds

    @staticmethod
    def _terms(value: str) -> set[str]:
        return {
            token
            for token in re.findall(r"[a-z0-9]+", value.lower())
            if len(token) > 1
        }

    def _index_signature(self) -> str:
        version = getattr(
            self.repository,
            "policy_index_version",
            None,
        )

        if version:
            return str(version)

        chunks = getattr(self.repository, "chunks", [])
        metadata = "|".join(
            sorted(
                (
                    f"{chunk.document_id}:"
                    f"{chunk.version}:"
                    f"{chunk.effective_date.isoformat()}"
                )
                for chunk in chunks
            )
        )

        return hashlib.sha256(
            metadata.encode("utf-8")
        ).hexdigest()[:16]

    def _cache_key(self, query: str, top_k: int) -> str:
        normalized = " ".join(query.lower().split())
        material = (
            f"{self._index_signature()}|{top_k}|{normalized}"
        )
        digest = hashlib.sha256(
            material.encode("utf-8")
        ).hexdigest()

        return f"hr-policy-search:{digest}"

    def search(
        self,
        query: str,
        top_k: int = 3,
    ) -> list[PolicyChunk]:
        normalized_query = " ".join(query.split())

        if not normalized_query:
            return []

        top_k = max(1, min(top_k, 5))
        cache_key = self._cache_key(normalized_query, top_k)

        cached = self.cache.get(cache_key)

        if isinstance(cached, list):
            try:
                return [
                    PolicyChunk.model_validate(item)
                    for item in cached
                ]
            except (ValueError, TypeError):
                # Invalid cache content behaves like a cache miss.
                pass

        embeddings = self.provider.embed([normalized_query])

        if not embeddings or not embeddings[0]:
            return []

        candidate_limit = max(top_k * 3, 6)

        lexical_results = self.repository.lexical_search(
            normalized_query,
            candidate_limit,
        )
        vector_results = self.repository.vector_search(
            embeddings[0],
            candidate_limit,
        )

        candidates: dict[str, PolicyChunk] = {}
        rrf_scores: dict[str, float] = {}

        # Reciprocal-rank fusion gives both retrieval methods a vote.
        for rank, chunk in enumerate(lexical_results, start=1):
            candidates[chunk.chunk_id] = chunk
            rrf_scores[chunk.chunk_id] = (
                rrf_scores.get(chunk.chunk_id, 0.0)
                + 1.0 / (60 + rank)
            )

        for rank, chunk in enumerate(vector_results, start=1):
            candidates.setdefault(chunk.chunk_id, chunk)
            rrf_scores[chunk.chunk_id] = (
                rrf_scores.get(chunk.chunk_id, 0.0)
                + 1.0 / (60 + rank)
            )

        query_terms = self._terms(normalized_query)
        ranked: list[PolicyChunk] = []

        for chunk_id, chunk in candidates.items():
            searchable = " ".join(
                [
                    chunk.document_id,
                    chunk.title,
                    chunk.section,
                    chunk.content,
                ]
            )
            chunk_terms = self._terms(searchable)
            lexical_overlap = len(query_terms & chunk_terms)

            # Small keyword rerank after RRF. This is intentionally bounded
            # so vector and lexical retrieval both still contribute.
            final_score = (
                rrf_scores.get(chunk_id, 0.0)
                + lexical_overlap * 0.05
            )

            ranked.append(
                chunk.model_copy(
                    update={"score": final_score}
                )
            )

        ranked.sort(
            key=lambda chunk: (
                -chunk.score,
                chunk.document_id,
                chunk.chunk_id,
            )
        )

        results = ranked[:top_k]

        self.cache.set(
            cache_key,
            [
                item.model_dump(mode="json")
                for item in results
            ],
            self.cache_ttl_seconds,
        )

        return results