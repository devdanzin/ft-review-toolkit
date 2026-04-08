#!/usr/bin/env python3
"""Scan C extensions for thread-unsafe Python/C API usage.

Finds: Python API calls in GIL-released sections, borrowed references without
protection, container mutations on shared objects, PyGILState no-ops under
free-threading, and deprecated thread-unsafe APIs.

Usage:
    python scan_unsafe_apis.py /path/to/extension
"""

import json
import re
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scan_common import (
    discover_c_files,
    find_project_root,
    is_in_region,
    parse_common_args,
)
from tree_sitter_utils import (
    extract_functions,
    extract_static_declarations,
    find_calls_in_scope,
    parse_bytes_for_file,
)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

_thread_safe_data: dict | None = None


def _load_thread_safe_apis() -> dict:
    """Load thread safety API classifications from data file."""
    global _thread_safe_data
    if _thread_safe_data is not None:
        return _thread_safe_data
    try:
        with open(_DATA_DIR / "thread_safe_apis.json", encoding="utf-8") as f:
            _thread_safe_data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(
            f"Warning: failed to load thread_safe_apis.json: {e}",
            file=sys.stderr,
        )
        _thread_safe_data = {}
    return _thread_safe_data


def _find_gil_released_regions(body_text: str) -> list[tuple[int, int]]:
    """Find byte offset ranges between Py_BEGIN_ALLOW_THREADS and Py_END_ALLOW_THREADS.

    Returns list of (start_offset, end_offset) tuples within the function body text.
    """
    regions = []
    begin_re = re.compile(r"Py_BEGIN_ALLOW_THREADS")
    end_re = re.compile(r"Py_END_ALLOW_THREADS")

    begins = list(begin_re.finditer(body_text))
    ends = list(end_re.finditer(body_text))

    for b in begins:
        matching_end = None
        for e in ends:
            if e.start() > b.end():
                matching_end = e
                break
        if matching_end:
            regions.append((b.end(), matching_end.start()))

    return regions


_is_in_region = is_in_region


def _check_api_in_gil_released(func: dict, source_bytes: bytes) -> list[dict]:
    """Check for Python API calls inside GIL-released regions."""
    findings = []
    body_text = func["body"]
    regions = _find_gil_released_regions(body_text)
    if not regions:
        return findings

    # Search for Py* and _Py* calls in GIL-released regions.
    py_calls = re.finditer(r"\b(Py\w+|_Py\w+)\s*\(", body_text)
    for m in py_calls:
        api_name = m.group(1)
        if api_name in ("Py_BEGIN_ALLOW_THREADS", "Py_END_ALLOW_THREADS"):
            continue
        if not _is_in_region(m.start(), regions):
            continue

        data = _load_thread_safe_apis()
        safe_apis = set(data.get("safe_without_gil", []))
        if api_name in safe_apis:
            continue

        line_offset = body_text[: m.start()].count("\n")
        findings.append(
            {
                "type": "unsafe_api_without_gil",
                "function": func["name"],
                "line": func["start_line"] + line_offset,
                "classification": "UNSAFE",
                "severity": "CRITICAL",
                "confidence": "high",
                "detail": (
                    f"Python API call {api_name}() in GIL-released region "
                    f"(between Py_BEGIN/END_ALLOW_THREADS) in function "
                    f"'{func['name']}'. This is undefined behavior."
                ),
                "api_call": api_name,
            }
        )

    return findings


def _check_borrowed_ref_unprotected(func: dict, source_bytes: bytes) -> list[dict]:
    """Check for borrowed reference APIs without immediate Py_INCREF."""
    findings = []
    data = _load_thread_safe_apis()
    borrowed_apis = data.get("borrowed_ref_apis", {})
    if not borrowed_apis:
        return findings

    api_names = set(borrowed_apis.keys())
    body_node = func["body_node"]
    calls = find_calls_in_scope(body_node, source_bytes, api_names)

    for call in calls:
        api_name = call["function_name"]
        api_info = borrowed_apis[api_name]
        safe_replacement = api_info.get("safe_replacement")

        # Check if there's a Py_INCREF/Py_XINCREF/Py_NewRef nearby.
        # Look at the next few lines after the call.
        body_text = func["body"]
        call_offset = call["start_byte"] - func["body_node"].start_byte
        if call_offset < 0:
            continue

        # Get next ~200 chars after the call.
        after_call = body_text[call_offset : call_offset + 200]
        lines_after = after_call.split("\n")[:3]
        near_text = "\n".join(lines_after)

        has_incref = bool(re.search(r"Py_(?:X?INCREF|NewRef|XNewRef)\s*\(", near_text))

        if has_incref:
            continue

        detail = (
            f"Borrowed reference from {api_name}() in '{func['name']}' "
            f"without immediate Py_INCREF. Under free-threading, another "
            f"thread could invalidate the borrowed reference at any time."
        )
        if safe_replacement:
            detail += (
                f" Use {safe_replacement}() instead "
                f"(available since Python {api_info.get('min_version', '3.13')})."
            )

        findings.append(
            {
                "type": "borrowed_ref_unprotected",
                "function": func["name"],
                "line": call["start_line"],
                "classification": "UNSAFE",
                "severity": "HIGH",
                "confidence": "medium",
                "detail": detail,
                "api_call": api_name,
                "safe_replacement": safe_replacement,
            }
        )

    return findings


def _check_container_mutation_unprotected(
    func: dict, source_bytes: bytes, global_vars: set[str]
) -> list[dict]:
    """Check for container mutation APIs on potentially shared containers."""
    findings = []
    data = _load_thread_safe_apis()
    mutation_apis = set(data.get("container_mutation_apis", []))
    if not mutation_apis:
        return findings

    body_node = func["body_node"]
    calls = find_calls_in_scope(body_node, source_bytes, mutation_apis)

    for call in calls:
        # Check if the first argument is a global variable.
        args_text = call["arguments_text"]
        first_arg = args_text.split(",")[0].strip() if args_text else ""

        is_global = first_arg in global_vars
        if not is_global:
            continue

        findings.append(
            {
                "type": "container_mutation_unprotected",
                "function": func["name"],
                "line": call["start_line"],
                "classification": "RACE",
                "severity": "HIGH",
                "confidence": "medium",
                "detail": (
                    f"Container mutation {call['function_name']}({first_arg}, ...) "
                    f"in '{func['name']}' — '{first_arg}' is a global variable. "
                    f"Under free-threading, concurrent mutations to shared "
                    f"containers cause data races and crashes. Protect with "
                    f"Py_BEGIN_CRITICAL_SECTION or move to per-module state."
                ),
                "api_call": call["function_name"],
                "container": first_arg,
            }
        )

    return findings


def _check_gilstate_noop(func: dict, source_bytes: bytes) -> list[dict]:
    """Check for PyGILState_Ensure/Release usage (no-ops under free-threading)."""
    findings = []
    data = _load_thread_safe_apis()
    gilstate_apis = set(data.get("gilstate_noop_apis", []))

    body_node = func["body_node"]
    calls = find_calls_in_scope(body_node, source_bytes, gilstate_apis)

    if calls:
        # Report once per function, not per call.
        findings.append(
            {
                "type": "gilstate_noop",
                "function": func["name"],
                "line": calls[0]["start_line"],
                "classification": "UNSAFE",
                "severity": "MEDIUM",
                "confidence": "high",
                "detail": (
                    f"PyGILState_Ensure/Release in '{func['name']}' — under "
                    f"free-threading, these are no-ops that provide no "
                    f"synchronization. If this code relies on GILState for "
                    f"thread safety, it needs real locks (PyMutex, "
                    f"Py_BEGIN_CRITICAL_SECTION, or pthread_mutex)."
                ),
                "api_calls": [c["function_name"] for c in calls],
            }
        )

    return findings


def _check_deprecated_thread_apis(func: dict, source_bytes: bytes) -> list[dict]:
    """Check for deprecated thread-unsafe APIs."""
    findings = []
    data = _load_thread_safe_apis()
    deprecated = data.get("deprecated_thread_apis", {})
    if not deprecated:
        return findings

    api_names = set(deprecated.keys())
    body_node = func["body_node"]
    calls = find_calls_in_scope(body_node, source_bytes, api_names)

    for call in calls:
        api_name = call["function_name"]
        info = deprecated[api_name]
        findings.append(
            {
                "type": "deprecated_thread_api",
                "function": func["name"],
                "line": call["start_line"],
                "classification": "MIGRATE",
                "severity": "MEDIUM",
                "confidence": "high",
                "detail": (
                    f"{api_name}() in '{func['name']}' — {info['reason']}. "
                    f"Use {info['replacement']}() instead."
                ),
                "api_call": api_name,
                "replacement": info["replacement"],
            }
        )

    return findings


def _analyze_file(filepath: Path, source_bytes: bytes) -> list[dict]:
    """Analyze a single file for unsafe API usage."""
    findings = []
    tree = parse_bytes_for_file(source_bytes, filepath)
    functions = extract_functions(tree, source_bytes)

    # Collect global variable names for container mutation check.
    static_decls = extract_static_declarations(tree, source_bytes)
    global_vars = {d["name"] for d in static_decls if d["is_pyobject"]}

    for func in functions:
        findings.extend(_check_api_in_gil_released(func, source_bytes))
        findings.extend(_check_borrowed_ref_unprotected(func, source_bytes))
        findings.extend(
            _check_container_mutation_unprotected(func, source_bytes, global_vars)
        )
        findings.extend(_check_gilstate_noop(func, source_bytes))
        findings.extend(_check_deprecated_thread_apis(func, source_bytes))

    return findings


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Analyze C extension for thread-unsafe API usage.

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
        print(traceback.format_exc(), file=sys.stderr)
        json.dump({"error": str(e), "type": type(e).__name__}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
