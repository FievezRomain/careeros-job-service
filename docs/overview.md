# CareerOS Job Service â€” service overview

## Responsibility

Ingest, normalize, deduplicate and manage job opportunities.

## Data ownership

Normalized job records, source provenance and freshness metadata.

## Interfaces

- Inputs: Authorized feeds, user imports and source adapters.
- Outputs: Job lifecycle events and query APIs.
- Dependencies: automation for scheduled collection; identity for scoped user data.

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

## Non-goals

This service does not absorb responsibilities owned by another CareerOS service. Cross-service workflows are coordinated through explicit contracts rather than shared databases.

## Security and privacy

Persist the minimum data required, record provenance, define retention, redact logs and use synthetic examples in this public repository.

The initial schema stores only an opaque record identifier, owner identifier and timestamps. Raw job source content and provenance are deliberately deferred to V1-018. Database credentials are supplied through environment-specific secret storage and are never logged.
