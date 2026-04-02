# ft-review-toolkit

A [Claude Code](https://docs.anthropic.com/en/docs/claude-code) plugin for analyzing and migrating CPython C extensions to free-threaded Python (PEP 703).

**Make your C extension free-threading safe.**

## What It Does

- **Finds thread-safety bugs** in existing C extension code (data races, unprotected shared state, unsafe API usage)
- **Plans migrations** from GIL-protected to free-threaded code
- **Triages ThreadSanitizer reports** into actionable findings
- **Scores readiness** with a quick dashboard

## Installation

Install as a Claude Code plugin:

```bash
claude plugins add /path/to/ft-review-toolkit/plugins/ft-review-toolkit
```

### Prerequisites

```bash
pip install tree-sitter tree-sitter-c
# Optional: pip install tree-sitter-cpp  (for C++ extensions)
```

## Commands

| Command | Purpose |
|---------|---------|
| `/ft-review-toolkit:assess [path]` | Quick readiness scorecard |
| `/ft-review-toolkit:explore [path]` | Full thread-safety analysis |
| `/ft-review-toolkit:plan [path]` | Phased migration plan |

## Classification System

| Tag | Meaning |
|-----|---------|
| **RACE** | Confirmed or highly likely data race |
| **UNSAFE** | Operation unsafe without the GIL |
| **PROTECT** | Shared state needing protection |
| **MIGRATE** | Pattern needing structural changes |
| **SAFE** | Confirmed safe pattern |

## Related Projects

- [cext-review-toolkit](https://github.com/devdanzin/cext-review-toolkit) — C extension correctness analysis
- [cpython-review-toolkit](https://github.com/devdanzin/cpython-review-toolkit) — CPython runtime C code analysis
- [code-review-toolkit](https://github.com/devdanzin/code-review-toolkit) — Python source code analysis

## License

MIT
