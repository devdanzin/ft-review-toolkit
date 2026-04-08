#!/usr/bin/env python3
"""Scan C extensions for lock discipline issues.

Finds: missing lock releases on error paths, unpaired acquire/release,
nested lock risks, and functions that should use critical sections.

Usage:
    python scan_lock_discipline.py /path/to/extension
"""

import json
import re
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scan_common import discover_c_files, find_project_root, parse_common_args
from tree_sitter_utils import (
    extract_functions,
    find_calls_in_scope,
    find_return_statements,
    parse_bytes_for_file,
    strip_comments,
)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

_lock_data: dict | None = None


def _load_lock_macros() -> dict:
    """Load lock macro definitions from data file."""
    global _lock_data
    if _lock_data is not None:
        return _lock_data
    try:
        with open(_DATA_DIR / "lock_macros.json", encoding="utf-8") as f:
            _lock_data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(
            f"Warning: failed to load lock_macros.json: {e}",
            file=sys.stderr,
        )
        _lock_data = {}
    return _lock_data


def _get_acquire_release_sets() -> tuple[set[str], set[str]]:
    """Get sets of all acquire and release macro/function names."""
    data = _load_lock_macros()
    acquires = set(data.get("all_acquire_macros", []))
    releases = set(data.get("all_release_macros", []))
    return acquires, releases


def _get_lock_pairs() -> list[dict]:
    """Get lock acquire/release pair definitions."""
    data = _load_lock_macros()
    return data.get("lock_pairs", [])


def _find_lock_operations(func: dict, source_bytes: bytes) -> list[dict]:
    """Find all lock acquire/release operations in a function."""
    acquires, releases = _get_acquire_release_sets()
    all_lock_ops = acquires | releases
    body_node = func["body_node"]
    calls = find_calls_in_scope(body_node, source_bytes, all_lock_ops)

    operations = []
    for call in calls:
        name = call["function_name"]
        is_acquire = name in acquires
        operations.append(
            {
                "name": name,
                "is_acquire": is_acquire,
                "is_release": name in releases,
                "line": call["start_line"],
                "arguments": call["arguments_text"],
                "byte_offset": call["start_byte"],
            }
        )

    return operations


def _line_to_offset(text: str, line_delta: int) -> int | None:
    """Convert a line delta to a byte offset in text."""
    offset = 0
    for _ in range(line_delta):
        idx = text.find("\n", offset)
        if idx == -1:
            return None
        offset = idx + 1
    return offset


def _find_matching_release(acquire_name: str, lock_pairs: list[dict]) -> list[str]:
    """Find the release functions that match a given acquire function."""
    for pair in lock_pairs:
        if acquire_name in pair.get("acquire", []):
            return pair.get("release", [])
    return []


def _check_lock_pairing(func: dict, source_bytes: bytes) -> list[dict]:
    """Check that every lock acquire has a matching release."""
    findings = []
    operations = _find_lock_operations(func, source_bytes)
    lock_pairs = _get_lock_pairs()

    if not operations:
        return findings

    # Track acquire/release balance.
    acquires = [op for op in operations if op["is_acquire"]]
    releases = [op for op in operations if op["is_release"]]

    for acquire in acquires:
        expected_releases = _find_matching_release(acquire["name"], lock_pairs)
        if not expected_releases:
            continue

        # Check if there's a matching release after this acquire.
        has_release = False
        for release in releases:
            if (
                release["name"] in expected_releases
                and release["line"] > acquire["line"]
            ):
                has_release = True
                break

        if not has_release:
            findings.append(
                {
                    "type": "missing_release",
                    "function": func["name"],
                    "line": acquire["line"],
                    "classification": "RACE",
                    "severity": "CRITICAL",
                    "confidence": "high",
                    "detail": (
                        f"Lock acquired via {acquire['name']}() at line "
                        f"{acquire['line']} in '{func['name']}' has no matching "
                        f"release ({', '.join(expected_releases)}). The lock will "
                        f"be held forever, causing deadlock."
                    ),
                    "lock_acquire": acquire["name"],
                    "expected_release": expected_releases,
                }
            )

    return findings


def _check_error_path_releases(func: dict, source_bytes: bytes) -> list[dict]:
    """Check that locks are released before error returns."""
    findings = []
    operations = _find_lock_operations(func, source_bytes)
    lock_pairs = _get_lock_pairs()

    if not operations:
        return findings

    acquires = [op for op in operations if op["is_acquire"]]
    releases = [op for op in operations if op["is_release"]]
    returns = find_return_statements(func["body_node"], source_bytes)

    body_text = strip_comments(func["body"])

    for acquire in acquires:
        expected_releases = _find_matching_release(acquire["name"], lock_pairs)
        if not expected_releases:
            continue

        # Find the matching final release.
        final_release_line = None
        for release in releases:
            if (
                release["name"] in expected_releases
                and release["line"] > acquire["line"]
            ):
                final_release_line = release["line"]

        if final_release_line is None:
            continue  # No release at all — handled by _check_lock_pairing.

        # Check each return between acquire and final release.
        for ret in returns:
            ret_line = ret["start_line"]
            if ret_line <= acquire["line"] or ret_line >= final_release_line:
                continue

            # Return is between acquire and release — check if there's
            # a release before this return.
            acq_offset = _line_to_offset(
                body_text, acquire["line"] - func["start_line"]
            )
            ret_offset = _line_to_offset(body_text, ret_line - func["start_line"])

            if acq_offset is None or ret_offset is None:
                continue

            span = body_text[acq_offset:ret_offset]

            # Check if any expected release is in this span.
            has_release_before_return = False
            for rel_name in expected_releases:
                if rel_name in span:
                    has_release_before_return = True
                    break

            # Also check for goto to cleanup label.
            if not has_release_before_return and "goto" in span:
                # Check if goto target releases the lock.
                goto_match = re.search(r"goto\s+(\w+)", span)
                if goto_match:
                    label = goto_match.group(1)
                    # Check if the label's code contains the release.
                    label_pattern = rf"{re.escape(label)}\s*:"
                    label_match = re.search(label_pattern, body_text)
                    if label_match:
                        after_label = body_text[label_match.end() :]
                        for rel_name in expected_releases:
                            if rel_name in after_label:
                                has_release_before_return = True
                                break

            if not has_release_before_return:
                ret_value = ret.get("value_text", "")
                findings.append(
                    {
                        "type": "missing_release_on_error",
                        "function": func["name"],
                        "line": ret_line,
                        "classification": "RACE",
                        "severity": "HIGH",
                        "confidence": "high",
                        "detail": (
                            f"Return at line {ret_line} in '{func['name']}' "
                            f"(returning {ret_value or 'void'}) is between lock "
                            f"acquire ({acquire['name']} at line {acquire['line']}) "
                            f"and release ({', '.join(expected_releases)} at line "
                            f"{final_release_line}) without releasing the lock."
                        ),
                        "lock_acquire": acquire["name"],
                        "acquire_line": acquire["line"],
                        "release_line": final_release_line,
                        "return_value": ret_value,
                    }
                )

    return findings


def _check_nested_locks(func: dict, source_bytes: bytes) -> list[dict]:
    """Check for nested lock acquisitions (potential deadlock)."""
    findings = []
    operations = _find_lock_operations(func, source_bytes)

    acquires = [op for op in operations if op["is_acquire"]]
    if len(acquires) < 2:
        return findings

    # Check if different lock types are acquired.
    lock_types = set()
    for acq in acquires:
        lock_types.add(acq["name"])

    if len(lock_types) >= 2:
        lock_names = sorted(lock_types)
        findings.append(
            {
                "type": "nested_locks",
                "function": func["name"],
                "line": acquires[0]["line"],
                "classification": "PROTECT",
                "severity": "MEDIUM",
                "confidence": "medium",
                "detail": (
                    f"Function '{func['name']}' acquires multiple different locks: "
                    f"{', '.join(lock_names)}. If another function acquires them "
                    f"in a different order, this causes deadlock. Verify consistent "
                    f"lock ordering across the codebase."
                ),
                "lock_types": lock_names,
                "acquire_lines": [a["line"] for a in acquires],
            }
        )

    return findings


def _check_critical_section_candidates(func: dict, source_bytes: bytes) -> list[dict]:
    """Find functions that access self->member and could use critical sections."""
    findings = []
    body_text = strip_comments(func["body"])

    # Check if function accesses self->member.
    has_self_access = bool(re.search(r"\bself\s*->\s*\w+", body_text))
    if not has_self_access:
        return findings

    # Check if function already uses critical section.
    if "Py_BEGIN_CRITICAL_SECTION" in body_text:
        return findings

    # Check if function releases the GIL (member access after GIL release is risky).
    has_gil_release = "Py_BEGIN_ALLOW_THREADS" in body_text

    # Check if function has no lock protection at all.
    acquires, _ = _get_acquire_release_sets()
    has_any_lock = any(api in body_text for api in acquires)

    if has_gil_release or not has_any_lock:
        # Find the self->member accesses.
        members = set(re.findall(r"self\s*->\s*(\w+)", body_text))

        severity = "HIGH" if has_gil_release else "MEDIUM"
        context = (
            "after releasing the GIL"
            if has_gil_release
            else "without any lock protection"
        )

        findings.append(
            {
                "type": "critical_section_candidate",
                "function": func["name"],
                "line": func["start_line"],
                "classification": "PROTECT",
                "severity": severity,
                "confidence": "medium",
                "detail": (
                    f"Function '{func['name']}' accesses self->{', self->'.join(sorted(members))} "
                    f"{context}. Under free-threading, use "
                    f"Py_BEGIN_CRITICAL_SECTION(self) / Py_END_CRITICAL_SECTION(self) "
                    f"to protect per-object state."
                ),
                "members_accessed": sorted(members),
                "has_gil_release": has_gil_release,
            }
        )

    return findings


def _analyze_file(filepath: Path, source_bytes: bytes) -> list[dict]:
    """Analyze a single file for lock discipline issues."""
    findings = []
    tree = parse_bytes_for_file(source_bytes, filepath)
    functions = extract_functions(tree, source_bytes)

    for func in functions:
        findings.extend(_check_lock_pairing(func, source_bytes))
        findings.extend(_check_error_path_releases(func, source_bytes))
        findings.extend(_check_nested_locks(func, source_bytes))
        findings.extend(_check_critical_section_candidates(func, source_bytes))

    return findings


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Analyze C extension for lock discipline issues.

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
