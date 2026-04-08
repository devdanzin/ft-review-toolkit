#!/usr/bin/env python3
# Vendored from cext-review-toolkit, upstream commit 1475ac6
# https://github.com/devdanzin/cext-review-toolkit
# ft-review-toolkit additions: is_thread_local, is_init_function, is_in_region,
# extract_nearby_comments, has_safety_annotation, make_finding
"""Shared utilities for cext-review-toolkit analysis scripts.

Provides common infrastructure: project root detection, C file discovery,
API table loading, and AST helpers used across multiple scanner scripts.
"""

import json
import re
import sys
from collections.abc import Generator
from pathlib import Path

try:
    import tree_sitter  # noqa: F401
    import tree_sitter_c  # noqa: F401
except ImportError:
    print(
        json.dumps(
            {
                "error": "tree-sitter not installed",
                "install": "pip install tree-sitter tree-sitter-c",
            }
        )
    )
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tree_sitter_utils import (
    get_node_text,
    get_declarator_name,
    C_EXTENSIONS,
    ALL_SOURCE_EXTENSIONS,
    is_cpp_available,
)


EXCLUDE_DIRS = frozenset(
    {
        ".git",
        ".tox",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        "build",
        "dist",
        ".eggs",
        "egg-info",
    }
)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

PYARG_PARSE_APIS = frozenset(
    {
        "PyArg_ParseTuple",
        "PyArg_ParseTupleAndKeywords",
        "PyArg_Parse",
        "PyArg_UnpackTuple",
        "PyArg_VaParse",
        "PyArg_VaParseTupleAndKeywords",
    }
)


def find_project_root(start: Path) -> Path:
    """Find project root by looking for common project markers."""
    current = start if start.is_dir() else start.parent
    for _ in range(20):
        for marker in (".git", "pyproject.toml", "setup.py", "setup.cfg"):
            if (current / marker).exists():
                return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return start if start.is_dir() else start.parent


def _get_source_extensions() -> frozenset[str]:
    """Return file extensions to scan (C only, or C+C++ if available)."""
    return ALL_SOURCE_EXTENSIONS if is_cpp_available() else C_EXTENSIONS


def discover_c_files(
    root: Path,
    *,
    max_files: int = 0,
) -> Generator[Path, None, None]:
    """Discover C/C++ source files under root, excluding common build dirs."""
    exts = _get_source_extensions()
    count = 0
    if root.is_file():
        if root.suffix in exts:
            yield root
        return
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix not in exts:
            continue
        try:
            parts = set(p.relative_to(root).parts)
        except ValueError:
            continue
        if parts & EXCLUDE_DIRS:
            continue
        yield p
        count += 1
        if max_files and count >= max_files:
            return


def load_api_tables() -> dict:
    """Load API classification tables from the data directory."""
    try:
        with open(_DATA_DIR / "api_tables.json", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(json.dumps({"error": f"Failed to load api_tables.json: {e}"}))
        sys.exit(1)


def find_assigned_variable(call_node, source_bytes: bytes) -> str | None:
    """Find the variable a call result is assigned to."""
    node = call_node.parent
    while node:
        if node.type == "init_declarator":
            decl = node.child_by_field_name("declarator")
            if decl:
                return get_declarator_name(decl, source_bytes)
        if node.type == "assignment_expression":
            left = node.child_by_field_name("left")
            if left:
                return get_node_text(left, source_bytes)
        # Skip past macro wrappers (ALL_CAPS function calls that wrap assignments)
        if node.type == "call_expression":
            func = node.child_by_field_name("function")
            if func:
                func_text = get_node_text(func, source_bytes)
                if func_text.isupper():
                    node = node.parent
                    continue
        if node.type in ("expression_statement", "declaration", "compound_statement"):
            break
        node = node.parent
    return None


_INIT_FUNCTION_RE = re.compile(
    r"^(PyInit_\w+|PyMODINIT_FUNC|module_init|init_\w+|_init\w*|exec_\w+)$"
)

_THREAD_LOCAL_KEYWORDS = frozenset(
    {
        "__thread",
        "_Thread_local",
        "thread_local",
        "_Py_thread_local",
    }
)


def is_thread_local(decl_type: str, source_line: str) -> bool:
    """Check if a declaration uses thread-local storage."""
    return any(kw in decl_type or kw in source_line for kw in _THREAD_LOCAL_KEYWORDS)


def is_init_function(name: str) -> bool:
    """Check if a function name looks like a module init function."""
    return bool(_INIT_FUNCTION_RE.match(name))


def is_in_region(offset: int, regions: list[tuple[int, int]]) -> bool:
    """Check if a byte offset falls within any of the given regions."""
    return any(start <= offset < end for start, end in regions)


def extract_nearby_comments(
    source_bytes: bytes, tree: object, line: int, radius: int = 5
) -> list[str]:
    """Extract comments within ±radius lines of the given line.

    Uses Tree-sitter to find comment nodes. Returns list of comment
    text strings (stripped of comment markers).
    """
    comments: list[str] = []
    min_line = max(0, line - radius - 1)  # 0-indexed
    max_line = line + radius - 1

    def _walk(node: object) -> None:
        if node.type == "comment":  # type: ignore[attr-defined]
            node_line = node.start_point[0]  # type: ignore[attr-defined]
            if min_line <= node_line <= max_line:
                text = source_bytes[
                    node.start_byte : node.end_byte  # type: ignore[attr-defined]
                ].decode("utf-8", errors="replace")
                comments.append(text)
        for child in node.children:  # type: ignore[attr-defined]
            _walk(child)

    _walk(tree.root_node)  # type: ignore[attr-defined]
    return comments


_SAFETY_KEYWORDS = {
    "safety:",
    "safe because",
    "intentional",
    "by design",
    "nolint",
    "checked:",
    "correct because",
    "this is safe",
    "not a bug",
    "deliberately",
    "expected",
    "thread-safe",
    "already protected",
    "mutex held",
    "lock held",
}


def has_safety_annotation(comments: list[str]) -> bool:
    """Check if any comment contains a safety annotation keyword."""
    for comment in comments:
        lower = comment.lower()
        if any(kw in lower for kw in _SAFETY_KEYWORDS):
            return True
    return False


def make_finding(
    finding_type: str,
    *,
    function: str = "",
    line: int = 0,
    classification: str,
    severity: str,
    confidence: str = "high",
    detail: str,
    **extra: object,
) -> dict:
    """Create a finding dict with consistent key naming.

    All scanners produce findings as dicts with a common structure.
    This helper ensures consistent keys and allows scanner-specific
    extra fields via **extra.
    """
    finding: dict = {
        "type": finding_type,
        "function": function,
        "line": line,
        "classification": classification,
        "severity": severity,
        "confidence": confidence,
        "detail": detail,
    }
    finding.update(extra)
    return finding


def parse_common_args(argv: list[str]) -> tuple[str, int]:
    """Parse common CLI arguments (path and --max-files).

    Returns (target_path, max_files).
    """
    max_files = 0
    positional: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--max-files" and i + 1 < len(argv):
            try:
                max_files = int(argv[i + 1])
            except ValueError:
                print(
                    json.dumps(
                        {
                            "error": f"--max-files requires an integer, got '{argv[i + 1]}'"
                        }
                    )
                )
                sys.exit(2)
            i += 2
        elif argv[i].startswith("--"):
            i += 1
        else:
            positional.append(argv[i])
            i += 1
    target = positional[0] if positional else "."
    return target, max_files
