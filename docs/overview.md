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

## Non-goals

This service does not absorb responsibilities owned by another CareerOS service. Cross-service workflows are coordinated through explicit contracts rather than shared databases.

## Security and privacy

Persist the minimum data required, record provenance, define retention, redact logs and use synthetic examples in this public repository.