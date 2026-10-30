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

"""Durable Scope-local evidence graph for Source lifecycle propagation.

The graph and its applied-event log form one canonical document per Scope. The
document is replaced whole under a version check, so a retried propagation can
always be replayed against the last committed intent. Native Memory and Topic
availability stays in their owning projections; this store only records what
should be available and moves Topic governance alongside it.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.builtin.persistence.artifact_governance import (
    ArtifactGovernanceRepository,
    ArtifactLifecycleState,
)
from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.builtin.persistence.errors import StoredPayloadConflictError
from powercontext.builtin.persistence.supervision import database_utc_now
from powercontext.builtin.persistence.tables import SOURCE_LIFECYCLE_STATE_TABLE

LifecycleNodeKind = Literal["source", "memory", "topic"]
LifecyclePolicy = Literal["all", "any"]


class SourceLifecycleConflictError(RuntimeError):
    """Raised when the graph document changed after it was read."""


class SourceLifecycleNode(BaseModel):
    """One registered node: its reference, evidence parents, and availability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    kind: LifecycleNodeKind
    reference: dict[str, object]
    available: bool
    parents: tuple[str, ...] = ()
    policy: LifecyclePolicy | None = None
    # Only a Source carries a caller-supplied lifecycle version and the last
    # applied event sequence.
    revision: int | None = None
    sequence: int = 0

    def binding(self) -> tuple[object, ...]:
        """Return the parts a repeated registration must reproduce exactly."""

        return (self.kind, self.parents, self.policy, self.revision, canonical_reference(self.reference))


class SourceLifecycleEventRecord(BaseModel):
    """One applied Source event, retained so replays stay idempotent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    revision: int
    sequence: int
    available: bool
    reason: str


class SourceLifecycleState(BaseModel):
    """The whole Scope-local graph document at one committed version."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(ge=0)
    nodes: tuple[SourceLifecycleNode, ...] = ()
    events: tuple[SourceLifecycleEventRecord, ...] = ()

    def node(self, node_id: str) -> SourceLifecycleNode | None:
        return next((node for node in self.nodes if node.node_id == node_id), None)

    def active_ids(self) -> tuple[str, ...]:
        return tuple(sorted(node.node_id for node in self.nodes if node.available))

    def with_node(self, node: SourceLifecycleNode) -> SourceLifecycleState:
        """Replace a registered node in place, or append a newly registered one."""

        nodes = list(self.nodes)
        for index, item in enumerate(nodes):
            if item.node_id == node.node_id:
                nodes[index] = node
                return self.model_copy(update={"nodes": tuple(nodes)})
        return self.model_copy(update={"nodes": tuple(nodes) + (node,)})

    def with_availability(self, available: dict[str, bool]) -> SourceLifecycleState:
        return self.model_copy(
            update={
                "nodes": tuple(
                    node.model_copy(update={"available": available.get(node.node_id, node.available)})
                    for node in self.nodes
                )
            }
        )


def canonical_reference(reference: dict[str, object], /) -> str:
    """Return one order-independent key for a registered reference payload."""

    return json.dumps(reference, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class SourceLifecycleStore:
    """Read and replace one Scope's graph document; move Topic head governance."""

    def __init__(
        self,
        *,
        database: AsyncDatabase,
        scope_id: str,
        governance: ArtifactGovernanceRepository | None = None,
    ) -> None:
        self._database = database
        self._scope_id = scope_id
        self._governance = ArtifactGovernanceRepository() if governance is None else governance

    async def load(self) -> SourceLifecycleState | None:
        async with self._database.transaction() as connection:
            return await self._load(connection)

    async def _load(self, connection: AsyncConnection) -> SourceLifecycleState | None:
        row = (
            (
                await connection.execute(
                    select(
                        SOURCE_LIFECYCLE_STATE_TABLE.c.state_version,
                        SOURCE_LIFECYCLE_STATE_TABLE.c.payload,
                    ).where(SOURCE_LIFECYCLE_STATE_TABLE.c.scope_id == self._scope_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        state = SourceLifecycleState.model_validate_json(bytes(row["payload"]), strict=True)
        return state.model_copy(update={"version": int(row["state_version"])})

    async def save(self, state: SourceLifecycleState, /, *, expected_version: int) -> SourceLifecycleState:
        """Commit the document only if it still sits at ``expected_version``."""

        committed = state.model_copy(update={"version": expected_version + 1})
        payload = committed.model_dump_json().encode()
        try:
            async with self._database.transaction() as connection:
                now = await database_utc_now(connection)
                if expected_version == 0:
                    await connection.execute(
                        insert(SOURCE_LIFECYCLE_STATE_TABLE).values(
                            scope_id=self._scope_id,
                            state_version=committed.version,
                            payload=payload,
                            updated_at=now,
                        )
                    )
                else:
                    result = await connection.execute(
                        update(SOURCE_LIFECYCLE_STATE_TABLE)
                        .where(
                            SOURCE_LIFECYCLE_STATE_TABLE.c.scope_id == self._scope_id,
                            SOURCE_LIFECYCLE_STATE_TABLE.c.state_version == expected_version,
                        )
                        .values(
                            state_version=committed.version,
                            payload=payload,
                            updated_at=now,
                        )
                    )
                    if result.rowcount != 1:
                        raise SourceLifecycleConflictError(self._scope_id)
        except IntegrityError as error:
            # A concurrent first write already created the document.
            raise SourceLifecycleConflictError(self._scope_id) from error
        except StoredPayloadConflictError as error:
            raise SourceLifecycleConflictError(self._scope_id) from error
        return committed

    async def topic_lifecycle(self, artifact_id: str, /) -> ArtifactLifecycleState:
        async with self._database.transaction() as connection:
            governance = await self._governance.get(connection, self._scope_id, "topic-memory", artifact_id)
            return governance.lifecycle_state

    async def set_topic_available(self, artifact_id: str, /, *, available: bool) -> None:
        """Move a Topic head between the current working set and deprecated."""

        target = ArtifactLifecycleState.ACTIVE if available else ArtifactLifecycleState.DEPRECATED
        for _ in range(4):
            try:
                async with self._database.transaction() as connection:
                    current = await self._governance.get(connection, self._scope_id, "topic-memory", artifact_id)
                    if current.lifecycle_state is target:
                        return
                    await self._governance.transition(
                        connection,
                        self._scope_id,
                        "topic-memory",
                        artifact_id,
                        current.governance_generation,
                        target,
                        None,
                    )
                return
            except StoredPayloadConflictError:
                continue
        raise SourceLifecycleConflictError(self._scope_id)


__all__ = [
    "LifecycleNodeKind",
    "LifecyclePolicy",
    "SourceLifecycleConflictError",
    "SourceLifecycleEventRecord",
    "SourceLifecycleNode",
    "SourceLifecycleState",
    "SourceLifecycleStore",
    "canonical_reference",
]
