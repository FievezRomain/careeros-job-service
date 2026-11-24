# CareerOS Job Service

Ingest, normalize, deduplicate and manage job opportunities.

## Scope

- **Owns:** Normalized job records, source provenance and freshness metadata.
- **Inputs:** Authorized feeds, user imports and source adapters.
- **Outputs:** Job lifecycle events and query APIs.
- **Dependencies:** automation for scheduled collection; identity for scoped user data.

## Safety baseline

This public repository must never contain secrets, real resumes, real applications or personal data. External communication and application submission always require explicit user approval.

## Documentation

See [docs/overview.md](docs/overview.md) and the central [CareerOS workspace](https://github.com/FievezRomain/careeros-workspace).

## Development

The service uses Python 3.14, Pydantic v2 and inward-facing hexagonal dependencies. Install the locked environment with `uv sync --locked --all-groups`, set `DATABASE_URL` to the service-owned PostgreSQL 16 database, then start the application with:

```text
uv run uvicorn careeros_job.bootstrap:create_application --factory
```

Startup applies every adapter-owned migration and verifies the expected heads before the HTTP server accepts traffic. Run the deterministic checks declared in `[tool.careeros.quality]` before review.

## Status

Python service scaffold with isolated PostgreSQL persistence, startup migrations and health endpoints. Job ingestion and query behavior remain future increments.

## License

Apache-2.0.
