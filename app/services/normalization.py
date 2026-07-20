"""Deterministic isolation, normalization, exact deduplication, and event clustering."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta
from hashlib import sha256
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.documents import EventCluster, NormalizedDocument
from app.domain.enums import DocumentState
from app.storage.models import EventClusterRow, NormalizedDocumentRow, RawDocumentRow

_SKIPPED_TAGS = {"script", "style", "noscript", "svg", "template", "nav"}
_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "source",
    "track",
    "wbr",
}
_TRACKING_PARAMETERS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "source"}
_WHITESPACE = re.compile(r"\s+")


class LanguageDetector(Protocol):
    detector_name: str

    def detect(self, text: str, declared_language: str) -> str: ...


class HeuristicLanguageDetector:
    detector_name = "unicode-script-v1"

    def detect(self, text: str, declared_language: str) -> str:
        sample = text[:10_000]
        letters = sum(character.isalpha() for character in sample)
        cjk = sum("\u3400" <= character <= "\u9fff" for character in sample)
        if letters and cjk / letters >= 0.2:
            return "zh"
        declared = declared_language.casefold().split("-", 1)[0]
        if declared in {"en", "zh", "ja", "ko", "de", "fr", "es", "pt", "ru", "ar"}:
            return declared
        return "en" if any("a" <= character.casefold() <= "z" for character in sample) else "und"


class _SafeTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipped_depth = 0
        self.open_tags: list[tuple[str, bool]] = []
        self.suspicious_flags: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.casefold()
        attributes = {key.casefold(): value for key, value in attrs}
        style = (attributes.get("style") or "").replace(" ", "").casefold()
        hidden = "hidden" in attributes or any(
            marker in style for marker in ("display:none", "visibility:hidden", "opacity:0")
        )
        skipped = normalized in _SKIPPED_TAGS or hidden
        if skipped:
            self.suspicious_flags.add(
                f"removed_{normalized}" if normalized in _SKIPPED_TAGS else "removed_hidden_text"
            )
        if normalized not in _VOID_TAGS:
            if skipped:
                self.skipped_depth += 1
            self.open_tags.append((normalized, skipped))
        if normalized in {"p", "div", "br", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.casefold()
        for index in range(len(self.open_tags) - 1, -1, -1):
            if self.open_tags[index][0] != normalized:
                continue
            closed = self.open_tags[index:]
            del self.open_tags[index:]
            self.skipped_depth = max(0, self.skipped_depth - sum(skipped for _, skipped in closed))
            break

    def handle_data(self, data: str) -> None:
        if not self.skipped_depth:
            self.parts.append(data)


def normalize_text(value: str, *, maximum: int) -> tuple[str, tuple[str, ...]]:
    parser = _SafeTextExtractor()
    parser.feed(value)
    parser.close()
    joined = "".join(parser.parts)
    normalized = unicodedata.normalize("NFKC", joined)
    controls = sum(
        unicodedata.category(character) in {"Cc", "Cf"} and character not in "\n\t"
        for character in normalized
    )
    if controls:
        parser.suspicious_flags.add("removed_control_characters")
    normalized = "".join(
        character
        for character in normalized
        if unicodedata.category(character) not in {"Cc", "Cf"} or character in "\n\t"
    )
    normalized = _WHITESPACE.sub(" ", normalized).strip()
    if len(normalized) > maximum:
        normalized = normalized[:maximum].rstrip()
        parser.suspicious_flags.add("content_truncated")
    return normalized, tuple(sorted(parser.suspicious_flags))


def canonicalize_url(value: str) -> str:
    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").rstrip(".").encode("idna").decode("ascii").casefold()
    port = parsed.port
    default_port = (parsed.scheme.casefold() == "https" and port == 443) or (
        parsed.scheme.casefold() == "http" and port == 80
    )
    netloc = hostname if port is None or default_port else f"{hostname}:{port}"
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in _TRACKING_PARAMETERS
    ]
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            netloc,
            parsed.path or "/",
            urlencode(sorted(query)),
            "",
        )
    )


def _similarity_signature(title: str, summary: str | None) -> set[str]:
    text = unicodedata.normalize("NFKC", f"{title} {summary or ''}").casefold()
    compact = "".join(character for character in text if character.isalnum())
    return {compact[index : index + 3] for index in range(max(0, len(compact) - 2))}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


class NormalizationService:
    def __init__(
        self,
        session: Session,
        workspace_id: str,
        detector: LanguageDetector | None = None,
    ) -> None:
        self.session = session
        self.workspace_id = workspace_id
        self.detector = detector or HeuristicLanguageDetector()

    def process_pending(self, *, now: datetime, limit: int = 500) -> tuple[int, int, int]:
        rows = self.session.scalars(
            select(RawDocumentRow)
            .outerjoin(
                NormalizedDocumentRow,
                (NormalizedDocumentRow.workspace_id == RawDocumentRow.workspace_id)
                & (NormalizedDocumentRow.document_id == RawDocumentRow.document_id),
            )
            .where(
                RawDocumentRow.workspace_id == self.workspace_id,
                RawDocumentRow.state.in_(
                    [DocumentState.DISCOVERED.value, DocumentState.FETCHED.value]
                ),
                NormalizedDocumentRow.document_id.is_(None),
            )
            .order_by(RawDocumentRow.fetched_at)
            .limit(limit)
        ).all()
        processed = duplicates = quarantined = 0
        for row in rows:
            try:
                normalized = self._normalize(row, now=now)
                duplicate = self._find_exact_duplicate(normalized)
                if duplicate is not None:
                    normalized = normalized.model_copy(
                        update={"duplicate_of_document_id": duplicate.document_id}
                    )
                    duplicates += 1
                self._persist(normalized)
                self._cluster(normalized)
                row.state = (
                    DocumentState.DEDUPLICATED.value
                    if duplicate is not None
                    else DocumentState.NORMALIZED.value
                )
                row.state_version += 1
                row.updated_at = now
                processed += 1
            except (ValueError, UnicodeError):
                row.state = DocumentState.QUARANTINED.value
                row.state_version += 1
                row.metadata_payload = {
                    **row.metadata_payload,
                    "normalization_error_code": "INVALID_OR_EMPTY_CONTENT",
                }
                row.updated_at = now
                quarantined += 1
        self.session.flush()
        return processed, duplicates, quarantined

    def _normalize(self, row: RawDocumentRow, *, now: datetime) -> NormalizedDocument:
        if row.raw_body is None:
            raise ValueError("raw body is unavailable")
        title, title_flags = normalize_text(row.title, maximum=1_000)
        body, body_flags = normalize_text(row.raw_body, maximum=100_000)
        if not title or not body:
            raise ValueError("normalized content is empty")
        summary_value = row.metadata_payload.get("summary")
        summary = None
        summary_flags: tuple[str, ...] = ()
        if isinstance(summary_value, str):
            summary, summary_flags = normalize_text(summary_value, maximum=20_000)
        original_hash = sha256(row.raw_body.encode("utf-8")).hexdigest()
        normalized_hash = sha256(f"{title}\n{body}".encode()).hexdigest()
        original_language = str(row.metadata_payload.get("language") or "und")[:32]
        return NormalizedDocument(
            document_id=row.document_id,
            source_id=row.source_id,
            canonical_url=canonicalize_url(row.source_url),
            title=title,
            body=body,
            summary=summary,
            original_language=original_language,
            detected_language=self.detector.detect(f"{title}\n{body}", original_language),
            original_sha256=original_hash,
            normalized_sha256=normalized_hash,
            suspicious_flags=tuple(sorted(set((*title_flags, *body_flags, *summary_flags)))),
            published_at=row.published_at,
            discovered_at=row.fetched_at,
            normalized_at=now,
        )

    def _find_exact_duplicate(self, document: NormalizedDocument) -> NormalizedDocument | None:
        row = self.session.scalar(
            select(NormalizedDocumentRow)
            .where(
                NormalizedDocumentRow.workspace_id == self.workspace_id,
                NormalizedDocumentRow.document_id != document.document_id,
                (NormalizedDocumentRow.canonical_url == str(document.canonical_url))
                | (NormalizedDocumentRow.normalized_hash == document.normalized_sha256),
            )
            .order_by(NormalizedDocumentRow.created_at)
        )
        return NormalizedDocument.model_validate(row.payload) if row is not None else None

    def _persist(self, document: NormalizedDocument) -> None:
        self.session.add(
            NormalizedDocumentRow(
                normalized_document_id=f"normalized-{document.document_id}",
                workspace_id=self.workspace_id,
                document_id=document.document_id,
                source_id=document.source_id,
                canonical_url=str(document.canonical_url),
                normalized_title=document.title,
                normalized_body=document.body,
                original_hash=document.original_sha256,
                normalized_hash=document.normalized_sha256,
                duplicate_of_document_id=document.duplicate_of_document_id,
                normalized_at=document.normalized_at,
                schema_version=document.schema_version,
                payload=document.model_dump(mode="json"),
            )
        )
        self.session.flush()

    def _cluster(self, document: NormalizedDocument) -> EventCluster:
        signature = _similarity_signature(document.title, document.summary)
        candidates = self.session.scalars(
            select(EventClusterRow).where(
                EventClusterRow.workspace_id == self.workspace_id,
                EventClusterRow.updated_at >= document.normalized_at - timedelta(hours=72),
            )
        ).all()
        chosen: EventClusterRow | None = None
        for candidate in candidates:
            stored = candidate.payload.get("similarity_signature", [])
            if isinstance(stored, list) and _jaccard(signature, set(map(str, stored))) >= 0.75:
                chosen = candidate
                break
        if chosen is None:
            event_key = sha256("|".join(sorted(signature)).encode()).hexdigest()[:48]
            cluster = EventCluster(
                cluster_id=str(uuid5(NAMESPACE_URL, f"{self.workspace_id}:event:{event_key}")),
                topic_ids=("unclassified",),
                document_ids=(document.document_id,),
                canonical_document_id=document.document_id,
                event_key=event_key,
                first_seen_at=document.discovered_at,
                last_seen_at=document.discovered_at,
            )
            payload = cluster.model_dump(mode="json")
            payload["similarity_signature"] = sorted(signature)
            self.session.add(
                EventClusterRow(
                    cluster_id=cluster.cluster_id,
                    workspace_id=self.workspace_id,
                    event_key=cluster.event_key,
                    canonical_document_id=cluster.canonical_document_id,
                    schema_version=cluster.schema_version,
                    payload=payload,
                )
            )
            return cluster
        current = EventCluster.model_validate(
            {key: value for key, value in chosen.payload.items() if key != "similarity_signature"}
        )
        if document.document_id in current.document_ids:
            return current
        updated = current.model_copy(
            update={
                "document_ids": (*current.document_ids, document.document_id),
                "first_seen_at": min(current.first_seen_at, document.discovered_at),
                "last_seen_at": max(current.last_seen_at, document.discovered_at),
            }
        )
        signature_value = chosen.payload.get("similarity_signature", [])
        chosen.payload = {
            **updated.model_dump(mode="json"),
            "similarity_signature": signature_value,
        }
        chosen.updated_at = document.normalized_at
        return updated
