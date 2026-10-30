# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Propagate Source lifecycle events to the evidence that depends on them.

Each Scope's evidence dependencies form one directed acyclic graph: Sources are
roots, memories and Topic memories are derived nodes whose availability follows
their parents under an ``all`` or ``any`` policy. Withdrawal deactivates the
native Memory entry or Topic head; restoring the Source makes still-supported
content usable again. Bodies never change, so an exact citation keeps reading
the version recorded before the withdrawal.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, StrictStr, field_validator
from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.persistence.artifact_governance import ArtifactLifecycleState
from powercontext.builtin.persistence.source_lifecycle import (
    LifecyclePolicy,
    SourceLifecycleConflictError,
    SourceLifecycleEventRecord,
    SourceLifecycleNode,
    SourceLifecycleState,
    SourceLifecycleStore,
    canonical_reference,
)
from powercontext.builtin.runtime.models import (
    GetMemoryEntryRequest,
    MemoryEntryRecord,
    ReactivateMemoryEntryRequest,
    RetireMemoryEntryRequest,
)
from powercontext.builtin.sources import validate_scope_id
from powercontext.sources import SourceRef

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from powercontext.builtin.runtime.application import BuiltinRuntime

ResolutionPurpose = Literal["generation", "audit"]
LifecycleReference = SourceRef | MemoryCitation | ArtifactRef

_TOPIC_MEMORY_FAMILY = "topic-memory"


class SourceLifecycleError(ValueError):
    """Raised when a registration, event, or lookup violates the lifecycle contract."""

    def __init__(self, code: str) -> None:
        super().__init__(f"source lifecycle: {code}")
        self.code = code


class SourceLifecycleEvent(BaseModel):
    """One caller-supplied Source availability change at an exact version."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: StrictStr
    revision: StrictInt
    sequence: StrictInt
    available: StrictBool
    reason: StrictStr

    @field_validator("node_id", "reason")
    @classmethod
    def require_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be a non-blank string")  # noqa: TRY003
        return value

    @field_validator("revision", "sequence")
    @classmethod
    def require_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("must be a positive integer")  # noqa: TRY003
        return value


class SourceLifecycleApplication:
    """Select the Source lifecycle service for one Scope."""

    def __init__(self, runtime: BuiltinRuntime) -> None:
        self._runtime = runtime
        self._locks: dict[str, asyncio.Lock] = {}

    def for_scope(self, scope_id: str, /) -> SourceLifecycleService:
        scope = validate_scope_id(scope_id)
        return SourceLifecycleService(self._runtime, scope, lock=self._locks.setdefault(scope, asyncio.Lock()))


class SourceLifecycleService:
    """Register one Scope's evidence graph and propagate Source events over it."""

    def __init__(self, runtime: BuiltinRuntime, scope_id: str, *, lock: asyncio.Lock | None = None) -> None:
        self._runtime = runtime
        self.scope_id = scope_id
        self._lock = asyncio.Lock() if lock is None else lock
        self._store: SourceLifecycleStore | None = None
        self._state: SourceLifecycleState | None = None

    # -- registration ----------------------------------------------------

    async def register_source(self, node_id: str, source_ref: SourceRef, /, *, revision: int) -> None:
        """Register a captured Source root at one lifecycle revision."""

        node = _require_node_id(node_id)
        source_revision = _require_positive_int(revision, "revision")
        if not isinstance(source_ref, SourceRef):
            raise SourceLifecycleError("source-reference")
        await self._require_reference(lambda runtime, scope: _read_source(runtime, scope, source_ref))
        await self._register(
            node,
            kind="source",
            reference=source_ref,
            parents=(),
            policy=None,
            revision=source_revision,
        )

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        /,
        *,
        parents: tuple[str, ...],
        policy: LifecyclePolicy = "all",
    ) -> None:
        """Register a Memory entry that depends on already registered nodes."""

        node = _require_node_id(node_id)
        if not isinstance(citation, MemoryCitation):
            raise SourceLifecycleError("memory-citation")
        await self._require_reference(lambda runtime, scope: _read_memory(runtime, scope, citation))
        await self._register(
            node,
            kind="memory",
            reference=citation,
            parents=parents,
            policy=_require_policy(policy),
            revision=None,
        )

    async def register_topic(
        self,
        node_id: str,
        artifact_ref: ArtifactRef,
        /,
        *,
        parents: tuple[str, ...],
        policy: LifecyclePolicy = "all",
    ) -> None:
        """Register a Topic Memory Revision that depends on registered nodes."""

        node = _require_node_id(node_id)
        if not isinstance(artifact_ref, ArtifactRef) or artifact_ref.family != _TOPIC_MEMORY_FAMILY:
            raise SourceLifecycleError("topic-reference")
        await self._require_reference(lambda runtime, scope: _read_topic(runtime, scope, artifact_ref))
        await self._register(
            node,
            kind="topic",
            reference=artifact_ref,
            parents=parents,
            policy=_require_policy(policy),
            revision=None,
        )

    async def _register(
        self,
        node_id: str,
        *,
        kind: Literal["source", "memory", "topic"],
        reference: LifecycleReference,
        parents: tuple[str, ...],
        policy: LifecyclePolicy | None,
        revision: int | None,
    ) -> None:
        normalized = _require_parents(node_id, parents, derived=kind != "source")
        payload = reference.model_dump(mode="json")
        binding = (kind, normalized, policy, revision, canonical_reference(payload))

        async with self._lock:

            def mutate(current: SourceLifecycleState) -> SourceLifecycleState | None:
                existing = current.node(node_id)
                if existing is not None:
                    if existing.binding() == binding:
                        return None
                    raise SourceLifecycleError("registration-conflict")
                parent_available: list[bool] = []
                for parent_id in normalized:
                    parent = current.node(parent_id)
                    if parent is None:
                        raise SourceLifecycleError("missing-parent")
                    parent_available.append(parent.available)
                available = (
                    True
                    if kind == "source"
                    else (all(parent_available) if policy == "all" else any(parent_available))
                )
                updated = current.with_node(
                    SourceLifecycleNode(
                        node_id=node_id,
                        kind=kind,
                        reference=payload,
                        available=available,
                        parents=normalized,
                        policy=policy,
                        revision=revision,
                    )
                )
                return updated.with_availability(_derive(updated))

            committed = await self._mutate(mutate)
            await self._reconcile_native(committed)

    # -- events ----------------------------------------------------------

    async def apply(self, event: SourceLifecycleEvent, /) -> list[str]:
        """Apply one Source event and return every currently available node."""

        if not isinstance(event, SourceLifecycleEvent):
            event = SourceLifecycleEvent.model_validate(event)
        record = SourceLifecycleEventRecord(
            node_id=event.node_id,
            revision=event.revision,
            sequence=event.sequence,
            available=event.available,
            reason=event.reason,
        )
        async with self._lock:

            def mutate(current: SourceLifecycleState) -> SourceLifecycleState | None:
                node = current.node(record.node_id)
                if node is None or node.kind != "source":
                    raise SourceLifecycleError("unknown-source")
                if node.revision != record.revision:
                    raise SourceLifecycleError("revision-mismatch")
                if record.sequence < node.sequence:
                    raise SourceLifecycleError("sequence-order")
                if record.sequence == node.sequence:
                    applied = _last_event(current, record.node_id)
                    if applied is None:
                        raise SourceLifecycleError("sequence-order")
                    if (applied.revision, applied.available, applied.reason) != (
                        record.revision,
                        record.available,
                        record.reason,
                    ):
                        raise SourceLifecycleError("sequence-conflict")
                    # A replayed event still reconciles native state below, so a
                    # retry after an interrupted propagation completes it.
                    return None
                updated = current.with_node(
                    node.model_copy(update={"available": record.available, "sequence": record.sequence})
                )
                updated = updated.model_copy(update={"events": updated.events + (record,)})
                return updated.with_availability(_derive(updated))

            committed = await self._mutate(mutate)
            await self._reconcile_native(committed)
            return list(committed.active_ids())

    # -- resolution ------------------------------------------------------

    async def resolve(self, node_id: str, /, *, purpose: str = "generation") -> LifecycleReference:
        """Return the registered reference, or reject generation from an unavailable node."""

        if purpose not in {"generation", "audit"}:
            raise SourceLifecycleError("purpose")
        state = await self._loaded()
        node = state.node(_require_node_id(node_id))
        if node is None:
            raise SourceLifecycleError("unknown-node")
        if purpose == "generation" and not node.available:
            raise SourceLifecycleError("unavailable")
        return _reference_of(node)

    # -- storage ---------------------------------------------------------

    def _store_for_scope(self) -> SourceLifecycleStore:
        if self._store is None:
            self._store = self._runtime.source_lifecycle_store(self.scope_id)
        return self._store

    async def _loaded(self) -> SourceLifecycleState:
        if self._state is None:
            stored = await self._store_for_scope().load()
            self._state = SourceLifecycleState(version=0) if stored is None else stored
        return self._state

    async def _mutate(
        self,
        mutate: Callable[[SourceLifecycleState], SourceLifecycleState | None],
    ) -> SourceLifecycleState:
        for _ in range(4):
            current = await self._loaded()
            updated = mutate(current)
            if updated is None:
                return current
            try:
                committed = await self._store_for_scope().save(updated, expected_version=current.version)
            except SourceLifecycleConflictError:
                self._state = None
                continue
            self._state = committed
            return committed
        raise SourceLifecycleError("state-conflict")

    # -- native state ----------------------------------------------------

    async def _reconcile_native(self, state: SourceLifecycleState) -> None:
        await self._reconcile_memory(state)
        await self._reconcile_topics(state)

    async def _reconcile_memory(self, state: SourceLifecycleState) -> None:
        nodes = tuple(node for node in state.nodes if node.kind == "memory")
        if not nodes:
            return
        memory = self._runtime.memory.for_scope(self.scope_id)
        page = await memory.list(include_inactive=True)
        if page.memory_ref is None:
            raise SourceLifecycleError("memory-missing")
        head = page.memory_ref
        entries: dict[str, MemoryEntryRecord] = {entry.entry.entry_id: entry for entry in page.entries}
        for node in nodes:
            citation = _reference_of(node)
            if not isinstance(citation, MemoryCitation):
                raise SourceLifecycleError("memory-citation")
            record = entries.get(citation.entry_id)
            if record is None:
                raise SourceLifecycleError("memory-missing")
            if (record.state == "active") == node.available:
                continue
            current = MemoryCitation(
                memory_ref=head,
                entry_id=record.entry.entry_id,
                entry_version_id=record.entry.entry_version_id,
            )
            reason = f"source-lifecycle:{node.node_id}"
            if node.available:
                result = await memory.reactivate(ReactivateMemoryEntryRequest(citation=current, reason=reason))
            else:
                result = await memory.retire(RetireMemoryEntryRequest(citation=current, reason=reason))
            head = result.memory_ref
            if result.entry is not None:
                entries[record.entry.entry_id] = record.model_copy(
                    update={"memory_ref": head, "state": result.entry.state}
                )

    async def _reconcile_topics(self, state: SourceLifecycleState) -> None:
        for node in state.nodes:
            if node.kind != "topic":
                continue
            reference = _reference_of(node)
            if not isinstance(reference, ArtifactRef):
                raise SourceLifecycleError("topic-reference")
            target = ArtifactLifecycleState.ACTIVE if node.available else ArtifactLifecycleState.DEPRECATED
            store = self._store_for_scope()
            if await store.topic_lifecycle(reference.artifact_id) is target:
                continue
            await store.set_topic_available(reference.artifact_id, available=node.available)

    # -- reference validation -------------------------------------------

    async def _require_reference(self, reader: Callable[[BuiltinRuntime, str], Awaitable[None]]) -> None:
        try:
            await reader(self._runtime, self.scope_id)
        except Exception as error:  # noqa: BLE001 - a missing reference is a caller error
            raise SourceLifecycleError("missing-reference") from error


async def _read_source(runtime: BuiltinRuntime, scope_id: str, source_ref: SourceRef) -> None:
    await runtime.records.for_scope(scope_id).get_source(source_ref.source_type, source_ref.source_id)


async def _read_memory(runtime: BuiltinRuntime, scope_id: str, citation: MemoryCitation) -> None:
    await runtime.memory.for_scope(scope_id).get(GetMemoryEntryRequest(citation=citation))


async def _read_topic(runtime: BuiltinRuntime, scope_id: str, artifact_ref: ArtifactRef) -> None:
    await runtime.records.for_scope(scope_id).get_artifact_revision(
        artifact_ref.family,
        artifact_ref.artifact_id,
        artifact_ref.revision,
    )


def _reference_of(node: SourceLifecycleNode) -> LifecycleReference:
    if node.kind == "source":
        return SourceRef.model_validate(node.reference)
    if node.kind == "memory":
        return MemoryCitation.model_validate(node.reference)
    return ArtifactRef.model_validate(node.reference)


def _derive(state: SourceLifecycleState) -> dict[str, bool]:
    """Recompute availability for the whole DAG, independent of document order."""

    available: dict[str, bool] = {node.node_id: node.available for node in state.nodes if node.kind == "source"}
    remaining = [node for node in state.nodes if node.kind != "source"]
    while remaining:
        progressed = False
        for node in tuple(remaining):
            if any(parent not in available for parent in node.parents):
                continue
            support = [available[parent] for parent in node.parents]
            available[node.node_id] = all(support) if node.policy == "all" else any(support)
            remaining.remove(node)
            progressed = True
        if not progressed:
            raise SourceLifecycleError("missing-parent")
    return available


def _last_event(state: SourceLifecycleState, node_id: str) -> SourceLifecycleEventRecord | None:
    return next((event for event in reversed(state.events) if event.node_id == node_id), None)


def _require_node_id(node_id: object) -> str:
    if not isinstance(node_id, str) or not node_id.strip():
        raise SourceLifecycleError("node-id")
    return node_id


def _require_positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SourceLifecycleError(field)
    return value


def _require_policy(policy: object) -> LifecyclePolicy:
    if policy not in {"all", "any"}:
        raise SourceLifecycleError("policy")
    return policy  # type: ignore[return-value]


def _require_parents(node_id: str, parents: object, *, derived: bool) -> tuple[str, ...]:
    if not isinstance(parents, tuple | list):
        raise SourceLifecycleError("parents")
    normalized = tuple(_require_node_id(parent) for parent in parents)
    if len(set(normalized)) != len(normalized):
        raise SourceLifecycleError("duplicate-parent")
    if node_id in normalized:
        raise SourceLifecycleError("self-parent")
    if derived and not normalized:
        raise SourceLifecycleError("missing-parents")
    if not derived and normalized:
        raise SourceLifecycleError("source-parents")
    return normalized


__all__ = [
    "ResolutionPurpose",
    "SourceLifecycleApplication",
    "SourceLifecycleError",
    "SourceLifecycleEvent",
    "SourceLifecycleService",
]
