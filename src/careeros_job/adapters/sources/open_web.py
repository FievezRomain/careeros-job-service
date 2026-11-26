"""Collect web links via the AI gateway and verify explicit public ATS references."""

import hashlib
import html
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import uuid4

import httpx2
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from careeros_job.application.discovery import (
    DiscoveredOpportunity,
    DiscoverySourceUnavailable,
    SearchSignature,
    SourceCheckpoint,
    SourceCollection,
    SourceDefinition,
    SourceGateway,
)
from careeros_job.domain.opportunity import normalize_identity_text

_ATS = re.compile(r"https://(?:boards|job-boards)\.greenhouse\.io/([\w-]{1,100})/jobs/(\d{1,20})")
_URL = re.compile(r"https://[^\s<>\"']+")
_TAG = re.compile(r"<[^>]+>")


class WebHit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    url: str = Field(min_length=1, max_length=2000)
    title: str = Field(min_length=1, max_length=200)
    snippet: str = Field(max_length=4000)
    score: float = Field(ge=0, le=1)
    source: str = Field(min_length=1, max_length=60)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or url.username is not None
            or url.port is not None
            or not re.fullmatch(r"(?:[a-z0-9-]+\.)+[a-z][a-z0-9-]*", url.hostname or "")
            or (url.hostname or "").endswith((".local", ".internal", ".localhost"))
            or "\\" in value
            or any(c.isspace() or ord(c) < 32 for c in value)
        ):
            raise ValueError("expected a public HTTPS result URL")
        return value


class WebResults(BaseModel):
    results: tuple[WebHit, ...] = Field(max_length=15)
    searchRequests: int = Field(ge=0, le=3)


class GatewayResult(BaseModel):
    capabilityId: str
    outputSchemaVersion: str
    output: WebResults


class CanonicalJob(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, str_strip_whitespace=True)
    id: int
    title: str = Field(min_length=1, max_length=200)
    company_name: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=75_000)


def _body(response: httpx2.Response) -> bytes:
    response.raise_for_status()
    content = bytearray()
    for chunk in response.iter_bytes(chunk_size=8192):
        content.extend(chunk)
        if len(content) > 100_000:
            raise DiscoverySourceUnavailable
    return bytes(content)


class OpenWebGateway:
    def __init__(self, client: httpx2.Client, gateway_url: str, token: str) -> None:
        self._client = client
        self._url = gateway_url.rstrip("/") + "/v1/capabilities/discover-job-links/invocations"
        self._token = token

    def collect(
        self, source: SourceDefinition, signature: SearchSignature, checkpoint: SourceCheckpoint
    ) -> SourceCollection:
        search_input = {**signature.model_dump(mode="json", exclude={"signature_id", "exclusions"})}
        search_input = {
            name: value[:5] if isinstance(value, list) else value
            for name, value in search_input.items()
        }
        try:
            with self._client.stream(
                "POST",
                self._url,
                timeout=40,
                follow_redirects=False,
                headers={"Authorization": f"Bearer {self._token}", "Idempotency-Key": str(uuid4())},
                json={
                    "capabilityVersion": "1.0",
                    "inputSchemaVersion": "1.0",
                    "input": search_input,
                },
            ) as response:
                result = GatewayResult.model_validate_json(_body(response))
        except (httpx2.HTTPError, ValidationError):
            raise DiscoverySourceUnavailable from None
        if result.capabilityId != "discover-job-links" or result.outputSchemaVersion != "1.0":
            raise DiscoverySourceUnavailable
        candidates: dict[str, DiscoveredOpportunity] = {}
        checked: dict[str, tuple[CanonicalJob, str, str] | None] = {}
        observed_at = datetime.now(UTC)
        # An explicit reference preserves the aggregator identity during later promotion.
        for hit in sorted(result.output.results, key=lambda item: bool(_ATS.fullmatch(item.url))):
            original = hit.url
            references = [url.split("?", 1)[0].rstrip(".,)") for url in _URL.findall(hit.snippet)]
            direct = original.split("?", 1)[0]
            explicit = {url for url in references if _ATS.fullmatch(url)}
            reference = (
                direct
                if _ATS.fullmatch(direct)
                else (next(iter(explicit)) if len(explicit) == 1 else None)
            )
            canonical = None
            if reference is not None:
                if reference not in checked:
                    checked[reference] = self._resolve(reference) if len(checked) < 3 else None
                canonical = checked[reference]
            if (
                canonical
                and reference
                and normalize_identity_text(canonical[0].title)
                == normalize_identity_text(hit.title)
            ):
                job, description, raw = canonical
                candidate = DiscoveredOpportunity(
                    external_id=hashlib.sha256(original.encode()).hexdigest(),
                    title=job.title,
                    employer=job.company_name,
                    description=description,
                    location=None,
                    source_url=reference,
                    original_url=original,
                    retrieval_provider=hit.source,
                    raw_source=raw,
                    observed_at=observed_at,
                )
            else:
                candidate = DiscoveredOpportunity(
                    external_id=hashlib.sha256(original.encode()).hexdigest(),
                    title=hit.title,
                    employer=None,
                    description=None,
                    location=None,
                    source_url=original,
                    original_url=original,
                    retrieval_provider=hit.source,
                    raw_source=hit.snippet or hit.title,
                    observed_at=observed_at,
                    source_access="external_to_verify",
                )
            candidates.setdefault(candidate.source_url, candidate)
        return SourceCollection(
            items=tuple(candidates.values()), pages_consumed=result.output.searchRequests
        )

    def _resolve(self, reference: str) -> tuple[CanonicalJob, str, str] | None:
        match = _ATS.fullmatch(reference)
        if match is None:
            return None
        board, identifier = match.groups()
        url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{identifier}"
        try:
            with self._client.stream("GET", url, follow_redirects=False) as response:
                raw = _body(response).decode()
            job = CanonicalJob.model_validate_json(raw)
            description = " ".join(_TAG.sub(" ", html.unescape(job.content)).split())
            if job.id == int(identifier) and 0 < len(description) <= 10_000:
                return job, description, raw
        except (httpx2.HTTPError, ValidationError, UnicodeError, DiscoverySourceUnavailable):
            return None
        return None


class DiscoverySources:
    def __init__(
        self,
        sources: tuple[SourceDefinition, ...],
        structured: SourceGateway,
        web: SourceGateway | None,
    ) -> None:
        self._sources = sources + (
            (
                SourceDefinition(
                    source_id="open-web",
                    source_type="open_web",
                    provider="open_web",
                    employer="Open web",
                ),
            )
            if web
            else ()
        )
        self._structured = structured
        self._web = web

    def sources(self, signature: SearchSignature) -> tuple[SourceDefinition, ...]:
        return self._sources

    def collect(
        self, source: SourceDefinition, signature: SearchSignature, checkpoint: SourceCheckpoint
    ) -> SourceCollection:
        gateway = self._web if source.source_type == "open_web" else self._structured
        if gateway is None:
            raise DiscoverySourceUnavailable
        return gateway.collect(source, signature, checkpoint)
