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

"""Pi coding-agent session history as per-event Sources with resumable capture."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, JsonValue

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import TEXT_EVIDENCE_PROJECTION_KEY, AdapterSourceDefinition, TextEvidence
from powercontext.sources.connectors import ConnectorRunCompletion, ConnectorRunSession, ConnectorRunStatus
from powercontext.sources.models import Source, SourceMaterialization

PI_EVENT_SOURCE_NAME = "pi-event"
PI_SESSION_CONNECTOR_NAME = "pi-session-history"
PI_DEFINITION_VERSION = "1"
PI_SESSION_HEADER_TYPE = "session"
PI_SESSION_VERSION = 3
CHECKPOINT_VERSION = 1
_IDENTITY_PREFIX = "pi"


class PiSessionInput(BaseModel):
    """One Pi session archive plus the leaf that selects the branch to capture."""

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """One visible event on the selected branch, retaining its complete record."""

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


class PiEventSource(Source):
    """A captured Pi event whose identity is derived from session and event."""

    source_id: str
    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("Pi session archive must be valid UTF-8") from error


def _load_records(text: str) -> list[dict[str, JsonValue]]:
    records: list[dict[str, JsonValue]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError("malformed Pi session record") from error
        if not isinstance(record, dict):
            raise ValueError("Pi session records must be JSON objects")
        records.append(record)
    return records


def _read_session_id(records: list[dict[str, JsonValue]]) -> str:
    if not records:
        raise ValueError("Pi session archive is empty")
    header = records[0]
    if header.get("type") != PI_SESSION_HEADER_TYPE:
        raise ValueError("first Pi session record must be the session header")
    if header.get("version") != PI_SESSION_VERSION:
        raise ValueError("unsupported Pi session version")
    session_id = header.get("id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("Pi session id must be a non-empty string")
    return session_id


def _block_text(blocks: object) -> str:
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def _message_body(message: dict[str, JsonValue]) -> tuple[str, str] | None:
    role = message.get("role")
    if not isinstance(role, str):
        return None
    content = message.get("content")
    if role in ("user", "assistant"):
        if isinstance(content, str):
            return role, content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind == "text":
                    text = block.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                elif kind == "toolCall":
                    name = block.get("name")
                    call_id = block.get("id")
                    arguments = block.get("arguments")
                    rendered = _compact_json(arguments if arguments is not None else {})
                    parts.append(f"[tool_call {name} {call_id}] {rendered}")
            return role, "\n".join(parts)
        return None
    if role == "toolResult":
        tool_name = message.get("toolName")
        tool_call_id = message.get("toolCallId")
        text = content if isinstance(content, str) else _block_text(content)
        return role, f"[tool_result {tool_name} {tool_call_id}] {text}"
    if role == "bashExecution":
        command = message.get("command")
        output = message.get("output")
        return role, "$ " + str(command if command is not None else "") + "\n" + str(output if output is not None else "")
    return None


def _render(record: dict[str, JsonValue]) -> tuple[str, str] | None:
    event_type = record.get("type")
    if event_type == "message":
        message = record.get("message")
        if isinstance(message, dict):
            return _message_body(message)
        return None
    if event_type in ("compaction", "branch_summary"):
        summary = record.get("summary")
        return (event_type, summary) if isinstance(summary, str) else None
    if event_type == "custom_message":
        content = record.get("content")
        if isinstance(content, str):
            return "custom", content
        return "custom", _block_text(content)
    return None


def pi_event_source_id(session_id: str, event_id: str) -> str:
    """Build a stable, collision-free identity bounded by the Source id limit."""
    identity = ":".join((_IDENTITY_PREFIX, quote(session_id, safe=""), quote(event_id, safe="")))
    if len(identity) <= MAX_SOURCE_ID_LENGTH:
        return identity
    digest = hashlib.sha256((session_id + "\x00" + event_id).encode()).hexdigest()
    return _IDENTITY_PREFIX + ":" + digest


def _branch(
    nodes: dict[str, dict[str, JsonValue]], leaf_id: str
) -> list[dict[str, JsonValue]]:
    chain: list[dict[str, JsonValue]] = []
    seen: set[str] = set()
    cursor: str | None = leaf_id
    while cursor is not None:
        if cursor in seen:
            raise ValueError("Pi session branch contains a cycle")
        seen.add(cursor)
        record = nodes.get(cursor)
        if record is None:
            raise ValueError("Pi session parent does not exist")
        chain.append(record)
        parent = record.get("parentId")
        cursor = parent if isinstance(parent, str) else None
    chain.reverse()
    return chain


def parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    """Parse the complete ancestor branch of the selected leaf into visible events."""
    records = _load_records(_decode(value.content))
    session_id = _read_session_id(records)

    nodes: dict[str, dict[str, JsonValue]] = {}
    order: list[str] = []
    for record in records[1:]:
        event_id = record.get("id")
        if not isinstance(event_id, str) or not event_id.strip():
            raise ValueError("Pi session record id must be a non-empty string")
        if event_id in nodes:
            raise ValueError("duplicate Pi session record id")
        parent_id = record.get("parentId")
        if parent_id is not None and not isinstance(parent_id, str):
            raise ValueError("Pi session parentId must be a string or null")
        nodes[event_id] = record
        order.append(event_id)
    for record in nodes.values():
        parent_id = record.get("parentId")
        if parent_id is not None and parent_id not in nodes:
            raise ValueError("Pi session parent does not exist")

    leaf_id = value.leaf_id if value.leaf_id is not None else (order[-1] if order else None)
    if leaf_id is None:
        return ()
    if leaf_id not in nodes:
        raise ValueError("Pi session leaf does not exist")

    events: list[PiEventInput] = []
    for record in _branch(nodes, leaf_id):
        rendered = _render(record)
        if rendered is None:
            continue
        role, body = rendered
        if not body.strip():
            continue
        events.append(
            PiEventInput(
                session_id=session_id,
                event_id=record["id"],
                parent_id=record.get("parentId"),
                event_type=str(record.get("type")),
                role=role,
                content=f"{role}: {body}",
                record=record,
            )
        )
    return tuple(events)


class PiEventSourceAdapter:
    """Resolve and read per-event Pi Sources."""

    input_class = PiEventInput
    name = PI_EVENT_SOURCE_NAME
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        source_id = pi_event_source_id(value.session_id, value.event_id)
        return PiEventSource(
            name=source_id,
            source_id=source_id,
            materialization=SourceMaterialization.CAPTURED,
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record=value.record,
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )


class PiEventTextEvidenceProjection:
    """Expose one captured Pi event as canonical textual evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PI_EVENT_SOURCE_NAME,
            source_id=source.source_id,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


PI_EVENT_SOURCE_ADAPTER = PiEventSourceAdapter()
PI_EVENT_SOURCE_DEFINITION = AdapterSourceDefinition(
    PI_EVENT_SOURCE_ADAPTER,
    version=PI_DEFINITION_VERSION,
    projections=(PiEventTextEvidenceProjection(),),
)


def _event_digest(event: PiEventInput) -> str:
    payload = json.dumps(event.record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _load_checkpoint(checkpoint: JsonValue | None) -> dict[str, str]:
    if checkpoint is None:
        return {}
    if not isinstance(checkpoint, dict):
        raise ValueError("Pi session checkpoint is malformed")
    events = checkpoint.get("events")
    if events is None:
        return {}
    if not isinstance(events, dict):
        raise ValueError("Pi session checkpoint is malformed")
    loaded: dict[str, str] = {}
    for event_id, digest in events.items():
        if not isinstance(event_id, str) or not isinstance(digest, str):
            raise ValueError("Pi session checkpoint is malformed")
        loaded[event_id] = digest
    return loaded


class PiSessionConnector:
    """Capture newly visible events of one Pi branch, resuming from the last checkpoint."""

    name = PI_SESSION_CONNECTOR_NAME
    version = PI_DEFINITION_VERSION
    source_definitions = frozenset((PI_EVENT_SOURCE_NAME,))

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self._path = Path(path)
        self._leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        imported = _load_checkpoint(session.checkpoint)
        try:
            content = self._path.read_bytes()
        except OSError as error:
            raise ValueError("Pi session file cannot be read") from error
        events = parse_pi_session(PiSessionInput(content=content, leaf_id=self._leaf_id))
        for event in events:
            digest = _event_digest(event)
            previous = imported.get(event.event_id)
            if previous is not None:
                if previous != digest:
                    raise ValueError("Pi event was rewritten after capture")
                continue
            await session.submit(event.event_id, PI_EVENT_SOURCE_NAME, event)
            imported[event.event_id] = digest
        return ConnectorRunCompletion(
            status=ConnectorRunStatus.COMPLETE,
            checkpoint={"version": CHECKPOINT_VERSION, "events": imported},
        )


__all__ = [
    "PI_EVENT_SOURCE_DEFINITION",
    "PI_EVENT_SOURCE_NAME",
    "PI_SESSION_CONNECTOR_NAME",
    "PiEventInput",
    "PiEventSource",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
    "pi_event_source_id",
]
