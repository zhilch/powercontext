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

"""Coalesce concurrent embedding work and cache successful vectors."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass

from powercontext.builtin.inference.models import EmbeddingResult, InferenceUsage

DEFAULT_MAX_BATCH_SIZE = 64
DEFAULT_COALESCE_MS = 5.0
DEFAULT_CACHE_SIZE = 1024


@dataclass(slots=True)
class _Batch:
    usage: InferenceUsage


class CoalescingEmbeddingModel:
    """Share in-flight embedding work across callers and reuse successful vectors."""

    def __init__(
        self,
        model: object,
        *,
        max_batch_size: int = DEFAULT_MAX_BATCH_SIZE,
        coalesce_ms: float = DEFAULT_COALESCE_MS,
        cache_size: int = DEFAULT_CACHE_SIZE,
    ) -> None:
        if type(max_batch_size) is not int or max_batch_size < 1:
            raise ValueError("max_batch_size must be a positive integer")
        if isinstance(coalesce_ms, bool) or not isinstance(coalesce_ms, (int, float)) or coalesce_ms < 0:
            raise ValueError("coalesce_ms must be a non-negative number")
        if type(cache_size) is not int or cache_size < 0:
            raise ValueError("cache_size must be a non-negative integer")
        self._model = model
        self._max_batch_size = max_batch_size
        self._coalesce_seconds = float(coalesce_ms) / 1000.0
        self._cache_size = cache_size
        self._cache: OrderedDict[str, tuple[float, ...]] = OrderedDict()
        self._inflight: dict[str, asyncio.Future[tuple[_Batch, tuple[float, ...]]]] = {}
        self._pending: list[str] = []
        self._flusher: asyncio.Task[None] | None = None

    @property
    def profile(self) -> object:
        return self._model.profile

    async def embed(self, texts: tuple[str, ...] | list[str]) -> EmbeddingResult:
        unique = list(dict.fromkeys(texts))
        if not unique:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0))

        futures: list[asyncio.Future[tuple[_Batch, tuple[float, ...]]]] = []
        for text in unique:
            cached = self._cache.get(text)
            if cached is not None:
                self._cache.move_to_end(text)
                future: asyncio.Future[tuple[_Batch, tuple[float, ...]]] = asyncio.get_running_loop().create_future()
                future.set_result((_Batch(InferenceUsage(requests=0)), cached))
                futures.append(future)
                continue
            future = self._inflight.get(text)
            if future is None:
                future = asyncio.get_running_loop().create_future()
                self._inflight[text] = future
                self._pending.append(text)
            futures.append(future)
        self._ensure_flusher()

        resolved: list[tuple[_Batch, tuple[float, ...]]] = []
        for future in futures:
            resolved.append(await asyncio.shield(future))

        by_text: dict[str, tuple[float, ...]] = {}
        charged: dict[int, InferenceUsage] = {}
        for text, (batch, vector) in zip(unique, resolved, strict=True):
            by_text[text] = vector
            charged[id(batch)] = batch.usage
        usage = _sum_usage(tuple(charged.values()))
        return EmbeddingResult(vectors=tuple(by_text[text] for text in texts), usage=usage)

    def _ensure_flusher(self) -> None:
        if self._flusher is None or self._flusher.done():
            self._flusher = asyncio.get_running_loop().create_task(self._drain())

    async def _drain(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._coalesce_seconds)
                pending = self._pending
                self._pending = []
                if not pending:
                    return
                for start in range(0, len(pending), self._max_batch_size):
                    await self._send(pending[start : start + self._max_batch_size])
                if not self._pending:
                    return
        finally:
            self._flusher = None

    async def _send(self, batch_texts: list[str]) -> None:
        batch = _Batch(InferenceUsage(requests=1))
        try:
            result = await self._model.embed(tuple(batch_texts))
        except asyncio.CancelledError:
            self._fail(batch_texts, asyncio.CancelledError())
            raise
        except Exception as error:  # noqa: BLE001
            self._fail(batch_texts, error)
            return
        vectors = tuple(result.vectors)
        if len(vectors) != len(batch_texts):
            self._fail(batch_texts, ValueError("embedding vector count does not match the batch"))
            return
        batch.usage = result.usage
        for text, vector in zip(batch_texts, vectors, strict=True):
            self._cache_put(text, tuple(vector))
            self._resolve(text, batch, tuple(vector))

    def _cache_put(self, text: str, vector: tuple[float, ...]) -> None:
        if self._cache_size == 0:
            return
        self._cache[text] = vector
        self._cache.move_to_end(text)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    def _resolve(self, text: str, batch: _Batch, vector: tuple[float, ...]) -> None:
        future = self._inflight.pop(text, None)
        if future is not None and not future.done():
            future.set_result((batch, vector))

    def _fail(self, batch_texts: list[str], error: BaseException) -> None:
        for text in batch_texts:
            future = self._inflight.pop(text, None)
            if future is not None and not future.done():
                future.set_exception(error)


def _sum_usage(usages: tuple[InferenceUsage, ...]) -> InferenceUsage:
    requests = sum(usage.requests for usage in usages)
    inputs = [usage.input_tokens for usage in usages if usage.input_tokens is not None]
    outputs = [usage.output_tokens for usage in usages if usage.output_tokens is not None]
    return InferenceUsage(
        requests=requests,
        input_tokens=sum(inputs) if inputs else None,
        output_tokens=sum(outputs) if outputs else None,
    )


__all__ = ["CoalescingEmbeddingModel"]
