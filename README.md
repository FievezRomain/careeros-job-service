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

## Status

Architecture and security baseline. Functional implementation will be added incrementally with tests and versioned contracts.

## License

Apache-2.0.