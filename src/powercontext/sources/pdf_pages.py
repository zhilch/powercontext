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

"""Physical PDF pages as captured Sources with a page-level evidence projection."""

from __future__ import annotations

import hashlib
import io
import math
import re
from collections import Counter
from urllib.parse import quote

from pydantic import BaseModel, Field
from pypdf import PdfReader

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import TEXT_EVIDENCE_PROJECTION_KEY, AdapterSourceDefinition, TextEvidence
from powercontext.sources.models import Source, SourceMaterialization

PDF_PAGE_SOURCE_NAME = "pdf-page"
PDF_DEFINITION_VERSION = "1"
_IDENTITY_PREFIX = "pdf"


class PDFDocumentInput(BaseModel):
    """Complete original PDF bytes for one document."""

    document_id: str
    content: bytes


class PDFPageInput(BaseModel):
    """One physical page of a parsed PDF document."""

    document_id: str
    document_sha256: str
    page_number: int = Field(ge=1)
    text: str


class PDFPageSource(Source):
    """A captured PDF page whose identity includes the document revision."""

    source_id: str
    document_id: str
    document_sha256: str
    page_number: int
    text: str


def _validate_document_id(document_id: str) -> str:
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError("document_id must be a non-empty string")
    return document_id


def pdf_page_source_id(document_id: str, document_sha256: str, page_number: int) -> str:
    """Build a stable, collision-free identity bounded by the Source id limit."""
    identity = ":".join((_IDENTITY_PREFIX, quote(document_id, safe=""), document_sha256, str(page_number)))
    if len(identity) <= MAX_SOURCE_ID_LENGTH:
        return identity
    payload = document_id + "|" + document_sha256 + "|" + str(page_number)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return _IDENTITY_PREFIX + ":" + digest


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Parse every physical page of the original PDF, preserving page order."""
    document_id = _validate_document_id(value.document_id)
    content = value.content
    if not isinstance(content, bytes) or not content:
        raise ValueError("PDF content must be non-empty bytes")
    digest = hashlib.sha256(content).hexdigest()
    try:
        reader = PdfReader(io.BytesIO(content), strict=False)
        if reader.is_encrypted:
            try:
                unlocked = reader.decrypt("")
            except Exception as error:
                raise ValueError("PDF requires a user password") from error
            if not unlocked:
                raise ValueError("PDF requires a user password")
        pages: list[PDFPageInput] = []
        for number, page in enumerate(reader.pages, start=1):
            try:
                text = page.extract_text() or ""
            except Exception as error:
                raise ValueError("PDF page text extraction failed") from error
            pages.append(
                PDFPageInput(
                    document_id=document_id,
                    document_sha256=digest,
                    page_number=number,
                    text=text,
                )
            )
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("PDF content cannot be parsed") from error
    if not pages:
        raise ValueError("PDF document has no pages")
    return tuple(pages)


class PDFPageSourceAdapter:
    """Resolve and read per-page PDF Sources."""

    input_class = PDFPageInput
    name = PDF_PAGE_SOURCE_NAME
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        source_id = pdf_page_source_id(value.document_id, value.document_sha256, value.page_number)
        return PDFPageSource(
            name=source_id,
            source_id=source_id,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            document_sha256=value.document_sha256,
            page_number=value.page_number,
            text=value.text,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


class PDFPageTextEvidenceProjection:
    """Expose one captured page as canonical textual evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PDF_PAGE_SOURCE_NAME,
            source_id=source.source_id,
            content=source.document_id + chr(10) + source.text,
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


PDF_PAGE_SOURCE_ADAPTER = PDFPageSourceAdapter()
PDF_PAGE_SOURCE_DEFINITION = AdapterSourceDefinition(
    PDF_PAGE_SOURCE_ADAPTER,
    version=PDF_DEFINITION_VERSION,
    projections=(PDFPageTextEvidenceProjection(),),
)


async def search_pdf_pages(memory: object, question: str, limit: int = 10) -> tuple[object, ...]:
    """Return up to the requested number of distinct page citations for one question.

    A natural-language question mixes the evidence request with instructions and
    filler, and filings phrase the same fact with different words than the caller
    does. Each stored page is therefore scored under several query readings —
    literal, domain-vocabulary expanded, and instruction-stripped with repeated
    terms counted — and the readings are fused by reciprocal rank. Only the
    public Memory read path is used, and every citation is the exact entry
    version the ranking read.
    """

    from powercontext.builtin.artifacts.memory.models import MemoryCitation

    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be a non-empty string")
    if type(limit) is not int or limit < 1:
        raise ValueError("limit must be a positive integer")
    readings = _query_readings(question)
    if not readings:
        return ()
    records = await _stored_pages(memory)
    if not records:
        return ()
    fused: dict[int, float] = {}
    for terms, repetitions in readings:
        for rank, index in enumerate(_rank_pages(records, terms, repetitions), start=1):
            if rank > _FUSION_DEPTH:
                break
            fused[index] = fused.get(index, 0.0) + 1.0 / (_FUSION_RRF_CONSTANT + rank)
    ordered = sorted(fused, key=lambda index: (-fused[index], index))
    return tuple(
        MemoryCitation(
            memory_ref=records[index].memory_ref,
            entry_id=records[index].entry.entry_id,
            entry_version_id=records[index].entry.entry_version_id,
        )
        for index in ordered[:limit]
    )


_TOKEN = re.compile(r"[a-z]+|\d+")
_STOPWORDS = frozenset(
    """
    about above after again against also am an and any are as at be because been before being below between both
    but by can could did do does doing down during each few for from further had has have having he her here hers
    herself him himself his how i if in into is it its itself just me more most my myself no nor not now of off on
    once only or other our ours ourselves out over own same she should so some such than that the their theirs them
    themselves then there these they this those through to too under until up very was we were what when where
    which while who whom why will with would you your yours yourself yourselves
    """.split()
)
# Generic filing vocabulary: questions speak analyst language, filings name line
# items. Expansions are added to one reading only, at a lower weight.
_EXPANSIONS = {
    "battles": ("litigation", "proceedings"),
    "battle": ("litigation", "proceedings"),
    "legal": ("litigation", "proceedings"),
    "geographies": ("geographic", "regions", "countries"),
    "geography": ("geographic", "regions"),
    "capex": ("expenditures", "property"),
    "employees": ("headcount", "workforce"),
    "revenue": ("sales",),
    "restructuring": ("severance", "charges"),
    "margin": ("income",),
}
_INSTRUCTION = re.compile(
    r"^\s*(?:respond|answer|give|assume|provide|please|use |using |rely|relying|calculate|compute|"
    r"state |if |round|show your|your answer|write )",
    re.IGNORECASE,
)
_BM25_K1 = 1.2
_BM25_B = 0.75
_EXPANSION_WEIGHT = 0.5
_REPETITION_WEIGHT = 0.5
_FUSION_DEPTH = 100
_FUSION_RRF_CONSTANT = 60


def _query_readings(question: str) -> tuple[tuple[tuple[str, ...], dict[str, float]], ...]:
    """Return the query readings whose rankings are fused for one question."""

    literal = _question_terms(question)
    if not literal:
        return ()
    return (
        (literal, {term: 1.0 for term in dict.fromkeys(literal)}),
        (_expanded_terms(question), _repetition_weights(question)),
        (_expanded_terms(question, strip_instructions=True), _repetition_weights(question, strip=True)),
    )


def _question_terms(question: str, *, strip_instructions: bool = False) -> tuple[str, ...]:
    text = question
    if strip_instructions:
        sentences = re.split(r"(?<=[.!?])\s+", question)
        kept = [sentence for sentence in sentences if not _INSTRUCTION.match(sentence)]
        if kept:
            text = " ".join(kept)
    return tuple(dict.fromkeys(term for term in _TOKEN.findall(text.lower()) if term not in _STOPWORDS))


def _expanded_terms(question: str, *, strip_instructions: bool = False) -> tuple[str, ...]:
    terms = _question_terms(question, strip_instructions=strip_instructions)
    extra: list[str] = []
    for term in terms:
        extra.extend(_EXPANSIONS.get(term, ()))
    return tuple(dict.fromkeys((*terms, *extra)))


def _repetition_weights(question: str, *, strip: bool = False) -> dict[str, float]:
    counts = Counter(_question_terms(question, strip_instructions=strip))
    return {term: 1.0 + _REPETITION_WEIGHT * count for term, count in counts.items()}


async def _stored_pages(memory: object) -> tuple[object, ...]:
    """Read the current stored page entries through the public Memory read path."""

    page = await memory.list()
    return tuple(record for record in page.entries if record.entry.text.strip())


def _rank_pages(
    records: tuple[object, ...],
    terms: tuple[str, ...],
    repetitions: dict[str, float],
) -> list[int]:
    """Rank page indexes by BM25 over the whole stored corpus."""

    analyzed = [_TOKEN.findall(str(record.entry.text).lower()) for record in records]
    document_frequency: dict[str, int] = dict.fromkeys(terms, 0)
    for tokens in analyzed:
        for term in set(tokens).intersection(document_frequency):
            document_frequency[term] += 1
    total = len(analyzed)
    weights = {
        term: math.log(1.0 + (total - frequency + 0.5) / (frequency + 0.5)) * repetitions.get(term, 1.0)
        for term, frequency in document_frequency.items()
        if frequency
    }
    if not weights:
        return list(range(len(analyzed)))
    average_length = sum(len(tokens) for tokens in analyzed) / total
    scored: list[tuple[float, int]] = []
    for index, tokens in enumerate(analyzed):
        counts: dict[str, int] = {}
        for token in tokens:
            if token in weights:
                counts[token] = counts.get(token, 0) + 1
        score = 0.0
        if counts:
            length = len(tokens)
            normalization = _BM25_K1 * (1 - _BM25_B + _BM25_B * length / average_length)
            score = sum(
                weights[term] * count * (_BM25_K1 + 1) / (count + normalization)
                for term, count in counts.items()
            )
        scored.append((-score, index))
    scored.sort()
    return [index for _score, index in scored]

__all__ = [
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "parse_pdf_document",
    "pdf_page_source_id",
    "search_pdf_pages",
]
