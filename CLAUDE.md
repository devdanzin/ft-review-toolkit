# CLAUDE.md — ft-review-toolkit development guide

## Project overview
ft-review-toolkit is a [Claude Code](https://docs.anthropic.com/en/docs/claude-code) plugin for analyzing and migrating CPython C extensions to free-threaded Python (PEP 703). It finds thread-safety bugs, plans migrations, and triages ThreadSanitizer reports.

Part of a family of review toolkits:
- [code-review-toolkit](https://github.com/devdanzin/code-review-toolkit) — Python source code
- [cpython-review-toolkit](https://github.com/devdanzin/cpython-review-toolkit) — CPython runtime C code
- [cext-review-toolkit](https://github.com/devdanzin/cext-review-toolkit) — C extension correctness
- **ft-review-toolkit** — C extension thread safety (this project)

Key distinction from cext-review-toolkit: cext-review-toolkit answers "does my extension have bugs?" ft-review-toolkit answers "is my extension safe without the GIL, and how do I make it safe?"

Uses Tree-sitter for C/C++ parsing via vendored `tree_sitter_utils.py` and `scan_common.py` from cext-review-toolkit.

## Prerequisites
- Python 3.10+
- `tree-sitter` and `tree-sitter-c`: `pip install tree-sitter tree-sitter-c`
- `tree-sitter-cpp` (optional): `pip install tree-sitter-cpp` — enables C++ file parsing
- No other dependencies — all scripts use only the standard library plus tree-sitter

## Dev commands
```bash
# Activate the project venv
source ~/venvs/cext-review-toolkit/bin/activate

# Run all tests
python -m unittest discover tests -v

# Run a specific test file
python -m unittest tests.test_scan_shared_state -v

# Run a single script standalone (all output JSON to stdout)
python plugins/ft-review-toolkit/scripts/scan_shared_state.py /path/to/extension
python plugins/ft-review-toolkit/scripts/scan_unsafe_apis.py /path/to/extension

# Lint and format
ruff format plugins/ft-review-toolkit/scripts/ tests/
ruff check plugins/ft-review-toolkit/scripts/ tests/
mypy
```

## Code style
- Python 3.10+ (uses `X | Y` union syntax)
- Double quotes for strings
- Type hints on all function signatures
- Docstrings on classes and public functions
- Tests use `unittest` — never pytest
- Linted and formatted with ruff, type checked with mypy

## Project structure
```
ft-review-toolkit/
├── CLAUDE.md                          # This file
├── README.md                          # User-facing documentation
├── CHANGELOG.md                       # Keep a Changelog format
├── LICENSE                            # MIT
├── plugins/ft-review-toolkit/        # The actual plugin
│   ├── .claude-plugin/plugin.json    # Plugin metadata
│   ├── agents/                       # Agent prompt definitions (markdown)
│   ├── commands/                     # Command definitions (markdown)
│   ├── scripts/                      # Python scripts (the core code)
│   └── data/                         # JSON data files
└── tests/                            # unittest test suite
```

## Architecture

### Scripts (the core analysis code)

All scripts live in `plugins/ft-review-toolkit/scripts/`. Every analysis script follows the same pattern: parse C/C++ files with Tree-sitter, find candidate issues, output JSON to stdout.

| Script | Purpose |
|--------|---------|
| `tree_sitter_utils.py` | Vendored core parsing module from cext-review-toolkit |
| `scan_common.py` | Vendored shared utilities from cext-review-toolkit |
| `scan_shared_state.py` | Global/static shared mutable state detection |
| `scan_unsafe_apis.py` | Thread-unsafe Python/C API usage detection |
| `analyze_ft_history.py` | Git history analysis for free-threading commits |

**Script calling convention:** Every analysis script exposes `analyze(target: str, *, max_files: int = 0) -> dict` returning a JSON envelope with `{project_root, scan_root, files_analyzed, functions_analyzed, findings, summary, skipped_files}`. Exception: `analyze_ft_history.py` takes `argv` (matching cext-review-toolkit convention).

### Classification system

Every finding is tagged with a classification and severity:

Classifications: **RACE** (data race), **UNSAFE** (operation unsafe without GIL), **PROTECT** (shared state needs protection), **MIGRATE** (structural change needed), **SAFE** (confirmed safe)

Severities: **CRITICAL**, **HIGH**, **MEDIUM**, **LOW**

### Agents

Markdown files in `plugins/ft-review-toolkit/agents/`. YAML frontmatter with `name`, `description` (with `<example>` tags), `model: opus`, `color`. Body has 3 phases: run scanner, triage findings, pattern review beyond script.

## Testing notes
- All tests use `unittest` — never pytest
- Test helper in `tests/helpers.py`: `TempExtension` context manager, `import_script()` loader
- Tests create temporary directories with C files, run scripts on them, and check JSON output
- `import_script(name)` loads scripts from `plugins/ft-review-toolkit/scripts/` via importlib

## Gotchas
- **`sys.path.insert` for imports:** Scripts use `sys.path.insert(0, str(Path(__file__).resolve().parent))` to import vendored modules. This is intentional.
- **`parse_bytes_for_file` vs `parse_bytes`:** Always use `parse_bytes_for_file(source_bytes, filepath)` which auto-selects C or C++ parser by extension.
- **C++ parsing is optional:** All scripts must work without tree-sitter-cpp. Use `is_cpp_available()` to gate C++ features.
- **`analyze_ft_history.py` has a different `analyze()` signature:** Takes `argv` list instead of `(target, max_files)`.

## Design document
`ft-review-toolkit-design.md` at the repo root is the authoritative design reference.
