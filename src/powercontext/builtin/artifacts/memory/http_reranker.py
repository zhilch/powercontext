# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""HTTP cross-encoder reranking with bounded batches and global ordering."""

from __future__ import annotations

import math
from typing import Any, Final

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision, _validate_bounds
from powercontext.builtin.inference.models import InferenceUsage

DEFAULT_HTTP_RERANK_POLICY_ID: Final = "http-cross-encoder-v1"
DEFAULT_HTTP_RERANK_BATCH_SIZE: Final = 32


def _reject_invalid_payload(message: str) -> None:
    raise ValueError(message)


def _parse_scores(payload: Any, expected: frozenset[int]) -> dict[int, float]:
    """Validate one response and map its ranks back to the global candidate pool."""
    if not isinstance(payload, dict) or set(payload) != {"scores"}:
        _reject_invalid_payload("rerank response must contain exactly the scores field")
    scores = payload["scores"]
    if not isinstance(scores, list):
        _reject_invalid_payload("rerank scores must be a list")
    collected: dict[int, float] = {}
    for item in scores:
        if not isinstance(item, dict) or set(item) != {"rank", "score"}:
            _reject_invalid_payload("each rerank score must contain exactly rank and score")
        rank = item["rank"]
        score = item["score"]
        if type(rank) is not int:
            _reject_invalid_payload("rerank rank must be an integer")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            _reject_invalid_payload("rerank score must be a finite number")
        if not math.isfinite(score):
            _reject_invalid_payload("rerank score must be finite")
        if rank not in expected:
            _reject_invalid_payload("rerank response contains a rank outside the request batch")
        if rank in collected:
            _reject_invalid_payload("rerank response contains a duplicate rank")
        collected[rank] = float(score)
    if set(collected) != set(expected):
        _reject_invalid_payload("rerank response must cover every requested rank exactly once")
    return collected


class HTTPMemoryReranker:
    """Score candidates through one HTTP service and select from the whole pool."""

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = DEFAULT_HTTP_RERANK_BATCH_SIZE,
        policy_id: str = DEFAULT_HTTP_RERANK_POLICY_ID,
    ) -> None:
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ValueError("rerank endpoint must be a non-empty string")
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("rerank batch_size must be a positive integer")
        if not isinstance(policy_id, str) or not policy_id.strip():
            raise ValueError("rerank policy_id must be a non-empty string")
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size
        self.policy_id = policy_id

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Score every candidate once, then select globally by score and original rank."""
        _validate_bounds(len(candidates), limit)
        scores: dict[int, float] = {}
        requests = 0
        for start in range(0, len(candidates), self._batch_size):
            batch = candidates[start : start + self._batch_size]
            ranks = tuple(range(start + 1, start + 1 + len(batch)))
            response = await self._client.post(
                self._endpoint,
                json={
                    "query": query,
                    "candidates": [
                        {"rank": rank, "text": candidate.text}
                        for rank, candidate in zip(ranks, batch, strict=True)
                    ],
                },
            )
            response.raise_for_status()
            requests += 1
            try:
                payload = response.json()
            except ValueError as error:
                raise ValueError("rerank response is not valid JSON") from error
            scores.update(_parse_scores(payload, frozenset(ranks)))
        selected = sorted(scores, key=lambda rank: (-scores[rank], rank))[:limit]
        return MemoryRerankDecision(
            selected_ranks=tuple(selected),
            usage=InferenceUsage(requests=requests),
        )


__all__ = ["DEFAULT_HTTP_RERANK_BATCH_SIZE", "DEFAULT_HTTP_RERANK_POLICY_ID", "HTTPMemoryReranker"]
