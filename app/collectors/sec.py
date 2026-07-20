"""SEC EDGAR submissions adapter with declared identity and strict host limits."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, time, timedelta, timezone
from hashlib import sha256

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.collectors.safe_http import SafeHTTPClient
from app.domain.base import IdempotencyKey, Identifier
from app.domain.collection import URLPolicy
from app.domain.contracts import (
    SourceDiscoveryRequest,
    SourceDiscoveryResult,
    SourceFetchRequest,
    SourceFetchResult,
)
from app.domain.documents import (
    DiscoveredDocument,
    ExternalDocumentContent,
    RawDocument,
    RawDocumentControl,
)
from app.domain.enums import DocumentState

_ACCESSION = re.compile(r"^\d{10}-\d{2}-\d{6}$")
_FORM = re.compile(r"^[A-Z0-9-]+(?:/A)?$")
_PRIMARY_DOCUMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_SEC_SYSTEM_TIME = timezone(timedelta(hours=-5), name="SEC-EST")


class SECConfigurationError(ValueError):
    pass


class SECResponseError(ValueError):
    pass


class SECCompany(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    entity_id: Identifier
    cik: str = Field(pattern=r"^\d{10}$")
    forms: tuple[str, ...] = Field(min_length=1, max_length=50)

    @field_validator("forms")
    @classmethod
    def normalize_forms(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip().upper() for value in values)
        if any(not _FORM.fullmatch(value) for value in normalized):
            raise ValueError("SEC form names are invalid")
        if len(set(normalized)) != len(normalized):
            raise ValueError("SEC form names must be unique")
        return normalized


class SECAdapter:
    adapter_name = "sec-submissions"

    def __init__(
        self,
        http_client: SafeHTTPClient,
        companies: tuple[SECCompany, ...],
        *,
        contact_email: str,
        max_filings_per_company: int = 50,
    ) -> None:
        if not companies:
            raise SECConfigurationError("at least one SEC company is required")
        if max_filings_per_company < 1 or max_filings_per_company > 250:
            raise SECConfigurationError("SEC filing limit must be between 1 and 250")
        email = contact_email.strip().casefold()
        if email.count("@") != 1 or "." not in email.rsplit("@", 1)[1]:
            raise SECConfigurationError("a valid SEC contact email is required")
        if len({company.cik for company in companies}) != len(companies):
            raise SECConfigurationError("SEC company CIKs must be unique")
        self._http = http_client
        self._companies = companies
        self._user_agent = f"investment-suggestion-tool/0.1 {email}"
        self._max_filings = max_filings_per_company
        self.last_result_count = 0

    async def discover(self, request: SourceDiscoveryRequest) -> SourceDiscoveryResult:
        effective_since = request.since.astimezone(UTC)
        if request.cursor is not None:
            try:
                cursor = datetime.fromisoformat(request.cursor.replace("Z", "+00:00"))
            except ValueError as error:
                raise SECConfigurationError("SEC cursor must be an ISO-8601 timestamp") from error
            if cursor.tzinfo is None:
                raise SECConfigurationError("SEC cursor must include a timezone")
            effective_since = max(effective_since, cursor.astimezone(UTC))
        if effective_since > request.until.astimezone(UTC):
            raise SECConfigurationError("SEC cursor cannot follow the request end time")
        documents: list[DiscoveredDocument] = []
        for company in self._companies:
            url = f"https://data.sec.gov/submissions/CIK{company.cik}.json"
            response = await self._http.fetch(url, self.metadata_policy(request.source_id))
            documents.extend(
                self.parse_submissions(
                    response.body,
                    source_id=request.source_id,
                    company=company,
                    since=effective_since,
                    until=request.until,
                    discovered_at=request.until,
                    maximum=self._max_filings,
                )
            )
        documents.sort(key=lambda value: value.published_at or value.discovered_at)
        self.last_result_count = len(documents)
        cursor = (
            max(
                (item.published_at for item in documents if item.published_at is not None),
                default=None,
            )
            or request.until
        )
        return SourceDiscoveryResult(
            documents=tuple(documents),
            next_cursor=cursor.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        )

    async def fetch(self, request: SourceFetchRequest) -> SourceFetchResult:
        response = await self._http.fetch(
            str(request.source_url),
            self.document_policy(request.source_id),
        )
        body = response.body.decode("utf-8", errors="replace")
        content = ExternalDocumentContent(
            source_url=response.final_url,
            title="SEC filing document",
            body=body,
            language="en",
            metadata={
                "content_type": response.content_type,
                "official_sec_document": True,
                "untrusted_filing_text": True,
            },
        )
        return SourceFetchResult(
            source_id=request.source_id,
            content=content,
            content_sha256=sha256(response.body).hexdigest(),
            fetched_at=datetime.now(UTC),
        )

    def metadata_policy(self, source_id: str) -> URLPolicy:
        return URLPolicy(
            source_id=source_id,
            allowed_hosts=("data.sec.gov",),
            allowed_content_types=("application/json",),
            max_response_bytes=5_000_000,
            minimum_interval_seconds=0.25,
            user_agent=self._user_agent,
        )

    def document_policy(self, source_id: str) -> URLPolicy:
        return URLPolicy(
            source_id=source_id,
            allowed_hosts=("www.sec.gov",),
            allowed_content_types=("text/html", "text/plain", "application/xhtml+xml"),
            max_response_bytes=5_000_000,
            minimum_interval_seconds=0.25,
            user_agent=self._user_agent,
        )

    @classmethod
    def parse_submissions(
        cls,
        body: bytes,
        *,
        source_id: str,
        company: SECCompany,
        since: datetime,
        until: datetime,
        discovered_at: datetime,
        maximum: int,
    ) -> tuple[DiscoveredDocument, ...]:
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SECResponseError("SEC submissions response is not valid JSON") from error
        if not isinstance(payload, dict):
            raise SECResponseError("SEC submissions response must be an object")
        filings = payload.get("filings")
        if not isinstance(filings, dict):
            raise SECResponseError("SEC submissions response has no filing object")
        recent = filings.get("recent")
        if not isinstance(recent, dict):
            raise SECResponseError("SEC submissions response has no recent filings")
        required = ("accessionNumber", "filingDate", "form", "primaryDocument")
        if any(not isinstance(recent.get(field), list) for field in required):
            raise SECResponseError("SEC recent filing columns are missing")

        lengths = [len(recent[field]) for field in required]
        documents: list[DiscoveredDocument] = []
        for index in range(min(*lengths)):
            form = str(recent["form"][index]).strip().upper()
            if form not in company.forms:
                continue
            accession = str(recent["accessionNumber"][index]).strip()
            primary_document = str(recent["primaryDocument"][index]).strip()
            filed_at = cls._filing_datetime(
                recent.get("acceptanceDateTime", [None] * max(lengths)),
                recent["filingDate"][index],
                index,
            )
            if (
                filed_at is None
                or filed_at < since.astimezone(UTC)
                or filed_at > until.astimezone(UTC)
            ):
                continue
            if not _ACCESSION.fullmatch(accession) or not _PRIMARY_DOCUMENT.fullmatch(
                primary_document
            ):
                continue
            accession_compact = accession.replace("-", "")
            cik_compact = str(int(company.cik))
            primary_url = (
                f"https://www.sec.gov/Archives/edgar/data/{cik_compact}/"
                f"{accession_compact}/{primary_document}"
            )
            index_url = (
                f"https://www.sec.gov/Archives/edgar/data/{cik_compact}/"
                f"{accession_compact}/{accession}-index.html"
            )
            description = cls._column_text(recent, "primaryDocDescription", index, 500)
            try:
                documents.append(
                    DiscoveredDocument(
                        source_id=source_id,
                        source_url=primary_url,
                        discovered_at=discovered_at,
                        external_reference=accession,
                        title=f"{form} — {description or company.entity_id}",
                        summary=description,
                        publisher="U.S. SEC EDGAR",
                        language="en",
                        published_at=filed_at,
                        metadata={
                            "accession_number": accession,
                            "cik": company.cik,
                            "entity_id": company.entity_id,
                            "form": form,
                            "filing_date": str(recent["filingDate"][index]),
                            "index_url": index_url,
                            "primary_document": primary_document,
                            "official_sec_filing": True,
                            "direct_etf_impact_unverified": True,
                            "fulltext_fetched": False,
                        },
                    )
                )
            except ValidationError:
                continue
            if len(documents) >= maximum:
                break
        return tuple(documents)

    @staticmethod
    def _filing_datetime(values: object, filing_date: object, index: int) -> datetime | None:
        if isinstance(values, list) and index < len(values) and isinstance(values[index], str):
            raw = values[index]
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=_SEC_SYSTEM_TIME)
                return parsed.astimezone(UTC)
            except ValueError:
                pass
        if isinstance(filing_date, str):
            try:
                day = datetime.strptime(filing_date, "%Y-%m-%d").date()
                return datetime.combine(day, time.min, tzinfo=_SEC_SYSTEM_TIME).astimezone(UTC)
            except ValueError:
                pass
        return None

    @staticmethod
    def _column_text(recent: dict[str, object], field: str, index: int, maximum: int) -> str | None:
        values = recent.get(field)
        if (
            not isinstance(values, list)
            or index >= len(values)
            or not isinstance(values[index], str)
        ):
            return None
        value = " ".join(values[index].split())[:maximum]
        return value or None


def sec_discovery_to_raw_document(document: DiscoveredDocument) -> RawDocument:
    accession = document.metadata.get("accession_number")
    if not isinstance(accession, str) or not _ACCESSION.fullmatch(accession):
        raise ValueError("a valid SEC accession number is required")
    if document.title is None:
        raise ValueError("a SEC filing title is required")
    digest = sha256(accession.encode("ascii")).hexdigest()
    return RawDocument(
        external=ExternalDocumentContent(
            source_url=document.source_url,
            title=document.title,
            body=document.summary or document.title,
            published_at=document.published_at,
            author=document.publisher,
            language="en",
            metadata={**document.metadata, "content_kind": "sec_filing_metadata"},
        ),
        control=RawDocumentControl(
            document_id=f"sec-{digest[:32]}",
            source_id=document.source_id,
            state=DocumentState.DISCOVERED,
            state_version=0,
            content_sha256=digest,
            idempotency=IdempotencyKey(
                scope="sec-discovery",
                key=digest,
                payload_sha256=digest,
            ),
            discovered_at=document.discovered_at,
        ),
    )
