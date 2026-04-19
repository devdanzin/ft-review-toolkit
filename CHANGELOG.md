# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.3.0] - 2026-04-18

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

### Added
- `test_scan_common.py` — 22 tests covering `discover_c_files`, `find_project_root`, `parse_common_args`, `is_thread_local`, `is_init_function`, `is_in_region`, `extract_nearby_comments`, `has_safety_annotation`, `make_finding`.
- `test_tree_sitter_utils.py` — 18 tests covering `extract_functions`, `extract_static_declarations`, `find_calls_in_scope`, `parse_bytes_for_file`, `get_node_text`, `strip_comments`.
- `make_finding()` helper in `scan_common.py` for consistent finding dict construction across all scanners.
- Version/commit tags on vendored files (`tree_sitter_utils.py`, `scan_common.py`) indicating upstream cext-review-toolkit commit `1475ac6`.

### Fixed
- `_run_git_streaming` return code now checked in `analyze_ft_history.py` — failed git commands warn to stderr instead of silently producing empty results.
- Silent data-loading failures in 4 scanners: `scan_shared_state.py`, `scan_unsafe_apis.py`, `scan_lock_discipline.py`, `scan_stw_safety.py` now warn to stderr when JSON data files fail to load instead of silently producing zero findings.
- Conditional test assertions in `test_scan_shared_state.py` and `test_scan_atomic_candidates.py` that silently passed when no findings were produced.
- Plugin metadata counts in `plugin.json` (9 agents → 10, 6 scripts → 7).
- Missing `test_output_envelope` assertions for `functions_analyzed` and `skipped_files` in `test_scan_lock_discipline.py` and `test_scan_atomic_candidates.py`.
- Redundant `source_bytes.decode()` called twice per declaration in `scan_shared_state.py:_analyze_file`.

### Enhanced
- All 7 `main()` functions now print full tracebacks to stderr before outputting JSON error envelopes, improving debuggability.
- Extracted duplicated helpers (`is_thread_local`, `is_init_function`, `is_in_region`) from individual scanners into `scan_common.py`, eliminating copy-paste across `scan_shared_state.py`, `scan_atomic_candidates.py`, `scan_unsafe_apis.py`, and `scan_stw_safety.py`.
- Documented JSON envelope variants for `parse_tsan_report.py`, `analyze_ft_history.py`, and `scan_stw_safety.py` in CLAUDE.md.
- Listed all script-backed agents explicitly in CLAUDE.md agents section.

- Simplified `_check_error_path_releases` in `scan_lock_discipline.py` — extracted `_has_release_via_goto()` helper, replaced manual loop with `any()`.

### Enhanced (previous)
- Revised STW safety contract based on YiFei Zhu's analysis of CPython allocation paths: object allocation is safe during STW on Python 3.14+ (GC runs only on eval breaker), `PyErr_NoMemory`/`PyErr_SetString` conditionally safe, dict ops safe with `CheckExact` types. Updated `stw_safe_apis.json`, `scan_stw_safety.py`, and tests.
- Port cext-review-toolkit enhancements: global non-restarting finding numbering in `explore` reports (cext #33), `extract_nearby_comments()`/`has_safety_annotation()` in `scan_common.py` for comment-aware triage (cext #30), enhanced deduplication guidance with intra-agent, cross-agent, and TSan dedup rules (cext #28).

### Fixed
- Remove `tsan-stress-generator` from `explore` pipeline Group C. It produces a script (not findings) that must be executed externally before its output is useful. Available as standalone aspect (`explore . stress-test`).
