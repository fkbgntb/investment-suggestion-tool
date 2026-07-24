"""Fixed-domain official document collection with bounded PDF and HTML parsing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from io import BytesIO
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.collectors.safe_http import SafeHTTPClient, SafeHTTPResponse
from app.domain.base import IdempotencyKey, Identifier
from app.domain.collection import URLPolicy
from app.domain.documents import ExternalDocumentContent, RawDocument, RawDocumentControl
from app.domain.enums import ContentType, DocumentState
from app.domain.provenance import OriginProvenance

_DANGEROUS_PDF_MARKERS = (b"/JavaScript", b"/JS", b"/Launch", b"/EmbeddedFile")


class OfficialDocumentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    document_key: Identifier
    source_id: Identifier
    url: AnyHttpUrl
    title: str = Field(min_length=1, max_length=1_000)
    publisher: str = Field(min_length=1, max_length=300)
    language: str = Field(pattern=r"^[A-Za-z-]+$", min_length=2, max_length=16)
    content_type: ContentType
    published_at: datetime | None = None


@dataclass(frozen=True)
class ParsedOfficialDocument:
    body: str
    parser_name: str
    page_count: int | None = None
    text_extraction_empty: bool = False


class OfficialDocumentRejected(ValueError):
    """An official response exceeded parsing limits or contained active PDF features."""


class OfficialDocumentParser:
    def __init__(self, *, max_pages: int = 80, max_characters: int = 150_000) -> None:
        self.max_pages = max_pages
        self.max_characters = max_characters

    def parse(self, response: SafeHTTPResponse, *, fallback_title: str) -> ParsedOfficialDocument:
        if response.content_type == "application/pdf":
            return self._parse_pdf(response.body, fallback_title=fallback_title)
        if response.content_type == "text/html":
            body = response.body.decode("utf-8", errors="replace")
            if not body.strip():
                raise OfficialDocumentRejected("official HTML response is empty")
            return ParsedOfficialDocument(
                body=body[: self.max_characters],
                parser_name="bounded-html-v1",
            )
        raise OfficialDocumentRejected("unsupported official document content type")

    def _parse_pdf(self, body: bytes, *, fallback_title: str) -> ParsedOfficialDocument:
        if not body.startswith(b"%PDF-"):
            raise OfficialDocumentRejected("official PDF response has an invalid header")
        if any(marker in body for marker in _DANGEROUS_PDF_MARKERS):
            raise OfficialDocumentRejected("active or embedded PDF content is not accepted")
        try:
            reader = PdfReader(BytesIO(body), strict=True)
        except (PdfReadError, ValueError, TypeError) as error:
            raise OfficialDocumentRejected("official PDF could not be parsed") from error
        if reader.is_encrypted:
            raise OfficialDocumentRejected("encrypted official PDFs are not accepted")
        if len(reader.pages) > self.max_pages:
            raise OfficialDocumentRejected("official PDF exceeds the page limit")

        parts: list[str] = []
        remaining = self.max_characters
        try:
            for page in reader.pages:
                if remaining <= 0:
                    break
                text = (page.extract_text() or "").strip()
                if text:
                    parts.append(text[:remaining])
                    remaining -= len(parts[-1])
        except (KeyError, TypeError, ValueError) as error:
            raise OfficialDocumentRejected("official PDF text extraction failed") from error
        extracted = "\n".join(parts).strip()
        return ParsedOfficialDocument(
            body=extracted or fallback_title,
            parser_name="pypdf-6-bounded-v1",
            page_count=len(reader.pages),
            text_extraction_empty=not bool(extracted),
        )


class OfficialDocumentAdapter:
    adapter_name = "official-document"

    def __init__(
        self,
        http_client: SafeHTTPClient,
        *,
        parser: OfficialDocumentParser | None = None,
    ) -> None:
        self._http = http_client
        self._parser = parser or OfficialDocumentParser()

    async def fetch(
        self,
        spec: OfficialDocumentSpec,
        *,
        allowed_domains: tuple[str, ...],
        fetched_at: datetime,
    ) -> RawDocument:
        response = await self._http.fetch(
            str(spec.url),
            URLPolicy(
                source_id=spec.source_id,
                allowed_hosts=allowed_domains,
                allowed_content_types=("application/pdf", "text/html"),
                max_response_bytes=5_000_000,
                max_redirects=2,
                minimum_interval_seconds=1,
                connect_timeout_seconds=15,
                read_timeout_seconds=60,
                total_timeout_seconds=90,
            ),
        )
        parsed = self._parser.parse(response, fallback_title=spec.title)
        content_digest = sha256(response.body).hexdigest()
        final_domain = (urlsplit(response.final_url).hostname or "").casefold()
        provenance = OriginProvenance(
            discovery_source_id=spec.source_id,
            original_publisher=spec.publisher,
            original_domain=final_domain,
            original_url=response.final_url,
            verified_original=True,
            content_type=spec.content_type,
        )
        return RawDocument(
            external=ExternalDocumentContent(
                source_url=response.final_url,
                title=spec.title,
                body=parsed.body,
                published_at=spec.published_at,
                author=spec.publisher,
                language=spec.language,
                metadata={
                    "origin_provenance": provenance.model_dump(mode="json"),
                    "content_type": spec.content_type.value,
                    "parser_name": parsed.parser_name,
                    "page_count": parsed.page_count,
                    "text_extraction_empty": parsed.text_extraction_empty,
                    "official_original_verified": True,
                },
            ),
            control=RawDocumentControl(
                document_id=f"official-{spec.document_key}-{content_digest[:20]}",
                source_id=spec.source_id,
                state=DocumentState.FETCHED,
                state_version=1,
                content_sha256=content_digest,
                idempotency=IdempotencyKey(
                    scope="official-document",
                    key=f"{spec.document_key}-{content_digest[:20]}",
                    payload_sha256=content_digest,
                ),
                discovered_at=fetched_at,
                fetched_at=fetched_at,
            ),
        )
