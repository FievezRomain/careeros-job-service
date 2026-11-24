# CareerOS Job Service â€” service overview

## Responsibility

Ingest, normalize, deduplicate and manage job opportunities.

## Data ownership

Normalized job records, source provenance and freshness metadata.

## Interfaces

- Inputs: Authorized feeds, user imports and source adapters.
- Outputs: Job lifecycle events and query APIs.
- Dependencies: automation for scheduled collection; identity for scoped user data.
- `POST /v1/jobs/manual` records one owned opportunity from explicit user input. It requires a valid bearer token, the `jobs:write` scope and an `Idempotency-Key`.
- `GET /v1/jobs?page=<n>&pageSize=<n>` returns only the authenticated owner's latest opportunity version per group and requires `jobs:read`.
- `GET /v1/jobs/{opportunityId}` returns one owned immutable version. A missing or differently owned identifier is reported as not found.
- `POST /v1/internal/discovery/search-snapshots` consumes the versioned
  `profile.match-relevant-change.v1` projection and requires the internal `discovery:write`
  scope. Older or duplicate events do not replace the current projection.
- `POST /v1/internal/discovery/runs` requires `discovery:run`, a correlation identifier and
  an idempotency key. It selects active snapshots inside their own IANA-timezone window,
  runs at most once per local day and shares collection only for identical signature IDs.

## Contract rules

- APIs and events are versioned.
- Consumers must be idempotent where retries are possible.
- Correlation identifiers are propagated without logging sensitive payloads.
- Authorization and ownership are checked at the service boundary.

## Runtime architecture

- Python 3.14 and Pydantic v2 validate configuration and boundary models.
- Application startup depends on the core-owned `MigrationRunner` port; it does not import SQLAlchemy.
- The composition root injects the PostgreSQL migration adapter into the startup use case.
- PostgreSQL 16 is isolated to this service. Migration scripts and their runner live under `adapters/postgresql/migrations`.
- Startup applies pending migrations, verifies all expected heads and fails before serving traffic when verification is incomplete.
- `/health/live` and `/health/ready` are operational endpoints only; no job business endpoint is introduced by the scaffold.
- Manual capture is orchestrated by an application use case through injected identity, clock, identifier and repository ports. JWT and SQLAlchemy types remain in adapters.
- User-visible title, employer and description text is normalized deterministically with Unicode NFKC and collapsed whitespace. Raw source wording remains unchanged for provenance.
- Automatic grouping uses normalized title and employer identity. Repeated material content is deduplicated, changed source content creates a new immutable version in the same group, and `forceSeparate` starts a distinct group when automatic grouping is wrong.
- Capture-command idempotency is stored separately from opportunity versions, so a deduplicated request still reserves its idempotency key and conflicting reuse is rejected.
- Query pagination is deterministic, returns current group versions only and never crosses account or tenant ownership boundaries.
- Daily discovery is orchestrated by a core use case through injected search-projection,
  source-registry, source-gateway, observation, checkpoint and outbox ports. SQLAlchemy,
  HTTP and provider details remain adapters.
- The first provider adapter uses Greenhouse's public unauthenticated Job Board API. A
  configured board is collected in full with `content=true`; the reported total is checked,
  conditional request metadata and rate-limit evidence are retained, and any truncation is
  explicit in the run result. Generic scraping, authenticated scraping and protection bypass
  are not supported.
- Observation identity is owner plus source plus external identifier. Unchanged material
  content updates `lastSeenAt` only; changed content creates a new immutable normalized
  opportunity version and a transactional `job.opportunity-version-discovered.v1` outbox
  event. Consumers are idempotent under at-least-once delivery.
- Automation owns bounded retries, exponential backoff and dead-letter routing around the
  idempotent run command. Failed run claims are retryable; completed claims replay safely.

## Non-goals

This service does not absorb responsibilities owned by another CareerOS service. Cross-service workflows are coordinated through explicit contracts rather than shared databases.

## Security and privacy

Persist the minimum data required, record provenance, define retention, redact logs and use synthetic examples in this public repository.

Manual capture stores the title, employer, description, source reference, exact raw source wording and server-recorded capture time for the authenticated owner and tenant. Raw source text is retained with the opportunity until future deletion/retention behavior is delivered; it is never written to logs. Database and OIDC credentials are supplied through environment-specific secret storage and are never logged.

Catalog responses mark title, employer and description as source-stated, expose no inferred fields in V1-019, and identify location, contract type and work mode as unknown until a later validated extraction provides evidence.

Automatically discovered Greenhouse records mark source-provided location as stated while
contract type and work mode remain unknown. The exact JSON source wording, source URL,
external identifier, first observation and last-seen time are retained for audit and are
never logged.

## Source configuration

`GREENHOUSE_BOARDS` is a JSON array of `{source_id, board_token, employer}` records supplied
per environment. No provider credential is required. Greenhouse documents that listing jobs
is public and that only application submission requires authentication:
https://docs.greenhouse.io/job-board.html
