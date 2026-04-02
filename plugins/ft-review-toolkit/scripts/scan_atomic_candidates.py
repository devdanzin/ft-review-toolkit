#!/usr/bin/env python3
"""Scan C extensions for variables that should use atomic operations.

Finds: shared bool/int flags, counter variables, pointer-width values that
are read and written across functions without atomic operations.

Usage:
    python scan_atomic_candidates.py /path/to/extension
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scan_common import discover_c_files, find_project_root, parse_common_args
from tree_sitter_utils import (
    extract_functions,
    extract_static_declarations,
    find_assignments_in_scope,
    parse_bytes_for_file,
    strip_comments,
)

_ATOMIC_TYPE_PATTERNS = re.compile(
    r"_Py_atomic_\w+|std::atomic|_Atomic\b|atomic_\w+", re.IGNORECASE
)

_THREAD_LOCAL_KEYWORDS = frozenset(
    {
        "__thread",
        "_Thread_local",
        "thread_local",
        "_Py_thread_local",
    }
)

_INIT_FUNCTION_RE = re.compile(
    r"^(PyInit_\w+|PyMODINIT_FUNC|module_init|init_\w+|_init\w*|exec_\w+)$"
)

_PRIMITIVE_TYPES = frozenset(
    {
        "int",
        "long",
        "short",
        "char",
        "bool",
        "unsigned",
        "size_t",
        "ssize_t",
        "Py_ssize_t",
        "uint8_t",
        "uint16_t",
        "uint32_t",
        "uint64_t",
        "int8_t",
        "int16_t",
        "int32_t",
        "int64_t",
        "float",
        "double",
    }
)


def _is_atomic_type(decl_type: str) -> bool:
    """Check if a type is already an atomic type."""
    return bool(_ATOMIC_TYPE_PATTERNS.search(decl_type))


def _is_thread_local(decl_type: str, source_line: str) -> bool:
    """Check if a declaration uses thread-local storage."""
    for kw in _THREAD_LOCAL_KEYWORDS:
        if kw in decl_type or kw in source_line:
            return True
    return False


def _is_init_function(name: str) -> bool:
    """Check if a function name looks like a module init function."""
    return bool(_INIT_FUNCTION_RE.match(name))


def _is_primitive_type(decl_type: str) -> bool:
    """Check if the type is a primitive scalar type."""
    words = decl_type.replace("static", "").replace("volatile", "").split()
    return any(w in _PRIMITIVE_TYPES for w in words)


def _find_read_locations(
    var_name: str, functions: list[dict], source_bytes: bytes
) -> list[dict]:
    """Find all read locations of a variable across functions."""
    reads = []
    for func in functions:
        body_text = strip_comments(func["body"])
        # Find references to var_name that aren't assignments (left side).
        # Simple heuristic: any occurrence not immediately followed by '='
        # (but not '==').
        for m in re.finditer(rf"\b{re.escape(var_name)}\b", body_text):
            # Check if this is a write (assignment).
            after = body_text[m.end() : m.end() + 5].lstrip()
            if after.startswith("=") and not after.startswith("=="):
                continue
            # Check if this is a declaration.
            before = body_text[max(0, m.start() - 20) : m.start()]
            if any(
                kw in before
                for kw in ("int ", "bool ", "char ", "long ", "static ", "unsigned ")
            ):
                continue
            line_offset = body_text[: m.start()].count("\n")
            reads.append(
                {
                    "function": func["name"],
                    "line": func["start_line"] + line_offset,
                    "is_init_function": _is_init_function(func["name"]),
                }
            )
    return reads


def _find_write_locations(
    var_name: str, functions: list[dict], source_bytes: bytes
) -> list[dict]:
    """Find all write locations of a variable across functions."""
    writes = []
    for func in functions:
        body_node = func["body_node"]
        assignments = find_assignments_in_scope(body_node, source_bytes, var_name)
        for assign in assignments:
            writes.append(
                {
                    "function": func["name"],
                    "line": assign["start_line"],
                    "is_init_function": _is_init_function(func["name"]),
                }
            )

        # Also check for increment/decrement (++/--)
        body_text = func["body"]
        for m in re.finditer(
            rf"(?:{re.escape(var_name)}\s*(?:\+\+|--)|(?:\+\+|--)\s*{re.escape(var_name)})",
            body_text,
        ):
            line_offset = body_text[: m.start()].count("\n")
            writes.append(
                {
                    "function": func["name"],
                    "line": func["start_line"] + line_offset,
                    "is_init_function": _is_init_function(func["name"]),
                }
            )

    return writes


def _suggest_atomic_type(decl_type: str) -> str:
    """Suggest the appropriate atomic type for a given C type."""
    type_lower = decl_type.lower()
    if "bool" in type_lower:
        return "_Py_atomic_int (or std::atomic<bool> for C++)"
    if "int" in type_lower or "long" in type_lower:
        return "_Py_atomic_int (or std::atomic<int> for C++)"
    if "*" in decl_type:
        return "_Py_atomic_address (or std::atomic<void*> for C++)"
    return "_Py_atomic_int"


def _analyze_file(filepath: Path, source_bytes: bytes) -> list[dict]:
    """Analyze a single file for atomic candidate variables."""
    findings = []
    tree = parse_bytes_for_file(source_bytes, filepath)
    rel_path = filepath.name

    static_decls = extract_static_declarations(tree, source_bytes)
    functions = extract_functions(tree, source_bytes)
    source_text = source_bytes.decode("utf-8", errors="replace")
    lines = source_text.splitlines()

    for decl in static_decls:
        # Skip const declarations.
        if decl["is_const"]:
            continue

        source_line = (
            lines[decl["start_line"] - 1] if decl["start_line"] <= len(lines) else ""
        )

        # Skip thread-local variables.
        if _is_thread_local(decl["type"], source_line):
            continue

        # Check if already atomic.
        if _is_atomic_type(decl["type"]):
            findings.append(
                {
                    "type": "existing_atomic_ok",
                    "file": rel_path,
                    "variable": decl["name"],
                    "var_type": decl["type"],
                    "line": decl["start_line"],
                    "classification": "SAFE",
                    "severity": "LOW",
                    "confidence": "high",
                    "detail": (
                        f"Variable '{decl['name']}' already uses atomic type "
                        f"({decl['type']}). Verify correct memory ordering."
                    ),
                }
            )
            continue

        # Only check primitive types and pointers.
        is_primitive = _is_primitive_type(decl["type"])
        is_pointer = decl["is_pointer"] and not decl["is_pyobject"]

        if not is_primitive and not is_pointer:
            continue

        # Skip PyObject* — handled by shared-state-auditor.
        if decl["is_pyobject"]:
            continue

        # Find reads and writes.
        writes = _find_write_locations(decl["name"], functions, source_bytes)
        reads = _find_read_locations(decl["name"], functions, source_bytes)

        has_non_init_writes = any(not w["is_init_function"] for w in writes)
        init_only = all(w["is_init_function"] for w in writes) if writes else False

        # Multiple functions reading and writing = atomic candidate.
        write_functions = {w["function"] for w in writes}
        read_functions = {r["function"] for r in reads}
        all_functions = write_functions | read_functions

        if not writes:
            continue

        if init_only and not has_non_init_writes:
            # Written only during init — lower concern.
            if len(read_functions) > 1:
                findings.append(
                    {
                        "type": f"non_atomic_shared_{'bool' if 'bool' in decl['type'] else 'int' if is_primitive else 'pointer'}",
                        "file": rel_path,
                        "variable": decl["name"],
                        "var_type": decl["type"],
                        "line": decl["start_line"],
                        "classification": "SAFE",
                        "severity": "LOW",
                        "confidence": "medium",
                        "detail": (
                            f"Static variable '{decl['name']}' ({decl['type']}) is "
                            f"written only during init but read by {len(read_functions)} "
                            f"functions. Likely safe if init completes before any reads."
                        ),
                        "write_locations": writes,
                        "read_locations": reads[:5],
                        "suggested_atomic": _suggest_atomic_type(decl["type"]),
                    }
                )
            continue

        if has_non_init_writes:
            # Determine finding type based on variable type.
            if "bool" in decl["type"].lower():
                finding_type = "non_atomic_shared_bool"
            elif is_pointer:
                finding_type = "non_atomic_shared_pointer"
            else:
                finding_type = "non_atomic_shared_int"

            severity = "HIGH" if len(all_functions) > 2 else "MEDIUM"

            findings.append(
                {
                    "type": finding_type,
                    "file": rel_path,
                    "variable": decl["name"],
                    "var_type": decl["type"],
                    "line": decl["start_line"],
                    "classification": "PROTECT",
                    "severity": severity,
                    "confidence": "high" if len(all_functions) > 2 else "medium",
                    "detail": (
                        f"Non-atomic static variable '{decl['name']}' ({decl['type']}) "
                        f"is written by {len(write_functions)} function(s) and read by "
                        f"{len(read_functions)} function(s). Under free-threading, "
                        f"concurrent read/write is undefined behavior. Use "
                        f"{_suggest_atomic_type(decl['type'])}."
                    ),
                    "write_locations": writes,
                    "read_locations": reads[:5],
                    "write_functions": sorted(write_functions),
                    "read_functions": sorted(read_functions)[:5],
                    "suggested_atomic": _suggest_atomic_type(decl["type"]),
                }
            )

    return findings


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Analyze C extension for atomic candidate variables.

    Returns JSON envelope with findings.
    """
    target_path = Path(target).resolve()
    project_root = find_project_root(target_path)

    findings: list[dict] = []
    files_analyzed = 0
    functions_analyzed = 0
    skipped_files: list[str] = []

    for filepath in discover_c_files(target_path, max_files=max_files):
        try:
            source_bytes = filepath.read_bytes()
        except OSError as e:
            skipped_files.append(f"{filepath}: {e}")
            continue

        files_analyzed += 1
        tree = parse_bytes_for_file(source_bytes, filepath)
        functions = extract_functions(tree, source_bytes)
        functions_analyzed += len(functions)

        file_findings = _analyze_file(filepath, source_bytes)

        try:
            rel = str(filepath.relative_to(project_root))
        except ValueError:
            rel = str(filepath)

        for f in file_findings:
            f["file"] = rel

        findings.extend(file_findings)

    # Summary statistics.
    finding_types: dict[str, int] = {}
    classifications: dict[str, int] = {}
    for f in findings:
        finding_types[f["type"]] = finding_types.get(f["type"], 0) + 1
        classifications[f["classification"]] = (
            classifications.get(f["classification"], 0) + 1
        )

    return {
        "project_root": str(project_root),
        "scan_root": str(target_path),
        "files_analyzed": files_analyzed,
        "functions_analyzed": functions_analyzed,
        "findings": findings,
        "summary": {
            "total_findings": len(findings),
            "by_type": finding_types,
            "by_classification": classifications,
        },
        "skipped_files": skipped_files,
    }


def main() -> None:
    """CLI entry point."""
    target, max_files = parse_common_args(sys.argv[1:])
    try:
        result = analyze(target, max_files=max_files)
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    except Exception as e:
        json.dump({"error": str(e), "type": type(e).__name__}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
