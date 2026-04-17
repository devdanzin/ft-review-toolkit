"""Tests for analyze_ft_history.py — free-threading git history analysis."""

import os
import subprocess
import unittest
from pathlib import Path

from helpers import import_script, TempExtension

fth = import_script("analyze_ft_history")


C_CODE_V1 = """\
#include <Python.h>

static PyObject *cache = NULL;

static PyObject *
get_cache(PyObject *self, PyObject *args)
{
    if (cache == NULL) {
        cache = PyDict_New();
    }
    Py_XINCREF(cache);
    return cache;
}
"""

C_CODE_V2_ATOMIC = """\
#include <Python.h>

static _Py_atomic_int initialized = 0;

static PyObject *
init_module(PyObject *self, PyObject *args)
{
    if (_Py_atomic_load_int(&initialized) == 0) {
        _Py_atomic_store_int(&initialized, 1);
    }
    Py_RETURN_NONE;
}
"""


def _make_git_env():
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@test.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@test.com",
    }


class TestClassifyFtCommit(unittest.TestCase):
    """Test free-threading commit classification."""

    def test_tsan_fix(self):
        self.assertEqual(
            fth.classify_ft_commit("Fix data race in cache lookup"),
            "ft_tsan_fix",
        )

    def test_atomic_migration(self):
        self.assertEqual(
            fth.classify_ft_commit("Use _Py_atomic_int for shared counter"),
            "ft_atomic_migration",
        )

    def test_lock_addition(self):
        self.assertEqual(
            fth.classify_ft_commit("Add Py_BEGIN_CRITICAL_SECTION to method"),
            "ft_lock_addition",
        )

    def test_ft_migration(self):
        self.assertEqual(
            fth.classify_ft_commit("Add free-threading support"),
            "ft_migration",
        )

    def test_subinterpreter(self):
        self.assertEqual(
            fth.classify_ft_commit("Support per-interpreter state"),
            "ft_subinterpreter",
        )

    def test_non_ft_commit(self):
        self.assertIsNone(
            fth.classify_ft_commit("Fix typo in documentation"),
        )

    def test_ft_keyword_in_diff(self):
        result = fth.classify_ft_commit(
            "Update module init",
            "+    Py_MOD_GIL_NOT_USED\n",
        )
        self.assertEqual(result, "ft_related")


class TestDetectRevertedAttempts(unittest.TestCase):
    """Test reverted free-threading attempt detection."""

    def test_revert_detected(self):
        commits = [
            {
                "hash": "abc1234",
                "date": "2025-01-01",
                "message": "Revert 'Add free-threading support'",
            },
        ]
        findings = fth._detect_reverted_attempts(commits)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["type"], "reverted_ft_attempt")

    def test_normal_revert_not_flagged(self):
        commits = [
            {
                "hash": "abc1234",
                "date": "2025-01-01",
                "message": "Revert 'Fix typo in docs'",
            },
        ]
        findings = fth._detect_reverted_attempts(commits)
        self.assertEqual(len(findings), 0)


class TestComputeMigrationTimeline(unittest.TestCase):
    """Test migration timeline computation."""

    def test_no_ft_commits(self):
        timeline = fth._compute_migration_timeline([])
        self.assertEqual(timeline["status"], "not_started")
        self.assertEqual(timeline["total_ft_commits"], 0)

    def test_active_migration(self):
        from datetime import datetime, timezone

        recent = datetime.now(timezone.utc).isoformat()
        commits = [
            {"date": "2024-06-01T00:00:00+00:00", "ft_type": "ft_migration"},
            {"date": recent, "ft_type": "ft_lock_addition"},
        ]
        timeline = fth._compute_migration_timeline(commits)
        self.assertEqual(timeline["status"], "active")
        self.assertEqual(timeline["total_ft_commits"], 2)


class TestAnalyzeIntegration(unittest.TestCase):
    """Integration test with a real git repo."""

    def test_non_git_repo(self):
        """Non-git directory returns error."""
        with TempExtension({"test.c": C_CODE_V1}) as root:
            result = fth.analyze([str(root)])
            self.assertIn("error", result)

    def test_git_repo_with_ft_commit(self):
        """Git repo with ft commit is detected."""
        with TempExtension({"mod.c": C_CODE_V1}, init_git=True) as root:
            # Add a ft-related commit.
            (root / "mod.c").write_text(C_CODE_V2_ATOMIC)
            env = _make_git_env()
            subprocess.run(["git", "add", "."], cwd=str(root), capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "Add _Py_atomic for thread safety"],
                cwd=str(root),
                capture_output=True,
                env=env,
            )

            result = fth.analyze([str(root), "--days", "30"])
            self.assertNotIn("error", result)
            self.assertIn("migration_timeline", result)
            self.assertGreater(result["summary"]["ft_commits"], 0)

    def test_output_structure(self):
        """Output has expected structure."""
        with TempExtension({"mod.c": C_CODE_V1}, init_git=True) as root:
            result = fth.analyze([str(root)])
            self.assertIn("project_root", result)
            self.assertIn("scan_root", result)
            self.assertIn("summary", result)
            self.assertIn("migration_timeline", result)
            self.assertIn("findings", result)


class TestParseArgsWorkers(unittest.TestCase):
    """Test --workers argument parsing."""

    def test_workers_default(self):
        args = fth.parse_args([])
        self.assertEqual(args["workers"], 8)

    def test_workers_explicit(self):
        args = fth.parse_args(["--workers", "4"])
        self.assertEqual(args["workers"], 4)

    def test_workers_min_one(self):
        # Zero or negative clamped to at least 1.
        args = fth.parse_args(["--workers", "0"])
        self.assertGreaterEqual(args["workers"], 1)

    def test_workers_invalid_falls_back(self):
        # Non-numeric value falls back to default 8.
        args = fth.parse_args(["--workers", "bogus"])
        self.assertEqual(args["workers"], 8)


class TestGetFtCommitDetailsWorkers(unittest.TestCase):
    """Test _get_ft_commit_details with varying worker counts."""

    def _make_repo_with_ft_commit(self, root: Path) -> str:
        """Create a ft-related commit, return its hash."""
        env = _make_git_env()
        (root / "mod.c").write_text(C_CODE_V2_ATOMIC)
        subprocess.run(["git", "add", "."], cwd=str(root), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Add _Py_atomic for thread safety"],
            cwd=str(root),
            capture_output=True,
            env=env,
        )
        rev = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
        )
        return rev.stdout.strip()

    def _run_with_workers(self, workers: int) -> None:
        with TempExtension({"mod.c": C_CODE_V1}, init_git=True) as root:
            commit_hash = self._make_repo_with_ft_commit(root)
            if not commit_hash:
                self.skipTest("git not available")
            commits = [
                {
                    "hash": commit_hash,
                    "message": "Add _Py_atomic for thread safety",
                    "date": "2025-01-01T00:00:00+00:00",
                    "author": "Test",
                    "files": ["mod.c"],
                    "ft_type": "ft_atomic_migration",
                }
            ]
            details = fth._get_ft_commit_details(commits, root, ".", workers=workers)
            self.assertEqual(len(details), 1)
            self.assertEqual(details[0]["commit"], commit_hash[:7])

    def test_workers_one(self):
        self._run_with_workers(1)

    def test_workers_eight(self):
        self._run_with_workers(8)

    def test_empty_commit_list(self):
        with TempExtension({"mod.c": C_CODE_V1}, init_git=True) as root:
            details = fth._get_ft_commit_details([], root, ".", workers=4)
            self.assertEqual(details, [])


class TestAnalyzeToolkitRepoSmoke(unittest.TestCase):
    """Smoke test: analyze() on a git repo returns a dict with the
    expected top-level keys and does not hang.
    """

    def test_analyze_does_not_hang(self):
        # Use a freshly-initialised tiny repo so the test is hermetic and
        # doesn't depend on the surrounding filesystem layout.
        with TempExtension({"mod.c": C_CODE_V1}, init_git=True) as root:
            result = fth.analyze([str(root), "--workers", "2", "--days", "30"])
            self.assertIsInstance(result, dict)
            self.assertIn("migration_timeline", result)


if __name__ == "__main__":
    unittest.main()
