# Approved modernization implementation status

This is a delivery ledger for the [approved plan](../superpowers/plans/2026-09-06-astra-modernization.md), last updated on 2026-09-29. GitHub readback confirms that the implementation PRs below and the original audit PR #118 were merged on September 24. Subsequent dependency and CI updates are also merged, and the current baseline is `2d352c2964b330c4b8f52e9980edf599c05719bc`. The remaining packages are still in progress. Merge and production deployment remain with the repository owner; this record does not establish deployment.

## Merged packages and historical verification

The reviewed heads below identify the exact candidates tested during implementation. Some PRs received later integration commits before merging. Their historical test results do not substitute for verification against the current baseline and dependency pins.

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
| [#129](https://github.com/DoRmAmMu1997/Streamlit-Scanner-App/pull/129) | `532224f` | Forward-return integrity, raw-candle preservation and fair retries, stacked on #124; independent review and all three posted hosted checks pass. |
| [#130](https://github.com/DoRmAmMu1997/Streamlit-Scanner-App/pull/130) | `a874684` | Current-role analysis authorization and terminal SDK-result enforcement; independent review, isolated browser checks and all six hosted checks pass. |

A separate integration worktree first combined #116, #117, #122, #123, #124, #125 and #126 at `e42cdec`. On the then-pinned Python 3.11 and 3.12 environments, that exact tree passed **2,156 tests, with one skip and 89.84% coverage including `app.py`**. Pre-commit configuration, compilation, Ruff, full mypy, Bandit, the pinned dependency audit, and diff checks also passed on both interpreters. The later local integration head `c475862` added #127, #128 and #129, but did not complete a full gate run. Current `main` supersedes these historical integration candidates.

The final local #129 candidate ran all 2,128 collected tests: 2,127 passed, one skipped, with 89.85% aggregate coverage including `app.py`. The final #130 candidate ran all 2,053 collected tests: 2,052 passed, one skipped, with 90.25% aggregate coverage including `app.py`. Local runs used recoverable module groups after host interruptions, with separate databases/coverage files and exact clean-commit/interpreter/test identity. Hosted CI subsequently passed the complete single-process suite on both Python versions supported at that time for each PR.

The repository now supports **Python 3.12, 3.13 and 3.14**, deploys Python 3.14, and targets Python 3.12 in Ruff/mypy. PR #131 corrected raw malformed-row preservation after the candle-cache and validation packages were combined. PRs #132–#141 added weekly Dependabot checks and upgraded dependencies, Actions, Python and PostgreSQL. Further dependency updates, #146's verbatim Agent SDK prompt delivery, and #147's BSE transcript compatibility fix are included in the current baseline. Remaining work must preserve those changes and run fresh verification using the current pins and matrix. The older Python 3.11 results remain historical evidence only.

## Work still required

| Package | Current state | Completion requirement |
|---|---|---|
| IPO current score and freshness | ADR and shared revision-lock interface recorded; implementation started on the forward-return schema branch. | Complete state selection, bounded retries, time-derived freshness and `20260921ipo013` migration. |
| IPO proposal approval and editing | Approved requirements recorded. | Implement document chronology/manual-baseline protection and source-bound forms after scoring state. |
| Shared evidence validation | Isolated implementation package started. | Unify URL/hash/label contracts and cache rejection, retain compatibility and verify all consumers. |
| Maintenance/dependencies | Weekly updates and runtime/dependency upgrades have merged; isolated follow-up package is based on current `main`. | Complete service extraction, unused compatibility cleanup, optional indicator verification and reviewed commit-SHA pins for the current Actions versions. |
| Database verification | Deliberate schema-drift regressions written. | Strengthen comparison; run disposable PostgreSQL integration and migration/repository deployment smoke. |
| Streamlit workflows | Approved requirements recorded; waits for underlying role/IPO behavior. | Group navigation, improve evidence comparison/status, add safe selected/stale rescoring and failed-item retry; browser-test isolated data. |
| Audit/documentation and final review | Original #118 has merged; this follow-up saves the approved plan and current delivery ledger. | Reconcile living docs, integrated schema chain, final security coverage, technical-debt/general review and delivery evidence. |

## Schema and evidence boundaries

The schema chain begins with #124's `20260904obs004` and follow-up `20260906obs004a`; forward-return retry state follows as `20260909valid005`. IPO scoring state and approval migrations must continue that single chain. Schema changes and their migrations ship together, and their composed result must converge to one Alembic head.

The original Codex Security scan reviewed baseline `8a8846f` and recorded three findings: two medium and one low. Manual coverage was explicitly partial: eight files fully reviewed, with additional call-chain/architecture inspection. Its sealed report is historical evidence, not a completed security assessment of these new branches. The final integrated scan and remaining source review are still required; remediation is not complete merely because an individual code package passes tests.

Testing uses mocked providers and disposable storage. Docker is unavailable locally, so hosted checks supply image/Compose evidence; the strengthened PostgreSQL lane remains a separate requirement until it actually runs. This implementation work does not authorize production database changes, broker execution, provider analysis, notifications, GitHub merges, or production deployment. Universe CSVs are outside the remaining edit scope.
