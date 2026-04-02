#!/usr/bin/env python3
"""Parse ThreadSanitizer (TSan) reports into structured findings.

Parses TSan text output, groups races by location, deduplicates, and
separates extension code races from CPython internal races.

Unlike other scripts, this uses regex (not Tree-sitter) because TSan
output is plain text, not C source.

Usage:
    python parse_tsan_report.py /path/to/tsan_report.txt
"""

import json
import re
import sys
from pathlib import Path


# Patterns for parsing TSan output.
_WARNING_RE = re.compile(r"WARNING: ThreadSanitizer: (.+?)(?:\s+\(pid=\d+\))?\s*$")
_ACCESS_RE = re.compile(
    r"^\s+((?:Previous )?(?:[Ww]rite|[Rr]ead)) of size (\d+) "
    r"at (0x[0-9a-f]+) by (.*?):\s*$"
)
_FRAME_RE = re.compile(r"^\s+#(\d+)\s+(\S+)\s+(.+?)(?:\s+\((.+?)\))?\s*$")
_LOCATION_RE = re.compile(
    r"^\s+Location is (.+?) of size (\d+) at (0x[0-9a-f]+)"
    r"(?: \((.+?)\))?\s*$"
)
_THREAD_CREATE_RE = re.compile(
    r"^\s+Thread T(\d+)\s+'?(.*?)'?\s+(?:\(tid=\d+.*?\)\s+)?created by (.*?) at:\s*$"
)
_SUMMARY_RE = re.compile(
    r"^SUMMARY: ThreadSanitizer: (.+?)\s+(\S+?)(?::(\d+))?(?::(\d+))?"
    r"\s+in\s+(.+?)\s*$"
)
_SEPARATOR_RE = re.compile(r"^={10,}$")

# Patterns for identifying CPython internal frames.
_CPYTHON_PATH_PATTERNS = [
    r"/Python/",
    r"/Objects/",
    r"/Include/",
    r"/Modules/_",
    r"/Lib/",
    r"/Parser/",
    r"pycore_",
    r"ceval\.c",
    r"methodobject\.c",
    r"call\.c",
    r"classobject\.c",
    r"descrobject\.c",
    r"context\.c",
    r"thread_pthread\.h",
    r"_threadmodule\.c",
]
_CPYTHON_RE = re.compile("|".join(_CPYTHON_PATH_PATTERNS))


def _parse_stack_frame(line: str) -> dict | None:
    """Parse a single stack frame line.

    Returns dict with: frame_num, function, location, module, file, line, col.
    """
    m = _FRAME_RE.match(line)
    if not m:
        return None

    frame_num = int(m.group(1))
    function = m.group(2)
    location = m.group(3).strip()
    module = m.group(4) or ""

    # Parse file:line:col from location.
    file_path = None
    line_num = None
    col_num = None
    loc_match = re.match(r"(.+?):(\d+):(\d+)", location)
    if loc_match:
        file_path = loc_match.group(1)
        line_num = int(loc_match.group(2))
        col_num = int(loc_match.group(3))
    elif location and location != "<null>":
        file_path = location

    return {
        "frame_num": frame_num,
        "function": function,
        "location": location,
        "module": module,
        "file": file_path,
        "line": line_num,
        "col": col_num,
    }


def _is_cpython_frame(frame: dict) -> bool:
    """Check if a stack frame is from CPython internals."""
    loc = frame.get("location", "") or ""
    module = frame.get("module", "") or ""
    combined = loc + " " + module
    return bool(_CPYTHON_RE.search(combined))


def _get_extension_frame(frames: list[dict]) -> dict | None:
    """Find the first non-CPython frame in a stack (the extension code)."""
    for frame in frames:
        if not _is_cpython_frame(frame):
            return frame
    return None


def _parse_tsan_block(lines: list[str]) -> dict | None:
    """Parse a single TSan warning block into a structured finding."""
    if not lines:
        return None

    # Find the WARNING line.
    race_type = None
    for line in lines:
        m = _WARNING_RE.match(line)
        if m:
            race_type = m.group(1)
            break

    if not race_type:
        return None

    # Parse access descriptors and their stack traces.
    accesses = []
    current_access = None
    thread_info = []
    location_info = None

    for line in lines:
        # Access header (Write/Read of size N).
        access_m = _ACCESS_RE.match(line)
        if access_m:
            if current_access:
                accesses.append(current_access)
            current_access = {
                "access_type": access_m.group(1).strip(),
                "size": int(access_m.group(2)),
                "address": access_m.group(3),
                "thread": access_m.group(4).strip(),
                "frames": [],
            }
            continue

        # Stack frame.
        frame = _parse_stack_frame(line)
        if frame and current_access:
            current_access["frames"].append(frame)
            continue

        # Location info.
        loc_m = _LOCATION_RE.match(line)
        if loc_m:
            location_info = {
                "description": loc_m.group(1),
                "size": int(loc_m.group(2)),
                "address": loc_m.group(3),
                "module": loc_m.group(4) or "",
            }
            # If we were collecting an access, finish it.
            if current_access:
                accesses.append(current_access)
                current_access = None
            continue

        # Thread creation info.
        thread_m = _THREAD_CREATE_RE.match(line)
        if thread_m:
            if current_access:
                accesses.append(current_access)
                current_access = None
            thread_info.append(
                {
                    "thread_id": int(thread_m.group(1)),
                    "thread_name": thread_m.group(2),
                    "creator": thread_m.group(3),
                }
            )
            continue

    # Don't forget the last access.
    if current_access:
        accesses.append(current_access)

    # Parse summary line.
    summary = None
    for line in lines:
        sum_m = _SUMMARY_RE.match(line)
        if sum_m:
            summary = {
                "type": sum_m.group(1),
                "file": sum_m.group(2),
                "line": int(sum_m.group(3)) if sum_m.group(3) else None,
                "col": int(sum_m.group(4)) if sum_m.group(4) else None,
                "function": sum_m.group(5),
            }
            break

    if not accesses:
        return None

    # Determine if this is an extension race or CPython internal.
    ext_frames = []
    for access in accesses:
        ef = _get_extension_frame(access["frames"])
        if ef:
            ext_frames.append(ef)

    is_extension_race = len(ext_frames) > 0
    is_cpython_only = not is_extension_race

    return {
        "race_type": race_type,
        "accesses": accesses,
        "location": location_info,
        "thread_info": thread_info,
        "summary": summary,
        "is_extension_race": is_extension_race,
        "is_cpython_only": is_cpython_only,
        "extension_frames": ext_frames,
    }


def _split_tsan_blocks(text: str) -> list[list[str]]:
    """Split TSan output into individual warning blocks."""
    blocks = []
    current_block: list[str] = []
    in_block = False

    for line in text.splitlines():
        if _SEPARATOR_RE.match(line):
            if in_block and current_block:
                blocks.append(current_block)
                current_block = []
                in_block = False
            else:
                in_block = True
            continue
        if in_block:
            current_block.append(line)

    if current_block:
        blocks.append(current_block)

    return blocks


def _dedup_key(finding: dict) -> str:
    """Generate a deduplication key from a finding's source locations."""
    parts = []
    for access in finding.get("accesses", []):
        ef = _get_extension_frame(access["frames"])
        if ef and ef.get("file") and ef.get("line"):
            parts.append(f"{ef['file']}:{ef['line']}")
        elif access["frames"]:
            f0 = access["frames"][0]
            parts.append(f"{f0.get('file', '?')}:{f0.get('line', '?')}")
    parts.sort()
    return "|".join(parts)


def _deduplicate_races(findings: list[dict]) -> list[dict]:
    """Deduplicate findings by source location pair."""
    seen: dict[str, int] = {}
    deduped: list[dict] = []

    for finding in findings:
        key = _dedup_key(finding)
        if key in seen:
            idx = seen[key]
            deduped[idx]["frequency"] = deduped[idx].get("frequency", 1) + 1
        else:
            seen[key] = len(deduped)
            finding["frequency"] = 1
            deduped.append(finding)

    return deduped


def _classify_severity(finding: dict) -> tuple[str, str]:
    """Classify a finding's severity and ft classification."""
    race_type = finding.get("race_type", "")
    location = finding.get("location", {}) or {}
    loc_desc = location.get("description", "")

    # Check if it involves a global variable.
    if "global" in loc_desc:
        return "RACE", "CRITICAL"

    # Check access types.
    access_types = [
        a.get("access_type", "").lower() for a in finding.get("accesses", [])
    ]
    has_write = any("write" in t for t in access_types)

    if has_write and len(access_types) >= 2:
        return "RACE", "HIGH"

    if "data race" in race_type.lower():
        return "RACE", "HIGH"

    return "RACE", "MEDIUM"


def analyze(target: str, *, max_files: int = 0) -> dict:
    """Parse a TSan report file into structured findings.

    Args:
        target: Path to TSan report file.
        max_files: Unused (kept for API compatibility).

    Returns JSON envelope with findings.
    """
    report_path = Path(target).resolve()

    if not report_path.exists():
        return {
            "error": f"TSan report not found: {report_path}",
            "report_path": str(report_path),
        }

    try:
        text = report_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"error": f"Failed to read report: {e}", "report_path": str(report_path)}

    # Split into blocks and parse each.
    blocks = _split_tsan_blocks(text)
    raw_findings = []
    for block in blocks:
        finding = _parse_tsan_block(block)
        if finding:
            raw_findings.append(finding)

    # Deduplicate.
    deduped = _deduplicate_races(raw_findings)

    # Classify each finding.
    findings = []
    for finding in deduped:
        classification, severity = _classify_severity(finding)
        finding["classification"] = classification
        finding["severity"] = severity
        findings.append(finding)

    # Separate extension vs CPython races.
    extension_races = [f for f in findings if f["is_extension_race"]]
    cpython_races = [f for f in findings if f["is_cpython_only"]]

    # Build summary.
    return {
        "report_path": str(report_path),
        "total_warnings": len(raw_findings),
        "unique_races": len(deduped),
        "extension_races": len(extension_races),
        "cpython_internal_races": len(cpython_races),
        "findings": findings,
        "summary": {
            "total_findings": len(findings),
            "by_classification": {
                "RACE": len([f for f in findings if f["classification"] == "RACE"]),
            },
            "by_severity": {
                s: len([f for f in findings if f["severity"] == s])
                for s in ("CRITICAL", "HIGH", "MEDIUM", "LOW")
                if any(f["severity"] == s for f in findings)
            },
            "actionable": len(extension_races),
            "cpython_internal": len(cpython_races),
        },
    }


def main() -> None:
    """CLI entry point."""
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: parse_tsan_report.py <report_file>"}))
        sys.exit(2)

    target = sys.argv[1]
    try:
        result = analyze(target)
        if "error" in result:
            json.dump(result, sys.stdout, indent=2)
            sys.stdout.write("\n")
            sys.exit(1)
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    except Exception as e:
        json.dump({"error": str(e), "type": type(e).__name__}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
