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
- `migration-planner` agent — phased free-threading migration plans
- `plan` command — produce a tailored migration plan
- Data files: `thread_safe_apis.json`, `lock_macros.json`, `atomic_patterns.json`, `critical_section_apis.json`, `ft_migration_checklist.json`
- `task-workflow` skill for standard issue → branch → code → test → commit → PR → merge cycle
