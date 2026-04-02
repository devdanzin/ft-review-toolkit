#!/usr/bin/env python3
"""Analyze git history for free-threading related commits.

Finds: free-threading migration commits, incomplete migrations, reverted
attempts, TSan fix patterns, and similar unfixed patterns.

Extended defaults: --days 730 (2 years), --max-commits 2000 — captures the
full PEP 703 era.

Usage:
    python analyze_ft_history.py [path] [options]

Options:
    --days N          Analyze last N days (default: 730)
    --since DATE      Start date (ISO format, overrides --days)
    --until DATE      End date (ISO format, default: today)
    --last N          Analyze exactly the last N commits
    --max-commits N   Cap total commits analyzed (default: 2000)
    --no-function     Skip function-level churn
"""

import json
import re
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tree_sitter_utils import parse_bytes_for_file, extract_functions
from scan_common import find_project_root

# Free-threading specific search terms.
FT_KEYWORDS = [
    "free-thread",
    "free_thread",
    "freethread",
    "nogil",
    "no-gil",
    "no_gil",
    "GIL_DISABLED",
    "Py_GIL_DISABLED",
    "Py_MOD_GIL",
    "Py_mod_gil",
    "critical_section",
    "Py_BEGIN_CRITICAL_SECTION",
    "Py_END_CRITICAL_SECTION",
    "_Py_atomic",
    "std::atomic",
    "_Atomic",
    "thread-safe",
    "thread_safe",
    "thread safe",
    "data race",
    "data_race",
    "race condition",
    "TSan",
    "TSAN",
    "ThreadSanitizer",
    "PyMutex",
    "StopTheWorld",
    "stop_the_world",
    "stop-the-world",
    "per_interpreter",
    "per-interpreter",
    "subinterpreter",
    "Py_MOD_GIL_NOT_USED",
    "PyMutex_Lock",
    "PyMutex_Unlock",
]

FT_KEYWORD_RE = re.compile(
    "|".join(re.escape(kw) for kw in FT_KEYWORDS),
    re.IGNORECASE,
)

# Standard commit classification (vendored from analyze_history.py).
CLASSIFICATION_RULES: list[tuple[str, list[str]]] = [
    (
        "fix",
        [
            "fix",
            "bug",
            "patch",
            "resolve",
            "crash",
            "error",
            "broken",
            "repair",
            "correct",
            "regression",
            "segfault",
        ],
    ),
    ("docs", ["doc", "readme", "comment", "typo", "changelog"]),
    ("test", ["test", "coverage", "assert", "mock"]),
    ("refactor", ["refactor", "clean", "simplify", "reorganize", "rename"]),
    (
        "chore",
        [
            "bump",
            "dependency",
            "update",
            "ci",
            "config",
            "lint",
            "format",
            "version",
            "release",
            "merge",
            "revert",
        ],
    ),
    (
        "feature",
        [
            "add",
            "implement",
            "new",
            "feature",
            "introduce",
            "support",
            "enable",
            "create",
        ],
    ),
]

_GIT_TIMEOUT = 30
_SCRIPT_START: float = 0.0
_SCRIPT_TIMEOUT = 300
_MAX_DIFF_LINES = 150


def _check_script_timeout() -> bool:
    return (time.monotonic() - _SCRIPT_START) > _SCRIPT_TIMEOUT


def classify_commit(message: str) -> str:
    """Standard commit classification."""
    msg_lower = message.lower()
    for category, keywords in CLASSIFICATION_RULES:
        for keyword in keywords:
            if keyword in msg_lower:
                return category
    return "unknown"


def classify_ft_commit(message: str, diff_text: str = "") -> str | None:
    """Free-threading specific commit classification.

    Returns a ft-specific category or None if not ft-related.
    """
    if FT_KEYWORD_RE.search(message):
        msg_lower = message.lower()
        if any(
            kw in msg_lower
            for kw in ("tsan", "threadsan", "data race", "race condition")
        ):
            return "ft_tsan_fix"
        if any(kw in msg_lower for kw in ("atomic", "_py_atomic", "std::atomic")):
            return "ft_atomic_migration"
        if any(
            kw in msg_lower
            for kw in ("critical_section", "pymutex", "py_begin_critical")
        ):
            return "ft_lock_addition"
        if any(
            kw in msg_lower
            for kw in (
                "free-thread",
                "freethread",
                "nogil",
                "gil_disabled",
                "py_mod_gil",
            )
        ):
            return "ft_migration"
        if any(
            kw in msg_lower
            for kw in (
                "subinterpreter",
                "per_interpreter",
                "per-interpreter",
                "per interpreter",
            )
        ):
            return "ft_subinterpreter"
        return "ft_related"

    # Check diff for ft-related changes even if message doesn't mention it.
    if diff_text and FT_KEYWORD_RE.search(diff_text):
        return "ft_related"

    return None


def _run_git(args: list[str], cwd: Path, timeout: int = _GIT_TIMEOUT):
    """Run a git command and return the result."""
    return subprocess.run(
        ["git"] + args,
        capture_output=True,
        text=True,
        cwd=str(cwd),
        timeout=timeout,
    )


def _run_git_streaming(args: list[str], cwd: Path):
    """Run a git command with streaming output."""
    return subprocess.Popen(
        ["git"] + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(cwd),
    )


def _is_git_repo(path: Path) -> bool:
    """Check if path is inside a git repository."""
    try:
        result = _run_git(["rev-parse", "--is-inside-work-tree"], path, timeout=5)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _relative_scope(scan_root: Path, project_root: Path) -> str:
    """Get relative path from project root to scan root."""
    try:
        rel = scan_root.resolve().relative_to(project_root.resolve())
        return str(rel) if str(rel) != "." else "."
    except ValueError:
        return "."


def parse_git_log(
    lines, max_commits: int, project_root: Path | None = None
) -> tuple[list[dict], dict[str, dict]]:
    """Parse git log output into structured commits and file change data."""
    commits: list[dict] = []
    file_changes: dict[str, dict] = {}
    current_commit: dict | None = None
    commit_count = 0

    for line in lines:
        line = line.rstrip("\n")
        if line.startswith("COMMIT:"):
            if current_commit is not None:
                commits.append(current_commit)
            commit_count += 1
            if commit_count > max_commits:
                break
            parts = line[7:].split("|", 3)
            if len(parts) < 4:
                current_commit = None
                continue
            commit_hash, date_str, author, message = parts
            current_commit = {
                "hash": commit_hash,
                "date": date_str,
                "author": author,
                "message": message,
                "type": classify_commit(message),
                "ft_type": classify_ft_commit(message),
                "files": [],
                "stats": [],
            }
        elif line.strip() and current_commit is not None:
            parts = line.split("\t", 2)
            if len(parts) == 3:
                added_str, removed_str, filepath = parts
                try:
                    added = int(added_str) if added_str != "-" else 0
                    removed = int(removed_str) if removed_str != "-" else 0
                except ValueError:
                    continue
                current_commit["files"].append(filepath)
                current_commit["stats"].append(
                    {
                        "file": filepath,
                        "added": added,
                        "removed": removed,
                    }
                )
                if filepath not in file_changes:
                    file_changes[filepath] = {
                        "commits": 0,
                        "lines_added": 0,
                        "lines_removed": 0,
                        "authors": set(),
                        "first_date": date_str,
                        "last_date": date_str,
                    }
                fc = file_changes[filepath]
                fc["commits"] += 1
                fc["lines_added"] += added
                fc["lines_removed"] += removed
                fc["authors"].add(author)
                if date_str < fc["first_date"]:
                    fc["first_date"] = date_str
                if date_str > fc["last_date"]:
                    fc["last_date"] = date_str

    if current_commit is not None and commit_count <= max_commits:
        commits.append(current_commit)

    return commits, file_changes


def _get_commit_diff(commit_hash: str, project_root: Path, scope: str) -> str:
    """Get the diff for a specific commit."""
    diff_args = ["show", "--format=", "--patch", commit_hash, "--"]
    if scope != ".":
        diff_args.append(scope)
    try:
        dr = _run_git(diff_args, project_root)
        diff_text = dr.stdout if dr.returncode == 0 else ""
    except subprocess.TimeoutExpired:
        diff_text = ""
    # Truncate long diffs.
    lines = diff_text.splitlines()
    if len(lines) > _MAX_DIFF_LINES:
        diff_text = "\n".join(lines[:_MAX_DIFF_LINES]) + "\n[diff truncated]"
    return diff_text


def _compute_migration_timeline(ft_commits: list[dict]) -> dict:
    """Compute when free-threading work started and its current state."""
    if not ft_commits:
        return {
            "ft_work_started": None,
            "ft_work_latest": None,
            "total_ft_commits": 0,
            "ft_commits_by_type": {},
            "status": "not_started",
        }

    dates = [c["date"] for c in ft_commits]
    dates.sort()

    ft_types: dict[str, int] = {}
    for c in ft_commits:
        ft_type = c.get("ft_type", "ft_related")
        ft_types[ft_type] = ft_types.get(ft_type, 0) + 1

    # Determine status.
    latest = dates[-1]
    try:
        latest_dt = datetime.fromisoformat(latest)
        days_since = (datetime.now(timezone.utc) - latest_dt).days
    except ValueError:
        days_since = 999

    if days_since < 30:
        status = "active"
    elif days_since < 180:
        status = "paused"
    else:
        status = "stalled"

    return {
        "ft_work_started": dates[0],
        "ft_work_latest": dates[-1],
        "total_ft_commits": len(ft_commits),
        "ft_commits_by_type": ft_types,
        "days_since_last_ft_commit": days_since,
        "status": status,
    }


def _detect_incomplete_migration(
    ft_commits: list[dict], project_root: Path, scope: str
) -> list[dict]:
    """Detect incomplete free-threading migrations.

    Looks for patterns like: critical_section added to some methods of a type
    but not all, atomic used for some shared variables but not similar ones.
    """
    findings: list[dict] = []

    # Collect files that had ft-related changes.
    ft_files: dict[str, list[str]] = defaultdict(list)
    for commit in ft_commits:
        if _check_script_timeout():
            break
        for filepath in commit["files"]:
            ft_files[filepath].append(commit["hash"])

    # For each file, check if critical_section or atomic was added
    # to some functions but not others.
    for filepath, commit_hashes in ft_files.items():
        if _check_script_timeout():
            break

        full_path = project_root / filepath
        if not full_path.exists() or full_path.suffix not in (
            ".c",
            ".h",
            ".cpp",
            ".cc",
        ):
            continue

        try:
            source_bytes = full_path.read_bytes()
        except OSError:
            continue

        tree = parse_bytes_for_file(source_bytes, full_path)
        functions = extract_functions(tree, source_bytes)

        # Check for critical_section in some functions but not all.
        funcs_with_cs = []
        funcs_without_cs = []
        for func in functions:
            body = func["body"]
            if (
                "Py_BEGIN_CRITICAL_SECTION" in body
                or "critical_section" in body.lower()
            ):
                funcs_with_cs.append(func["name"])
            elif "self->" in body:
                # Functions that access self members but don't use critical_section.
                funcs_without_cs.append(func["name"])

        if funcs_with_cs and funcs_without_cs:
            findings.append(
                {
                    "type": "incomplete_migration",
                    "file": filepath,
                    "classification": "PROTECT",
                    "severity": "HIGH",
                    "confidence": "medium",
                    "detail": (
                        f"Critical section used in {', '.join(funcs_with_cs[:3])} "
                        f"but not in {', '.join(funcs_without_cs[:3])} "
                        f"(which also access self->member). Incomplete migration."
                    ),
                    "functions_protected": funcs_with_cs,
                    "functions_unprotected": funcs_without_cs[:5],
                }
            )

    return findings


def _detect_reverted_attempts(commits: list[dict]) -> list[dict]:
    """Detect free-threading commits that were later reverted."""
    findings = []
    revert_re = re.compile(
        r"revert.*(?:"
        + "|".join(
            re.escape(kw)
            for kw in [
                "free-thread",
                "nogil",
                "atomic",
                "critical_section",
                "thread-safe",
            ]
        )
        + ")",
        re.IGNORECASE,
    )

    for commit in commits:
        if revert_re.search(commit["message"]):
            findings.append(
                {
                    "type": "reverted_ft_attempt",
                    "commit": commit["hash"][:7],
                    "date": commit["date"],
                    "message": commit["message"],
                    "classification": "PROTECT",
                    "severity": "MEDIUM",
                    "confidence": "high",
                    "detail": (
                        f"Reverted free-threading change: '{commit['message']}' "
                        f"({commit['hash'][:7]}). Investigate why it was reverted."
                    ),
                }
            )

    return findings


def _get_ft_commit_details(
    ft_commits: list[dict], project_root: Path, scope: str
) -> list[dict]:
    """Get detailed info for free-threading related commits."""
    details = []
    for commit in ft_commits[:20]:  # Cap at 20 for performance.
        if _check_script_timeout():
            break
        diff_text = _get_commit_diff(commit["hash"], project_root, scope)
        details.append(
            {
                "commit": commit["hash"][:7],
                "message": commit["message"],
                "date": commit["date"],
                "author": commit["author"],
                "ft_type": commit.get("ft_type"),
                "files": commit["files"],
                "diff": diff_text,
            }
        )
    return details


def parse_args(argv: list[str]) -> dict:
    """Parse CLI arguments."""
    args: dict = {
        "path": ".",
        "days": 730,
        "since": None,
        "until": None,
        "last": None,
        "max_commits": 2000,
        "max_files": 0,
        "no_function": False,
    }
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--days" and i + 1 < len(argv):
            args["days"] = int(argv[i + 1])
            i += 2
        elif arg == "--since" and i + 1 < len(argv):
            args["since"] = argv[i + 1]
            i += 2
        elif arg == "--until" and i + 1 < len(argv):
            args["until"] = argv[i + 1]
            i += 2
        elif arg == "--last" and i + 1 < len(argv):
            args["last"] = int(argv[i + 1])
            i += 2
        elif arg == "--max-commits" and i + 1 < len(argv):
            args["max_commits"] = int(argv[i + 1])
            i += 2
        elif arg == "--max-files" and i + 1 < len(argv):
            args["max_files"] = int(argv[i + 1])
            i += 2
        elif arg == "--no-function":
            args["no_function"] = True
            i += 1
        elif not arg.startswith("-"):
            args["path"] = arg
            i += 1
        else:
            i += 1
    return args


def analyze(argv: list[str] | None = None) -> dict:
    """Analyze git history for free-threading related commits."""
    global _SCRIPT_START
    _SCRIPT_START = time.monotonic()

    if argv is None:
        argv = sys.argv[1:]
    args = parse_args(argv)

    scan_root = Path(args["path"]).resolve()
    project_root = find_project_root(scan_root)

    if not _is_git_repo(project_root):
        return {"error": "Not a git repository", "project_root": str(project_root)}

    now = datetime.now(timezone.utc)
    since = args["since"] or (now - timedelta(days=args["days"])).isoformat()
    until = args["until"] or now.isoformat()

    last_n = args["last"]
    max_commits = args["max_commits"]

    git_args = ["log", "--numstat", "--format=COMMIT:%H|%aI|%an|%s"]
    if last_n is not None:
        git_args.append(f"-{last_n}")
    else:
        git_args.extend([f"--since={since}", f"--until={until}"])
    git_args.append("--")
    rel_scope = _relative_scope(scan_root, project_root)
    if rel_scope != ".":
        git_args.append(rel_scope)

    proc = _run_git_streaming(git_args, project_root)
    try:
        commits, file_changes = parse_git_log(proc.stdout, max_commits, project_root)
    finally:
        proc.wait()

    # Filter to free-threading related commits.
    ft_commits = [c for c in commits if c.get("ft_type") is not None]

    # For commits without ft_type from message, check diffs.
    non_ft_commits = [c for c in commits if c.get("ft_type") is None]
    for commit in non_ft_commits[:100]:  # Check first 100 non-ft commits.
        if _check_script_timeout():
            break
        diff_text = _get_commit_diff(commit["hash"], project_root, rel_scope)
        ft_type = classify_ft_commit(commit["message"], diff_text)
        if ft_type:
            commit["ft_type"] = ft_type
            ft_commits.append(commit)

    # Compute migration timeline.
    timeline = _compute_migration_timeline(ft_commits)

    # Detect incomplete migrations.
    incomplete = _detect_incomplete_migration(ft_commits, project_root, rel_scope)

    # Detect reverted attempts.
    reverted = _detect_reverted_attempts(commits)

    # Get detailed ft commit info.
    ft_details = _get_ft_commit_details(ft_commits, project_root, rel_scope)

    # Compute file churn for ft-related files.
    ft_file_stats = []
    for filepath, fc in file_changes.items():
        ft_file_stats.append(
            {
                "file": filepath,
                "commits": fc["commits"],
                "lines_added": fc["lines_added"],
                "lines_removed": fc["lines_removed"],
                "authors": len(fc["authors"]),
            }
        )
    ft_file_stats.sort(key=lambda x: x["commits"], reverse=True)

    # Build findings list.
    findings: list[dict] = []
    findings.extend(incomplete)
    findings.extend(reverted)

    # Summary.
    commits_by_type: dict[str, int] = defaultdict(int)
    for c in commits:
        commits_by_type[c["type"]] += 1

    result = {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "time_range": {
            "start": since,
            "end": until,
            "days": args["days"],
        },
        "summary": {
            "total_commits": len(commits),
            "ft_commits": len(ft_commits),
            "commits_by_type": dict(commits_by_type),
        },
        "migration_timeline": timeline,
        "ft_commit_details": ft_details,
        "findings": findings,
        "file_churn": ft_file_stats[:30],
    }

    return result


def main() -> None:
    """CLI entry point."""
    try:
        result = analyze()
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
