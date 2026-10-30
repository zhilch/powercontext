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

"""Versioned context packs with prefix-reusing deltas over canonical wire bytes."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from typing import Any

PACK_SCHEMA = "powercontext.context-pack.v1"
DELTA_SCHEMA = "powercontext.context-pack-delta.v1"
DEFAULT_CHUNK_BYTES = 512

_PACK_FIELDS = frozenset(("schema", "items", "digest", "byte_count"))
_DELTA_FIELDS = frozenset(("schema", "base_digest", "target_digest", "prefix_items", "appended"))


class ContextPackError(ValueError):
    """Raised when a context pack or delta violates the wire contract."""


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _digest(items: tuple[bytes, ...]) -> str:
    unsigned = {
        "schema": PACK_SCHEMA,
        "items": [base64.b64encode(item).decode("ascii") for item in items],
    }
    return "sha256:" + hashlib.sha256(_canonical(unsigned)).hexdigest()


def _encode_items(items: tuple[bytes, ...]) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _decode_items(values: object) -> tuple[bytes, ...]:
    if not isinstance(values, list):
        raise ContextPackError("context pack items must be a list")
    decoded: list[bytes] = []
    for value in values:
        if not isinstance(value, str):
            raise ContextPackError("context pack items must be base64 strings")
        try:
            decoded.append(base64.b64decode(value, validate=True))
        except (binascii.Error, ValueError) as error:
            raise ContextPackError("context pack item is not valid base64") from error
    return tuple(decoded)


def _require_digest(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:") or len(value) != len("sha256:") + 64:
        raise ContextPackError("context pack digest is malformed")
    int(value[len("sha256:") :], 16)
    return value


def _chunk(content: bytes, limit: int) -> tuple[bytes, ...]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContextPackError("context content must be valid UTF-8") from error
    chunks: list[bytes] = []
    current = bytearray()
    for character in text:
        encoded = character.encode("utf-8")
        if len(current) + len(encoded) > limit:
            if not current:
                raise ContextPackError("chunk_bytes is too small for one UTF-8 character")
            chunks.append(bytes(current))
            current = bytearray()
        current.extend(encoded)
    if current:
        chunks.append(bytes(current))
    return tuple(chunks)


@dataclass(frozen=True, slots=True)
class ContextPack:
    """One complete, self-describing set of content chunks."""

    items: tuple[bytes, ...]

    @classmethod
    def build(cls, items: tuple[bytes, ...]) -> ContextPack:
        if not isinstance(items, tuple):
            items = tuple(items)
        for item in items:
            if not isinstance(item, bytes):
                raise ContextPackError("context pack items must be bytes")
        return cls(items)

    @classmethod
    def from_content(cls, content: str | bytes, *, chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> ContextPack:
        if type(chunk_bytes) is not int or chunk_bytes < 1:
            raise ContextPackError("chunk_bytes must be a positive integer")
        raw = content.encode("utf-8") if isinstance(content, str) else content
        if not isinstance(raw, bytes):
            raise ContextPackError("context content must be text or bytes")
        return cls(_chunk(raw, chunk_bytes))

    @property
    def digest(self) -> str:
        return _digest(self.items)

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        unsigned = {"schema": PACK_SCHEMA, "items": _encode_items(self.items), "digest": self.digest}
        size = len(_canonical({**unsigned, "byte_count": 0}))
        while True:
            payload = {**unsigned, "byte_count": size}
            encoded = _canonical(payload)
            if len(encoded) == size:
                return encoded
            size = len(encoded)

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPack:
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ContextPackError("context pack is not valid JSON") from error
        if not isinstance(payload, dict) or set(payload) != _PACK_FIELDS:
            raise ContextPackError("context pack must contain exactly schema, items, digest and byte_count")
        if payload["schema"] != PACK_SCHEMA:
            raise ContextPackError("context pack schema is not supported")
        items = _decode_items(payload["items"])
        if _require_digest(payload["digest"]) != _digest(items):
            raise ContextPackError("context pack digest does not match its items")
        if type(payload["byte_count"]) is not int or payload["byte_count"] != len(data):
            raise ContextPackError("context pack byte_count does not match the wire payload")
        return cls(items)


@dataclass(frozen=True, slots=True)
class ContextPackDelta:
    """A prefix-reusing delta from one pack to the next."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> ContextPackDelta:
        shared = 0
        limit = min(len(base.items), len(target.items))
        while shared < limit and base.items[shared] == target.items[shared]:
            shared += 1
        return cls(
            base_digest=base.digest,
            target_digest=target.digest,
            prefix_items=shared,
            appended=target.items[shared:],
        )

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        return _canonical(
            {
                "schema": DELTA_SCHEMA,
                "base_digest": self.base_digest,
                "target_digest": self.target_digest,
                "prefix_items": self.prefix_items,
                "appended": _encode_items(self.appended),
            }
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPackDelta:
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ContextPackError("context pack delta is not valid JSON") from error
        if not isinstance(payload, dict) or set(payload) != _DELTA_FIELDS:
            raise ContextPackError("context pack delta has an invalid field set")
        if payload["schema"] != DELTA_SCHEMA:
            raise ContextPackError("context pack delta schema is not supported")
        prefix = payload["prefix_items"]
        if type(prefix) is not int or prefix < 0:
            raise ContextPackError("context pack delta prefix_items must be a non-negative integer")
        return cls(
            base_digest=_require_digest(payload["base_digest"]),
            target_digest=_require_digest(payload["target_digest"]),
            prefix_items=prefix,
            appended=_decode_items(payload["appended"]),
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    """Rebuild the target pack from a base pack and one delta."""
    if base.digest != delta.base_digest:
        raise ContextPackError("context pack delta does not apply to this base pack")
    if delta.prefix_items > len(base.items):
        raise ContextPackError("context pack delta prefix exceeds the base pack")
    rebuilt = ContextPack.build(base.items[: delta.prefix_items] + delta.appended)
    if rebuilt.digest != delta.target_digest:
        raise ContextPackError("rebuilt context pack does not match the target digest")
    return rebuilt


__all__ = [
    "ContextPack",
    "ContextPackDelta",
    "ContextPackError",
    "apply_context_pack_delta",
];
