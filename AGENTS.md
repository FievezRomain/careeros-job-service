# Agent instructions â€” careeros-job-service

Read README.md, docs/overview.md and SECURITY.md before changing this repository.

## Required invariants

- Keep this service inside its documented responsibility boundary.
- Never add a secret, token, real CV, real application or personal data.
- Use fictitious fixtures and placeholder environment values only.
- Authenticate every operation and enforce ownership/tenant isolation server-side.
- Document public API or event changes and update contract tests.
- Add or update tests with every behavioral change.
- Do not trigger external messages, applications or destructive actions without explicit user approval at action time.

Consult the central workspace skills for architecture, contracts, events, security, testing and documentation work.

## Code Review Rules

- Reject framework, ORM or migration imports from domain and application modules.
- Require core-owned ports, constructor injection and concrete wiring only in the composition root.
- Keep PostgreSQL migrations and their runner inside `adapters/postgresql/migrations`.
- Require startup tests proving migration application, head verification and fail-closed behavior.
- Use Pydantic v2 for configuration and boundary data; do not use dataclasses as boundary DTOs.
- Treat P0/P1 findings from `code-quality-review` as blockers in addition to deterministic CI.
