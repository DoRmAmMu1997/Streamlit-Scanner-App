# Approved modernization implementation status

This is a delivery ledger for the [approved plan](../superpowers/plans/2026-09-06-astra-modernization.md), last updated on 2026-09-23. Published PRs remain open: publication and successful checks do not mean a change has landed on `main`. Merge and production deployment remain with the repository owner.

## Published and verified packages

| PR | Reviewed head | Status and evidence |
|---|---|---|
| [#116](https://github.com/DoRmAmMu1997/Streamlit-Scanner-App/pull/116) | `130efd8` | Retained source-universe path fix; included in combined verification. |
| [#117](https://github.com/DoRmAmMu1997/Streamlit-Scanner-App/pull/117) | `8cc6f14` | Retained Ruff hook alignment guard; combined policy checks pass. |
| [#122](https://github.com/DoRmAmMu1997/Streamlit-Scanner-App/pull/122) | `e840512` | Requirement-name parsing correction published; hosted Python and Docker/security checks pass. |
| [#123](https://github.com/DoRmAmMu1997/Streamlit-Scanner-App/pull/123) | `b0bbe53` | Retained `app.py` coverage measurement and 89% floor. |
| [#124](https://github.com/DoRmAmMu1997/Streamlit-Scanner-App/pull/124) | `8e1f186` | Valid baseline, single-read observations, bounded membership, SQL selection, migration and test isolation corrected; all six hosted checks pass. |
| [#125](https://github.com/DoRmAmMu1997/Streamlit-Scanner-App/pull/125) | `6d653d8` | Transcript destination checks, redirect limits and public-IP binding published; all six hosted checks pass. |
| [#126](https://github.com/DoRmAmMu1997/Streamlit-Scanner-App/pull/126) | `a985970` | Historical market-date ranking and shared finite-number validation published; all six hosted checks pass. |
| [#127](https://github.com/DoRmAmMu1997/Streamlit-Scanner-App/pull/127) | `616c549` | PDF parser process containment, stacked on #125; independent review and all three posted hosted checks pass, including native Linux and Docker. |
| [#128](https://github.com/DoRmAmMu1997/Streamlit-Scanner-App/pull/128) | `5e5e57a` | Atomic locked candle-cache preservation and stale-repair protection; independent review and all six hosted checks pass. |

A separate integration worktree first combined #116, #117, #122, #123, #124, #125 and #126 at `e42cdec`. On both pinned Python 3.11 and 3.12, that exact tree's full suite passes **2,156 tests, with one skip and 89.84% coverage including `app.py`**. Pre-commit configuration, compilation, Ruff, full mypy, Bandit, the pinned dependency audit, and diff checks also pass on both interpreters. The next integration head, `9b3dbe7`, additionally includes #127 and #128; its complete gate run remains pending. Neither tree is the final integrated revision.

## Work still required

| Package | Current state | Completion requirement |
|---|---|---|
| Analysis permissions and SDK contracts | Implementation and documentation committed through local `91dfee3`; affected suite passes 327 tests, static/audit gates pass. Review found the missing-terminal-result case in IPO extraction. | Require a terminal SDK result before parsing, prove no proposal writes on premature stream end, re-review, verify full gates and publish. |
| Forward-return processing | Core migration and reviewed fixes committed through `532224f`. Malformed rows survive normalization/range slicing, and incomplete dates cannot establish earliest-bar authority. Both review findings are resolved. | Complete the final full gate and composed-cache checks, publish after #124, and verify hosted checks. |
| IPO current score and freshness | ADR and shared revision-lock interface recorded; implementation started on the forward-return schema branch. | Complete state selection, bounded retries, time-derived freshness and `20260921ipo013` migration. |
| IPO proposal approval and editing | Approved requirements recorded. | Implement document chronology/manual-baseline protection and source-bound forms after scoring state. |
| Shared evidence validation | Isolated implementation package started. | Unify URL/hash/label contracts and cache rejection, retain compatibility and verify all consumers. |
| Maintenance/dependencies | Official dependency/Actions inventory complete; isolated implementation package prepared. | Complete service extraction, unused compatibility cleanup, optional indicator verification, reviewed pins and weekly checks. |
| Database verification | Deliberate schema-drift regressions written. | Strengthen comparison; run disposable PostgreSQL integration and migration/repository deployment smoke. |
| Streamlit workflows | Approved requirements recorded; waits for underlying role/IPO behavior. | Group navigation, improve evidence comparison/status, add safe selected/stale rescoring and failed-item retry; browser-test isolated data. |
| Audit/documentation and final review | This ledger and approved plan saved; #118 remains in progress. | Reconcile living docs, integrated schema chain, final security coverage, technical-debt/general review and delivery evidence. |

## Schema and evidence boundaries

The schema chain begins with #124's `20260904obs004` and follow-up `20260906obs004a`; forward-return retry state follows as `20260909valid005`. IPO scoring state and approval migrations must continue that single chain. Schema changes and their migrations ship together, and their composed result must converge to one Alembic head.

The original Codex Security scan reviewed baseline `8a8846f` and recorded three findings: two medium and one low. Manual coverage was explicitly partial: eight files fully reviewed, with additional call-chain/architecture inspection. Its sealed report is historical evidence, not a completed security assessment of these new branches. The final integrated scan and remaining source review are still required; remediation is not complete merely because an individual code package passes tests.

Testing uses mocked providers and disposable storage. Docker is unavailable locally, so hosted checks supply image/Compose evidence; the strengthened PostgreSQL lane remains a separate requirement until it actually runs. No production database, broker execution, provider analysis, notifications, GitHub merge, or production deployment is included. The user's two modified universe CSVs in the shared checkout remain untouched.
