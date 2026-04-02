#!/usr/bin/env python3
"""Scan C extensions for global/static shared mutable state.

Finds thread-safety concerns for free-threaded Python: unprotected global
PyObject* variables, non-atomic shared flags, static PyTypeObject definitions,
module state stored in globals, and singleton patterns.

Usage:
    python scan_shared_state.py /path/to/extension
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

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

_INIT_FUNCTION_PATTERNS = re.compile(
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

_LOCK_APIS: set[str] = set()


def _load_lock_apis() -> set[str]:
    """Load lock acquire/release API names from data file."""
    global _LOCK_APIS
    if _LOCK_APIS:
        return _LOCK_APIS
    try:
        with open(_DATA_DIR / "lock_macros.json", encoding="utf-8") as f:
            data = json.load(f)
        _LOCK_APIS = set(data.get("all_acquire_macros", []))
        _LOCK_APIS |= set(data.get("all_release_macros", []))
    except (OSError, json.JSONDecodeError):
        pass
    return _LOCK_APIS


def _is_thread_local(decl_type: str, source_line: str) -> bool:
    """Check if a declaration uses thread-local storage."""
    for kw in _THREAD_LOCAL_KEYWORDS:
        if kw in decl_type or kw in source_line:
            return True
    return False


def _is_init_function(name: str) -> bool:
    """Check if a function name looks like a module init function."""
    return bool(_INIT_FUNCTION_PATTERNS.match(name))


def _classify_declaration(decl: dict) -> str:
    """Classify a static declaration by its type.

    Returns one of: pyobject, type_object, primitive, struct, function_ptr, other.
    """
    decl_type = decl["type"]
    if "PyTypeObject" in decl_type:
        return "type_object"
    if decl["is_pyobject"]:
        return "pyobject"
    if "(*" in decl_type or "( *" in decl_type:
        return "function_ptr"
    # Primitive types
    for prim in (
        "int",
        "long",
        "short",
        "char",
        "bool",
        "size_t",
        "ssize_t",
        "unsigned",
        "float",
        "double",
        "Py_ssize_t",
    ):
        if prim in decl_type.split():
            return "primitive"
    if decl["is_pointer"]:
        return "pointer"
    return "other"


def _find_write_locations(
    var_name: str, functions: list[dict], source_bytes: bytes
) -> list[dict]:
    """Find all assignments to var_name across all functions."""
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
    return writes


def _check_module_def(tree, source_bytes: bytes) -> list[dict]:
    """Check PyModuleDef for m_size indicating global state usage."""
    findings = []
    from tree_sitter_utils import extract_struct_initializers

    module_defs = extract_struct_initializers(tree, source_bytes, "PyModuleDef")
    for mod_def in module_defs:
        init_text = strip_comments(mod_def["initializer_text"])
        # m_size is the 4th field in PyModuleDef (after HEAD_INIT, name, doc)
        # A value of -1 means no per-module state; 0 means minimal
        if "-1" in init_text:
            findings.append(
                {
                    "type": "module_state_in_globals",
                    "variable": mod_def["variable_name"],
                    "line": mod_def["start_line"],
                    "classification": "MIGRATE",
                    "severity": "MEDIUM",
                    "confidence": "high",
                    "detail": (
                        f"PyModuleDef '{mod_def['variable_name']}' has m_size = -1 "
                        f"(no per-module state). All module state lives in globals, "
                        f"blocking safe free-threading and subinterpreter support."
                    ),
                }
            )
    return findings


def _is_protected_by_lock(
    var_name: str,
    func: dict,
    source_bytes: bytes,  # noqa: ARG001
) -> bool:
    """Check if accesses to var_name in func are inside a lock region."""
    lock_apis = _load_lock_apis()
    if not lock_apis:
        return False
    body_text = strip_comments(func["body"])
    # Simple heuristic: if the function contains any lock acquire call
    # and references the variable, consider it potentially protected.
    has_lock = False
    for api in lock_apis:
        if api in body_text:
            has_lock = True
            break
    return has_lock and var_name in body_text


def _analyze_file(filepath: Path, source_bytes: bytes) -> list[dict]:
    """Analyze a single file for shared state issues."""
    findings = []
    tree = parse_bytes_for_file(source_bytes, filepath)
    rel_path = filepath.name

    # Extract all static declarations and functions.
    static_decls = extract_static_declarations(tree, source_bytes)
    functions = extract_functions(tree, source_bytes)

    # Check PyModuleDef.
    findings.extend(_check_module_def(tree, source_bytes))
    for f in findings:
        f["file"] = rel_path

    # Analyze each static declaration.
    for decl in static_decls:
        source_line = (
            source_bytes.decode("utf-8", errors="replace").splitlines()[
                decl["start_line"] - 1
            ]
            if decl["start_line"]
            <= len(source_bytes.decode("utf-8", errors="replace").splitlines())
            else ""
        )

        # Skip thread-local variables.
        if _is_thread_local(decl["type"], source_line):
            findings.append(
                {
                    "type": "thread_local_safe",
                    "file": rel_path,
                    "variable": decl["name"],
                    "var_type": decl["type"],
                    "line": decl["start_line"],
                    "classification": "SAFE",
                    "severity": "LOW",
                    "confidence": "high",
                    "detail": f"Thread-local variable '{decl['name']}' — safe for free-threading.",
                }
            )
            continue

        # Skip const declarations.
        if decl["is_const"]:
            continue

        # Skip function pointer declarations (callbacks, method tables).
        decl_class = _classify_declaration(decl)
        if decl_class == "function_ptr":
            continue

        # Find all writes to this variable.
        writes = _find_write_locations(decl["name"], functions, source_bytes)
        init_only = all(w["is_init_function"] for w in writes) if writes else False
        has_non_init_writes = any(not w["is_init_function"] for w in writes)

        if decl_class == "type_object":
            findings.append(
                {
                    "type": "static_type_object",
                    "file": rel_path,
                    "variable": decl["name"],
                    "var_type": decl["type"],
                    "line": decl["start_line"],
                    "classification": "MIGRATE",
                    "severity": "MEDIUM",
                    "confidence": "high",
                    "detail": (
                        f"Static PyTypeObject '{decl['name']}' is shared across all "
                        f"threads and interpreters. Under free-threading, mutations to "
                        f"tp_dict and other internal fields cause data races. "
                        f"Convert to heap type via PyType_FromSpec."
                    ),
                }
            )

        elif decl_class == "pyobject":
            severity = "HIGH" if has_non_init_writes else "MEDIUM"
            classification = "PROTECT" if has_non_init_writes else "PROTECT"

            if init_only and writes:
                severity = "LOW"
                classification = "PROTECT"
                detail = (
                    f"Global PyObject* '{decl['name']}' is written only during "
                    f"module init. Likely safe if truly write-once-read-many, "
                    f"but should be moved to module state for subinterpreter "
                    f"safety and free-threading correctness."
                )
            elif has_non_init_writes:
                detail = (
                    f"Global PyObject* '{decl['name']}' is written outside "
                    f"init functions (at lines "
                    f"{', '.join(str(w['line']) for w in writes if not w['is_init_function'])}). "
                    f"This is a data race under free-threading."
                )
            else:
                detail = (
                    f"Global PyObject* '{decl['name']}' — no writes found in "
                    f"analyzed functions. Verify it is truly immutable after init."
                )

            # Check if any write is inside a lock.
            protected = False
            for func in functions:
                if _is_protected_by_lock(decl["name"], func, source_bytes):
                    protected = True
                    break

            if protected and has_non_init_writes:
                severity = "LOW"
                detail += " Some accesses appear to be lock-protected."

            findings.append(
                {
                    "type": "unprotected_global_pyobject",
                    "file": rel_path,
                    "variable": decl["name"],
                    "var_type": decl["type"],
                    "line": decl["start_line"],
                    "classification": classification,
                    "severity": severity,
                    "confidence": "high" if has_non_init_writes else "medium",
                    "detail": detail,
                    "write_locations": writes,
                    "init_only": init_only,
                    "lock_protected": protected,
                }
            )

        elif decl_class == "primitive":
            # Non-atomic shared primitive — potential race.
            if has_non_init_writes:
                findings.append(
                    {
                        "type": "non_atomic_shared_flag",
                        "file": rel_path,
                        "variable": decl["name"],
                        "var_type": decl["type"],
                        "line": decl["start_line"],
                        "classification": "PROTECT",
                        "severity": "HIGH",
                        "confidence": "high",
                        "detail": (
                            f"Non-atomic static variable '{decl['name']}' (type: "
                            f"{decl['type']}) is written outside init functions. "
                            f"Under free-threading, concurrent read/write is undefined "
                            f"behavior. Use _Py_atomic_int or std::atomic."
                        ),
                        "write_locations": writes,
                    }
                )
            elif writes:
                # Init-only writes — lower concern.
                findings.append(
                    {
                        "type": "non_atomic_shared_flag",
                        "file": rel_path,
                        "variable": decl["name"],
                        "var_type": decl["type"],
                        "line": decl["start_line"],
                        "classification": "SAFE",
                        "severity": "LOW",
                        "confidence": "medium",
                        "detail": (
                            f"Static variable '{decl['name']}' (type: {decl['type']}) "
                            f"is written only during init. Likely safe if read-only "
                            f"after initialization."
                        ),
                        "write_locations": writes,
                    }
                )

        elif decl_class == "pointer":
            # Non-PyObject pointer — could be a singleton or shared resource.
            if has_non_init_writes:
                findings.append(
                    {
                        "type": "unprotected_singleton",
                        "file": rel_path,
                        "variable": decl["name"],
                        "var_type": decl["type"],
                        "line": decl["start_line"],
                        "classification": "PROTECT",
                        "severity": "MEDIUM",
                        "confidence": "medium",
                        "detail": (
                            f"Static pointer '{decl['name']}' (type: {decl['type']}) "
                            f"is written outside init functions. If this is a "
                            f"singleton or shared resource, it needs protection "
                            f"under free-threading."
                        ),
                        "write_locations": writes,
                    }
                )

    return findings


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Analyze C extension for shared mutable state.

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

        # Add relative path to findings.
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
