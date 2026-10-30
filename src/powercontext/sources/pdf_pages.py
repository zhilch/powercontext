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
from collections.abc import Sequence
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
    filler, and a filing phrases the same fact with different words than the
    caller does. Every stored page is therefore scored twice — once on the
    expanded question and once on the instruction-free question with repeated
    terms counted — by BM25 over the page body plus a stronger weight on the
    page's title region, and the two rankings are fused by reciprocal rank. Only
    the public Memory read path is used, and every citation is the exact entry
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
    analyzed = [_analyze(str(record.entry.text)) for record in records]
    titles = [_heading_text(_page_body(str(record.entry.text))) for record in records]
    fused: dict[int, float] = {}
    for terms, repetitions in readings:
        for rank, index in enumerate(_rank_pages(analyzed, terms, repetitions), start=1):
            if rank > _FUSION_DEPTH:
                break
            fused[index] = fused.get(index, 0.0) + 1.0 / (_FUSION_RRF_CONSTANT + rank)
    fused = _with_document_identity(question, records, fused)
    ordered = sorted(fused, key=lambda index: (-fused[index], index))
    ordered = _with_statement_pages(question, records, titles, ordered, limit)
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
# Generic filing vocabulary: a question speaks analyst language while a filing
# names line items and statements. One reading adds these, the other does not.
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
    "liquidity": ("balance", "current"),
    "liability": ("balance",),
    "liabilities": ("balance",),
}
_INSTRUCTION = re.compile(
    r"^\s*(?:respond|answer|give|assume|provide|please|use |using |rely|relying|calculate|compute|"
    r"state |if |round|show your|your answer|write )",
    re.IGNORECASE,
)
_BM25_K1 = 1.2
_BM25_B = 0.75
_REPETITION_WEIGHT = 0.5
_HEADING_WEIGHT = 2.0
_HEADING_LINES = 3
_FUSION_DEPTH = 100
_FUSION_RRF_CONSTANT = 60
_MAX_IDENTITY_LINE = 128
# A metric question is answered on a primary statement; the page itself rarely
# repeats the metric's name, so those pages are reserved inside the ranking.
_METRIC_WORDS = frozenset(
    """
    capital cost costs earnings ebitda equity expense expenses gross income liability liabilities liquidity margin
    margins net profit quick ratio ratios rate rates revenue revenues sales tax taxes working
    """.split()
)
# A primary statement owns its own page title; segment tables only embed the
# words "income statement data" in a caption, so the title must open with it.
_STATEMENT_TITLE = re.compile(
    r"^(?:condensed\s+)?(?:consolidated\s+)?(?:statements?\b|balance\s+sheets?\b)",
    re.IGNORECASE,
)
_STATEMENT_RESERVE = 5
_DOCUMENT_WEIGHT = 0.3
_NAME_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]*")


def _document_of(record: object) -> str:
    """Return the document identity line a page entry carries."""

    return str(record.entry.text).partition("\n")[0]


def _statement_indexes(
    titles: Sequence[str],
    documents: Sequence[str],
    document: str | None,
    limit: int,
) -> list[int]:
    """Return the first page of each primary financial statement.

    Only pages of ``document`` are considered: a scope may hold several filings,
    and another filing's statement must not take a slot from this question.
    """

    found: list[int] = []
    for index, title in enumerate(titles):
        if document is not None and documents[index] != document:
            continue
        if any(_STATEMENT_TITLE.match(line.strip()) for line in title.splitlines() if line.strip()):
            found.append(index)
        if len(found) >= limit:
            break
    return found


def _with_document_identity(
    question: str,
    records: Sequence[object],
    fused: dict[int, float],
) -> dict[int, float]:
    """Nudge pages whose document the question names.

    A scope may hold several filings, and only some of them are about the company
    the question names. The document identity line that every page carries is the
    only cross-document signal available; when the scope holds a single document
    the bonus is constant and the ranking is unchanged.
    """

    names: dict[int, frozenset[str]] = {}
    named = 0
    for index, record in enumerate(records):
        identity = str(record.entry.text).partition("\n")[0]
        tokens = frozenset(token for token in _NAME_TOKEN.findall(identity.lower()) if len(token) >= 3)
        names[index] = tokens
        if tokens:
            named += 1
    if named < 2 or len({names[index] for index in names}) < 2:
        return fused
    asked = frozenset(_TOKEN.findall(question.lower()))
    if not asked:
        return fused
    bonus = {index: _DOCUMENT_WEIGHT * len(tokens & asked) for index, tokens in names.items()}
    if not any(bonus.values()):
        return fused
    return {index: score + bonus[index] for index, score in fused.items()}


def _with_statement_pages(
    question: str,
    records: Sequence[object],
    titles: Sequence[str],
    ordered: Sequence[int],
    limit: int,
) -> list[int]:
    """Reserve result slots for the primary statements a metric question needs."""

    if not ordered or not (_METRIC_WORDS & set(_TOKEN.findall(question.lower()))):
        return list(ordered)
    documents = [_document_of(record) for record in records]
    reserved = _statement_indexes(titles, documents, documents[ordered[0]], _STATEMENT_RESERVE)
    if not reserved:
        return list(ordered)
    head = [index for index in ordered if index not in reserved][: max(limit - len(reserved), 0)]
    merged = head + [index for index in reserved if index not in head]
    return merged + [index for index in ordered if index not in merged]


def _analyze(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return the page body tokens and its title-region tokens."""

    body = _page_body(text)
    return tuple(_TOKEN.findall(body.lower())), tuple(_TOKEN.findall(_heading_text(body).lower()))


def _page_body(text: str) -> str:
    """Drop the document identity line so it cannot match every page equally.

    A page entry is stored as ``DOCUMENT_ID`` on its own line followed by the
    page text. That identity appears on every page of the document, so leaving it
    in the ranking text adds a term that cannot discriminate between pages while
    diluting the weights that can.
    """

    first, separator, rest = text.partition("\n")
    if separator and rest.strip() and len(first) <= _MAX_IDENTITY_LINE and " " not in first.strip():
        return rest
    return text


def _heading_text(text: str) -> str:
    """Return the leading lines plus the standalone uppercase headings of a page."""

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    picked = lines[:_HEADING_LINES]
    for line in lines:
        letters = [character for character in line if character.isalpha()]
        if not letters:
            continue
        uppercase = sum(1 for character in letters if character.isupper()) / len(letters)
        if uppercase >= 0.7 and len(line) <= 90:
            picked.append(line)
    return "\n".join(picked)


def _query_readings(question: str) -> tuple[tuple[tuple[str, ...], dict[str, float]], ...]:
    """Return the query readings whose rankings are fused for one question."""

    literal = _expanded_terms(question)
    if not literal:
        return ()
    stripped = _expanded_terms(question, strip_instructions=True)
    return (
        (literal, {term: 1.0 for term in literal}),
        (stripped, _repetition_weights(question)),
    )


def _question_terms(question: str, *, strip_instructions: bool = False) -> tuple[str, ...]:
    text = question
    if strip_instructions:
        sentences = re.split(r"(?<=[.!?])\s+", question)
        kept = [sentence for sentence in sentences if not _INSTRUCTION.match(sentence)]
        if kept:
            text = " ".join(kept)
    return tuple(dict.fromkeys(term for term in _TOKEN.findall(text.lower()) if term not in _STOPWORDS))


def _plural_variants(term: str) -> tuple[str, ...]:
    """Return the regular plural of a term so page headings match singular asks."""

    if term.isdigit() or len(term) < 3:
        return ()
    if term.endswith("y"):
        return (term[:-1] + "ies",)
    if term.endswith(("s", "x", "ch", "sh")):
        return (term + "es",)
    return (term + "s",)


def _expanded_terms(question: str, *, strip_instructions: bool = False) -> tuple[str, ...]:
    terms = _question_terms(question, strip_instructions=strip_instructions)
    extra: list[str] = []
    for term in terms:
        extra.extend(_EXPANSIONS.get(term, ()))
    for term in (*terms, *extra):
        extra.extend(_plural_variants(term))
    return tuple(dict.fromkeys((*terms, *extra)))


def _repetition_weights(question: str) -> dict[str, float]:
    counts = Counter(_question_terms(question))
    return {term: 1.0 + _REPETITION_WEIGHT * count for term, count in counts.items()}


async def _stored_pages(memory: object) -> tuple[object, ...]:
    """Read the current stored page entries through the public Memory read path."""

    page = await memory.list()
    return tuple(record for record in page.entries if record.entry.text.strip())


def _rank_pages(
    analyzed: Sequence[tuple[tuple[str, ...], tuple[str, ...]]],
    terms: tuple[str, ...],
    repetitions: dict[str, float],
) -> list[int]:
    """Rank page indexes by BM25 over the corpus body and title regions."""

    document_frequency: dict[str, int] = dict.fromkeys(terms, 0)
    for body, heading in analyzed:
        for term in set(body) | set(heading):
            if term in document_frequency:
                document_frequency[term] += 1
    total = len(analyzed)
    weights = {
        term: math.log(1.0 + (total - frequency + 0.5) / (frequency + 0.5)) * repetitions.get(term, 1.0)
        for term, frequency in document_frequency.items()
        if frequency
    }
    if not weights:
        return list(range(total))
    average_body = sum(len(body) for body, _heading in analyzed) / total
    average_heading = max(1.0, sum(len(heading) for _body, heading in analyzed) / total)
    scored: list[tuple[float, int]] = []
    for index, (body, heading) in enumerate(analyzed):
        score = _bm25(_counts(body, weights), weights, len(body), average_body)
        if heading:
            score += _HEADING_WEIGHT * _bm25(_counts(heading, weights), weights, len(heading), average_heading)
        scored.append((-score, index))
    scored.sort()
    return [index for _score, index in scored]


def _counts(tokens: Sequence[str], weights: dict[str, float]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for token in tokens:
        if token in weights:
            counts[token] = counts.get(token, 0) + 1
    return counts


def _bm25(counts: dict[str, int], weights: dict[str, float], length: int, average_length: float) -> float:
    if not counts:
        return 0.0
    normalization = _BM25_K1 * (1 - _BM25_B + _BM25_B * length / max(average_length, 1.0))
    return sum(
        weights[term] * count * (_BM25_K1 + 1) / (count + normalization) for term, count in counts.items()
    )

__all__ = [
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "parse_pdf_document",
    "pdf_page_source_id",
    "search_pdf_pages",
]
