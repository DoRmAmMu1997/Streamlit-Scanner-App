# IPO-005 implementation checklist

- [x] Refresh `main`, remove the merged IPO-004 worktree, and create an isolated branch.
- [x] Add failing formula, missing-data, edge-case, and reconciliation tests.
- [x] Add pure frozen ratio contracts and exact Decimal calculations.
- [x] Extend manual evidence DTOs, ORM rows, repository adapters, and public exports.
- [x] Add the legacy-compatible migration and guarded downgrade tests.
- [x] Extend the admin form and correction prefill with sourced IPO-005 facts.
- [x] Update architecture, storage, UI, security, observability, and operations docs.
- [x] Run focused/full tests, coverage, compileall, Ruff, mypy, Bandit, and pip-audit.
- [x] Run migration parity, rendered desktop Browser QA, and Codex Security.
- [ ] Run Docker/Compose and mobile Browser QA (Docker is not installed locally; the in-app viewport command timed out, so hosted CI and a later Browser retry own these checks).
- [ ] Review the diff, commit with co-authorship, push, open a draft PR, and monitor CI.
