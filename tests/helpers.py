"""Test helpers for ft-review-toolkit tests."""

import importlib.util
import os
import shutil
import tempfile
from pathlib import Path


def import_script(name: str):
    """Import a script from plugins/ft-review-toolkit/scripts/ as a module."""
    script_dir = (
        Path(__file__).resolve().parent.parent
        / "plugins"
        / "ft-review-toolkit"
        / "scripts"
    )
    spec = importlib.util.spec_from_file_location(name, script_dir / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TempExtension:
    """Create a temporary C extension project for testing.

    Usage:
        with TempExtension({"src/myext.c": c_code}) as root:
            result = some_script.analyze(str(root))
    """

    def __init__(self, files: dict[str, str], *, init_git: bool = False):
        self.files = files
        self.init_git = init_git
        self._tmpdir = None

    def __enter__(self) -> Path:
        self._tmpdir = tempfile.mkdtemp(prefix="ft_test_")
        root = Path(self._tmpdir)

        for rel_path, content in self.files.items():
            full_path = root / rel_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")

        if self.init_git:
            import subprocess

            subprocess.run(["git", "init"], cwd=str(root), capture_output=True)
            subprocess.run(["git", "add", "."], cwd=str(root), capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "initial"],
                cwd=str(root),
                capture_output=True,
                env={
                    **os.environ,
                    "GIT_AUTHOR_NAME": "Test",
                    "GIT_AUTHOR_EMAIL": "test@test.com",
                    "GIT_COMMITTER_NAME": "Test",
                    "GIT_COMMITTER_EMAIL": "test@test.com",
                },
            )

        return root

    def __exit__(self, *args):
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
