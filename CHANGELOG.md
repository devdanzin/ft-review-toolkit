# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Project scaffolding and plugin structure
- Vendored `tree_sitter_utils.py` and `scan_common.py` from cext-review-toolkit
- `scan_shared_state.py` — detect global/static shared mutable state
- `scan_unsafe_apis.py` — detect thread-unsafe Python/C API usage
- `analyze_ft_history.py` — git history analysis for free-threading commits
- `shared-state-auditor` agent
- `unsafe-api-detector` agent
- `ft-history-analyzer` agent
- `assess` command — quick free-threading readiness dashboard
- `scan_lock_discipline.py` — lock acquire/release pairing and error path analysis
- `scan_atomic_candidates.py` — find variables needing atomic operations
- `lock-discipline-checker` agent
- `atomic-candidate-finder` agent
- `explore` command — full thread-safety analysis with phased agent groups
- `parse_tsan_report.py` — ThreadSanitizer report parsing, deduplication, and triage
- `tsan-report-analyzer` agent
- `stop-the-world-advisor` agent — synchronization mechanism recommendations
- `tsan-stress-generator` agent — generate concurrent stress test scripts for TSan race detection, with TSan auto-detection, fork-based subprocess isolation, and per-scenario timeout
- `migration-planner` agent — phased free-threading migration plans
- `plan` command — produce a tailored migration plan
- Data files: `thread_safe_apis.json`, `lock_macros.json`, `atomic_patterns.json`, `critical_section_apis.json`, `ft_migration_checklist.json`
- `task-workflow` skill for standard issue → branch → code → test → commit → PR → merge cycle
- `scan_stw_safety.py` — StopTheWorld safety analysis with intra-file call graph, function classification (stw_safe/stw_unsafe/stw_unknown), and STW region violation detection
- `stw-safety-checker` agent — verifies code during `_PyEval_StopTheWorld` doesn't invoke Python, trigger GC, or set exceptions
- `data/stw_safe_apis.json` — CPython API classification for StopTheWorld safety (safe/unsafe/contract rules with CPython source evidence)
- `_PyEval_StopTheWorld`/`_PyEval_StartTheWorld` pairing in lock discipline scanner
- Enhanced `stop-the-world-advisor` with STW safety contract guidance

### Fixed
- Remove `tsan-stress-generator` from `explore` pipeline Group C. It produces a script (not findings) that must be executed externally before its output is useful. Available as standalone aspect (`explore . stress-test`).
