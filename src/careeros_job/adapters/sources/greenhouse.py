"""Adapter for Greenhouse's public, unauthenticated Job Board API."""

import html
import json
import re
from datetime import UTC, datetime
from typing import Protocol
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, ValidationError

from careeros_job.application.discovery import (
    DiscoveredOpportunity,
    SearchSignature,
    SourceCheckpoint,
    SourceCollection,
    SourceDefinition,
)

_TAG = re.compile(r"<[^>]+>")


class TransportResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: int
    headers: dict[str, str] = Field(default_factory=dict)
    body: bytes = b""


class GreenhouseLocation(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    name: str = Field(min_length=1, max_length=500)


class GreenhouseJob(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: int | str
    title: str = Field(min_length=1, max_length=200)
    absolute_url: AnyHttpUrl
    location: GreenhouseLocation
    content: str = Field(default="", max_length=100_000)


class GreenhouseMeta(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    total: int = Field(ge=0)


class GreenhousePayload(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    jobs: tuple[GreenhouseJob, ...]
    meta: GreenhouseMeta


class HttpTransport(Protocol):
    def get(self, url: str, headers: dict[str, str]) -> TransportResponse: ...


class UrlLibTransport:
    def get(self, url: str, headers: dict[str, str]) -> TransportResponse:
        request = Request(url, headers=headers, method="GET")  # nosec B310
        try:
            with urlopen(request, timeout=20) as response:  # nosec B310
                return TransportResponse(
                    status=response.status,
                    headers={key.lower(): value for key, value in response.headers.items()},
                    body=response.read(),
                )
        except HTTPError as error:
            if error.code == 304:
                return TransportResponse(status=304)
            raise


class GreenhouseSourceRegistry:
    def __init__(self, sources: tuple[SourceDefinition, ...]) -> None:
        if any(source.provider != "greenhouse" for source in sources):
            raise ValueError("Greenhouse registry accepts only greenhouse sources")
        self._sources = sources

    def sources(self, signature: SearchSignature) -> tuple[SourceDefinition, ...]:
        del signature
        return self._sources


class GreenhouseGateway:
    def __init__(self, transport: HttpTransport, board_tokens: dict[str, str]) -> None:
        self._transport = transport
        self._board_tokens = dict(board_tokens)

    def collect(
        self,
        source: SourceDefinition,
        signature: SearchSignature,
        checkpoint: SourceCheckpoint,
    ) -> SourceCollection:
        del signature
        headers = {"Accept": "application/json", "User-Agent": "CareerOS/1.0"}
        if checkpoint.etag:
            headers["If-None-Match"] = checkpoint.etag
        if checkpoint.last_modified:
            headers["If-Modified-Since"] = checkpoint.last_modified
        try:
            board_token = self._board_tokens[source.source_id]
        except KeyError as error:
            raise ValueError("Greenhouse source has no configured board token") from error
        url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true"
        response = self._transport.get(url, headers)
        if response.status == 304:
            return SourceCollection(not_modified=True, pages_consumed=1, checkpoint=checkpoint)
        if response.status != 200:
            raise RuntimeError(f"greenhouse source returned HTTP {response.status}")
        try:
            raw_payload = json.loads(response.body)
            payload = GreenhousePayload.model_validate(raw_payload)
            raw_jobs = raw_payload["jobs"]
            reported_total = payload.meta.total
            observed_at = datetime.now(UTC)
            items = tuple(
                self._candidate(source, job, raw_job, observed_at)
                for job, raw_job in zip(payload.jobs, raw_jobs, strict=True)
            )
        except (KeyError, TypeError, ValueError, ValidationError, json.JSONDecodeError) as error:
            raise ValueError("invalid Greenhouse Job Board response") from error
        return SourceCollection(
            items=items,
            pages_consumed=1,
            reported_total=reported_total,
            truncated=len(items) != reported_total,
            checkpoint=SourceCheckpoint(
                etag=response.headers.get("etag"),
                last_modified=response.headers.get("last-modified"),
            ),
            rate_limit_remaining=self._remaining(response.headers.get("x-ratelimit-remaining")),
        )

    @staticmethod
    def _remaining(value: str | None) -> int | None:
        return int(value) if value and value.isdigit() else None

    @staticmethod
    def _candidate(
        source: SourceDefinition,
        job: GreenhouseJob,
        raw_job: dict[str, object],
        observed_at: datetime,
    ) -> DiscoveredOpportunity:
        content = html.unescape(job.content)
        description = " ".join(_TAG.sub(" ", content).split())
        raw_source = json.dumps(raw_job, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return DiscoveredOpportunity(
            external_id=str(job.id),
            title=job.title,
            employer=source.employer,
            description=description or "Description not provided by source.",
            location=job.location.name,
            source_url=str(job.absolute_url),
            raw_source=raw_source,
            observed_at=observed_at,
        )
