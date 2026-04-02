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
- Data files: `thread_safe_apis.json`, `lock_macros.json`
