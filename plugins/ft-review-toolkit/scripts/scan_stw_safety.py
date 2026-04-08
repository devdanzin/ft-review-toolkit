#!/usr/bin/env python3
"""Scan C extensions for StopTheWorld safety violations.

Builds an intra-file call graph, classifies functions as STW-safe or
STW-unsafe (may invoke Python code), and detects unsafe operations
within _PyEval_StopTheWorld regions.

Usage:
    python scan_stw_safety.py /path/to/extension
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
    find_calls_in_scope,
    parse_bytes_for_file,
)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

_stw_data: dict | None = None


def _load_stw_apis() -> dict:
    """Load STW safety classifications from data file."""
    global _stw_data
    if _stw_data is not None:
        return _stw_data
    try:
        with open(_DATA_DIR / "stw_safe_apis.json", encoding="utf-8") as f:
            _stw_data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(
            f"Warning: failed to load stw_safe_apis.json: {e}",
            file=sys.stderr,
        )
        _stw_data = {}
    return _stw_data


def _get_safe_apis() -> set[str]:
    """Get all APIs classified as safe during STW.

    On 3.14+ free-threading builds, allocation APIs are also safe
    (GC runs only on eval breaker, not during allocation).
    Conditionally-safe exception APIs are treated as safe by the
    scanner (the "no exception set" precondition cannot be verified
    statically — the agent triages these).
    """
    data = _load_stw_apis()
    safe = set()
    for category in data.get("safe_during_stw", {}).values():
        if isinstance(category, list):
            safe.update(category)
    # On 3.14+, allocation APIs are safe during STW.
    alloc_314 = data.get("safe_allocation_on_314", {})
    safe_alloc = alloc_314.get("safe_if_builtin_types_only", [])
    if isinstance(safe_alloc, list):
        safe.update(safe_alloc)
    # Conditionally-safe exception APIs — scanner treats as safe,
    # agent triages the preconditions.
    exc = data.get("unsafe_during_stw", {}).get("exception_setting", {})
    cond_safe = exc.get("conditionally_safe_during_stw", {})
    if isinstance(cond_safe, dict):
        for api_name, condition in cond_safe.items():
            if not api_name.startswith("_"):
                safe.add(api_name)
    return safe


def _extract_apis_from_value(value: object) -> set[str]:
    """Extract API names from a data file value (list or dict with sub-lists)."""
    apis = set()
    if isinstance(value, list):
        apis.update(value)
    elif isinstance(value, dict):
        # Handle nested structures like exception_setting with sub-categories.
        for sub_val in value.values():
            if isinstance(sub_val, list):
                apis.update(sub_val)
            elif isinstance(sub_val, str):
                # Conditional entries like "PyErr_NoMemory": "Safe IF..."
                # The key itself is the API name.
                pass
        # Also extract keys from conditional entries.
        for sub_key, sub_val in value.items():
            if isinstance(sub_val, str) and not sub_key.startswith("_"):
                apis.add(sub_key)
    return apis


def _get_unsafe_apis() -> set[str]:
    """Get all APIs classified as unsafe during STW.

    Excludes APIs that are in the safe set (safe wins — e.g.,
    allocation APIs are safe on 3.14+ even though they were
    unsafe on older versions).
    """
    data = _load_stw_apis()
    unsafe = set()
    for category in data.get("unsafe_during_stw", {}).values():
        unsafe.update(_extract_apis_from_value(category))
    # Remove anything that's classified as safe (3.14+ overrides).
    safe = _get_safe_apis()
    unsafe -= safe
    return unsafe


def _get_unsafe_apis_for_propagation() -> set[str]:
    """Get unsafe APIs for call-graph propagation (excludes STW control)."""
    data = _load_stw_apis()
    unsafe = set()
    for cat_name, apis in data.get("unsafe_during_stw", {}).items():
        if cat_name != "stw_start":
            unsafe.update(_extract_apis_from_value(apis))
    return unsafe


def _get_unsafe_categories() -> dict[str, set[str]]:
    """Get unsafe APIs grouped by category."""
    data = _load_stw_apis()
    categories: dict[str, set[str]] = {}
    for cat_name, apis in data.get("unsafe_during_stw", {}).items():
        extracted = _extract_apis_from_value(apis)
        if extracted:
            categories[cat_name] = extracted
    return categories


def _find_stw_regions(body_text: str) -> list[tuple[int, int]]:
    """Find byte offset ranges between _PyEval_StopTheWorld and _PyEval_StartTheWorld.

    Returns list of (start_offset, end_offset) tuples within the function body text.
    """
    regions = []
    stop_re = re.compile(r"_PyEval_StopTheWorld(?:All)?\s*\(")
    start_re = re.compile(r"_PyEval_StartTheWorld(?:All)?\s*\(")

    stops = list(stop_re.finditer(body_text))
    starts = list(start_re.finditer(body_text))

    for s in stops:
        matching_start = None
        for st in starts:
            if st.start() > s.end():
                matching_start = st
                break
        if matching_start:
            regions.append((s.end(), matching_start.start()))

    return regions


_is_in_region = is_in_region


def _classify_call(func_name: str, safe_apis: set[str], unsafe_apis: set[str]) -> str:
    """Classify a function call as safe, unsafe, or unknown."""
    if func_name in safe_apis:
        return "safe"
    if func_name in unsafe_apis:
        return "unsafe"
    # Standard C library functions are safe.
    c_stdlib = {
        "memcpy",
        "memmove",
        "memset",
        "memcmp",
        "strlen",
        "strcmp",
        "strncmp",
        "strcpy",
        "strncpy",
        "printf",
        "fprintf",
        "sprintf",
        "snprintf",
        "malloc",
        "calloc",
        "realloc",
        "free",
        "assert",
        "sizeof",
        "offsetof",
        "abs",
        "labs",
    }
    if func_name in c_stdlib:
        return "safe"
    return "unknown"


def _build_call_graph(
    functions: list[dict], source_bytes: bytes
) -> dict[str, list[str]]:
    """Build a mapping of function_name → list of functions it calls."""
    graph: dict[str, list[str]] = {}
    for func in functions:
        body_node = func["body_node"]
        calls = find_calls_in_scope(body_node, source_bytes)
        called_names = [c["function_name"] for c in calls]
        graph[func["name"]] = called_names
    return graph


def _propagate_stw_safety(
    graph: dict[str, list[str]],
    safe_apis: set[str],
    unsafe_apis: set[str],
    internal_funcs: set[str],
) -> dict[str, str]:
    """Propagate STW safety through the call graph.

    Returns dict mapping function_name → "safe" | "unsafe" | "unknown".
    """
    classifications: dict[str, str] = {}

    def classify(func_name: str, visited: set[str]) -> str:
        if func_name in classifications:
            return classifications[func_name]
        if func_name in visited:
            # Recursive call — assume safe to break cycle.
            return "safe"

        visited.add(func_name)

        if func_name not in graph:
            # External function — classify by API tables.
            result = _classify_call(func_name, safe_apis, unsafe_apis)
            classifications[func_name] = result
            return result

        # Internal function — check all callees.
        result = "safe"
        for callee in graph[func_name]:
            callee_class = classify(callee, visited)
            if callee_class == "unsafe":
                result = "unsafe"
                break
            elif callee_class == "unknown" and result == "safe":
                result = "unknown"

        classifications[func_name] = result
        return result

    for func_name in graph:
        classify(func_name, set())

    return classifications


def _get_unsafe_reason(func_name: str, unsafe_categories: dict[str, set[str]]) -> str:
    """Get the category reason why an API is unsafe."""
    for cat_name, apis in unsafe_categories.items():
        if func_name in apis:
            return cat_name
    return "unknown"


def _check_stw_regions(
    func: dict,
    source_bytes: bytes,
    classifications: dict[str, str],
    safe_apis: set[str],
    unsafe_apis: set[str],
    unsafe_categories: dict[str, set[str]],
) -> list[dict]:
    """Check for unsafe calls within _PyEval_StopTheWorld regions."""
    findings = []
    body_text = func["body"]
    regions = _find_stw_regions(body_text)

    if not regions:
        return findings

    # Find all calls in the function body.
    body_node = func["body_node"]
    calls = find_calls_in_scope(body_node, source_bytes)

    # STW control functions are not violations themselves.
    stw_control = {
        "_PyEval_StopTheWorld",
        "_PyEval_StopTheWorldAll",
        "_PyEval_StartTheWorld",
        "_PyEval_StartTheWorldAll",
    }

    for call in calls:
        call_name = call["function_name"]
        if call_name in stw_control:
            continue
        # Calculate offset within the function body.
        call_offset = call["start_byte"] - func["body_node"].start_byte
        if call_offset < 0:
            continue

        if not _is_in_region(call_offset, regions):
            continue

        # This call is inside a STW region. Check propagated classifications
        # first (handles internal functions), then fall back to direct API
        # classification (handles external APIs not reached during propagation).
        call_class = classifications.get(call_name)
        if call_class is None:
            call_class = _classify_call(call_name, safe_apis, unsafe_apis)
        if call_class == "safe":
            continue

        if call_class == "unsafe":
            reason = _get_unsafe_reason(call_name, unsafe_categories)
            if not reason or reason == "unknown":
                # Check if it's an internal function that transitively invokes Python.
                reason = "transitively_invokes_python"

            finding_type = "stw_unsafe_call"
            if reason == "exception_setting":
                finding_type = "stw_exception_during_stw"
            elif reason == "may_trigger_gc_or_alloc":
                finding_type = "stw_allocation_during_stw"

            findings.append(
                {
                    "type": finding_type,
                    "function": func["name"],
                    "line": call["start_line"],
                    "classification": "RACE",
                    "severity": "CRITICAL",
                    "confidence": "high",
                    "detail": (
                        f"Unsafe call {call_name}() inside _PyEval_StopTheWorld "
                        f"region in '{func['name']}'. Category: {reason}. "
                        f"During STW, all other threads are suspended. This call "
                        f"may invoke Python code, trigger GC, or set exceptions, "
                        f"which can deadlock or corrupt interpreter state."
                    ),
                    "api_call": call_name,
                    "unsafe_reason": reason,
                }
            )

        elif call_class == "unknown":
            findings.append(
                {
                    "type": "stw_unknown_call",
                    "function": func["name"],
                    "line": call["start_line"],
                    "classification": "PROTECT",
                    "severity": "MEDIUM",
                    "confidence": "medium",
                    "detail": (
                        f"Unclassified call {call_name}() inside _PyEval_StopTheWorld "
                        f"region in '{func['name']}'. Cannot determine if this "
                        f"function may invoke Python code. Manual review needed."
                    ),
                    "api_call": call_name,
                }
            )

    return findings


def _analyze_file(
    filepath: Path, source_bytes: bytes
) -> tuple[list[dict], dict[str, str]]:
    """Analyze a single file for STW safety issues.

    Returns (findings, function_classifications).
    """
    findings = []
    tree = parse_bytes_for_file(source_bytes, filepath)
    functions = extract_functions(tree, source_bytes)

    safe_apis = _get_safe_apis()
    unsafe_apis = _get_unsafe_apis()
    unsafe_apis_for_prop = _get_unsafe_apis_for_propagation()
    unsafe_categories = _get_unsafe_categories()

    # Build call graph and propagate safety.
    # Use propagation-specific unsafe set (excludes STW control functions
    # so having STW in a function doesn't make it "invokes Python").
    graph = _build_call_graph(functions, source_bytes)
    classifications = _propagate_stw_safety(
        graph,
        safe_apis,
        unsafe_apis_for_prop,
        set(),
    )

    # Check each function for STW region violations.
    for func in functions:
        func_findings = _check_stw_regions(
            func,
            source_bytes,
            classifications,
            safe_apis,
            unsafe_apis,
            unsafe_categories,
        )
        findings.extend(func_findings)

    # Also report function classifications for functions that have STW regions.
    for func in functions:
        body_text = func["body"]
        if "_PyEval_StopTheWorld" in body_text:
            # Report the functions called during STW as context.
            pass

    return findings, classifications


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Analyze C extension for StopTheWorld safety issues.

    Returns JSON envelope with findings and function classifications.
    """
    target_path = Path(target).resolve()
    project_root = find_project_root(target_path)

    findings: list[dict] = []
    files_analyzed = 0
    functions_analyzed = 0
    skipped_files: list[str] = []
    all_classifications: dict[str, dict[str, str]] = {}
    stw_functions: list[dict] = []

    for filepath in discover_c_files(target_path, max_files=max_files):
        try:
            source_bytes = filepath.read_bytes()
        except OSError as e:
            skipped_files.append(f"{filepath}: {e}")
            continue

        # Only analyze files that use StopTheWorld.
        source_text = source_bytes.decode("utf-8", errors="replace")
        has_stw = "_PyEval_StopTheWorld" in source_text

        files_analyzed += 1
        tree = parse_bytes_for_file(source_bytes, filepath)
        functions = extract_functions(tree, source_bytes)
        functions_analyzed += len(functions)

        if not has_stw:
            continue

        file_findings, classifications = _analyze_file(filepath, source_bytes)

        try:
            rel = str(filepath.relative_to(project_root))
        except ValueError:
            rel = str(filepath)

        for f in file_findings:
            f["file"] = rel

        findings.extend(file_findings)
        all_classifications[rel] = classifications

        # Collect functions that have STW regions.
        for func in functions:
            if "_PyEval_StopTheWorld" in func["body"]:
                stw_functions.append(
                    {
                        "file": rel,
                        "function": func["name"],
                        "line": func["start_line"],
                        "classification": classifications.get(func["name"], "unknown"),
                    }
                )

    # Summary statistics.
    finding_types: dict[str, int] = {}
    classifications_summary: dict[str, int] = {}
    for f in findings:
        finding_types[f["type"]] = finding_types.get(f["type"], 0) + 1
        classifications_summary[f["classification"]] = (
            classifications_summary.get(f["classification"], 0) + 1
        )

    return {
        "project_root": str(project_root),
        "scan_root": str(target_path),
        "files_analyzed": files_analyzed,
        "functions_analyzed": functions_analyzed,
        "findings": findings,
        "stw_functions": stw_functions,
        "function_classifications": all_classifications,
        "summary": {
            "total_findings": len(findings),
            "by_type": finding_types,
            "by_classification": classifications_summary,
            "stw_function_count": len(stw_functions),
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
