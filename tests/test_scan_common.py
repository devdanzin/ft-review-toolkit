"""Tests for scan_common.py shared utilities."""

import unittest
from pathlib import Path

from helpers import TempExtension, import_script

sc = import_script("scan_common")


class TestFindProjectRoot(unittest.TestCase):
    """Tests for find_project_root()."""

    def test_git_marker(self):
        """Directory with .git is detected as project root."""
        with TempExtension({"src/test.c": "int x;"}, init_git=True) as root:
            found = sc.find_project_root(root / "src")
            self.assertEqual(found, root)

    def test_no_markers(self):
        """Directory without markers returns the start directory."""
        with TempExtension({"test.c": "int x;"}) as root:
            found = sc.find_project_root(root)
            self.assertEqual(found, root)

    def test_file_path(self):
        """File path returns its parent as root."""
        with TempExtension({"test.c": "int x;"}) as root:
            found = sc.find_project_root(root / "test.c")
            self.assertEqual(found, root)


class TestDiscoverCFiles(unittest.TestCase):
    """Tests for discover_c_files()."""

    def test_finds_c_files(self):
        """Discovers .c and .h files in a directory."""
        with TempExtension({"a.c": "int x;", "b.h": "int y;"}) as root:
            files = list(sc.discover_c_files(root))
            suffixes = {f.suffix for f in files}
            self.assertIn(".c", suffixes)
            self.assertIn(".h", suffixes)

    def test_excludes_build_dir(self):
        """Files in build/ are excluded."""
        with TempExtension({"src/a.c": "int x;", "build/b.c": "int y;"}) as root:
            files = list(sc.discover_c_files(root))
            names = {f.name for f in files}
            self.assertIn("a.c", names)
            self.assertNotIn("b.c", names)

    def test_single_file_target(self):
        """Single file target yields that file."""
        with TempExtension({"test.c": "int x;"}) as root:
            files = list(sc.discover_c_files(root / "test.c"))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].name, "test.c")

    def test_non_c_file_skipped(self):
        """Non-C file target yields nothing."""
        with TempExtension({"test.py": "x = 1"}) as root:
            files = list(sc.discover_c_files(root / "test.py"))
            self.assertEqual(len(files), 0)

    def test_max_files(self):
        """max_files caps the number of files yielded."""
        with TempExtension({"a.c": "int a;", "b.c": "int b;", "c.c": "int c;"}) as root:
            files = list(sc.discover_c_files(root, max_files=2))
            self.assertEqual(len(files), 2)

    def test_empty_dir(self):
        """Empty directory yields nothing."""
        with TempExtension({}) as root:
            files = list(sc.discover_c_files(root))
            self.assertEqual(len(files), 0)


class TestParseCommonArgs(unittest.TestCase):
    """Tests for parse_common_args()."""

    def test_no_args(self):
        """No arguments returns default path and max_files."""
        target, max_files = sc.parse_common_args([])
        self.assertEqual(target, ".")
        self.assertEqual(max_files, 0)

    def test_positional_path(self):
        """Positional argument sets target path."""
        target, max_files = sc.parse_common_args(["/some/path"])
        self.assertEqual(target, "/some/path")
        self.assertEqual(max_files, 0)

    def test_max_files_flag(self):
        """--max-files flag sets max_files."""
        target, max_files = sc.parse_common_args(["--max-files", "5", "/path"])
        self.assertEqual(target, "/path")
        self.assertEqual(max_files, 5)


class TestHelperFunctions(unittest.TestCase):
    """Tests for is_thread_local, is_init_function, is_in_region."""

    def test_thread_local_keyword(self):
        """__thread keyword detected."""
        self.assertTrue(sc.is_thread_local("__thread int", ""))

    def test_thread_local_in_source(self):
        """thread_local in source line detected."""
        self.assertFalse(sc.is_thread_local("int", "int x;"))
        self.assertTrue(sc.is_thread_local("int", "thread_local int x;"))

    def test_not_thread_local(self):
        """Normal variable not flagged."""
        self.assertFalse(sc.is_thread_local("static int", "static int x;"))

    def test_init_function_pyinit(self):
        """PyInit_xxx is an init function."""
        self.assertTrue(sc.is_init_function("PyInit_mymod"))

    def test_init_function_exec(self):
        """exec_xxx is an init function."""
        self.assertTrue(sc.is_init_function("exec_module"))

    def test_not_init_function(self):
        """Regular function name not flagged."""
        self.assertFalse(sc.is_init_function("process_data"))

    def test_in_region(self):
        """Offset inside region returns True."""
        self.assertTrue(sc.is_in_region(5, [(0, 10)]))

    def test_not_in_region(self):
        """Offset outside region returns False."""
        self.assertFalse(sc.is_in_region(15, [(0, 10)]))

    def test_in_region_boundary(self):
        """Start is inclusive, end is exclusive."""
        self.assertTrue(sc.is_in_region(0, [(0, 10)]))
        self.assertFalse(sc.is_in_region(10, [(0, 10)]))

    def test_empty_regions(self):
        """Empty region list returns False."""
        self.assertFalse(sc.is_in_region(5, []))


class TestExtractNearbyComments(unittest.TestCase):
    """Tests for extract_nearby_comments()."""

    def test_finds_inline_comment(self):
        """Finds C inline comment near target line."""
        code = b"int x; // this is a comment\nint y;\n"
        from tree_sitter_utils import parse_bytes_for_file

        tree = parse_bytes_for_file(code, Path("test.c"))
        comments = sc.extract_nearby_comments(code, tree, 1, radius=1)
        self.assertTrue(len(comments) > 0)
        self.assertIn("comment", comments[0].lower())

    def test_no_comments(self):
        """Source with no comments returns empty list."""
        code = b"int x;\nint y;\n"
        from tree_sitter_utils import parse_bytes_for_file

        tree = parse_bytes_for_file(code, Path("test.c"))
        comments = sc.extract_nearby_comments(code, tree, 1, radius=1)
        self.assertEqual(len(comments), 0)


class TestHasSafetyAnnotation(unittest.TestCase):
    """Tests for has_safety_annotation()."""

    def test_thread_safe_keyword(self):
        """'thread-safe' keyword detected."""
        self.assertTrue(sc.has_safety_annotation(["// thread-safe by design"]))

    def test_mutex_held(self):
        """'mutex held' keyword detected."""
        self.assertTrue(sc.has_safety_annotation(["/* mutex held */"]))

    def test_intentional(self):
        """'intentional' keyword detected."""
        self.assertTrue(sc.has_safety_annotation(["// intentional race"]))

    def test_no_annotation(self):
        """Normal comment not flagged."""
        self.assertFalse(sc.has_safety_annotation(["// increment counter"]))

    def test_empty_list(self):
        """Empty list returns False."""
        self.assertFalse(sc.has_safety_annotation([]))

    def test_case_insensitive(self):
        """Keywords are case-insensitive."""
        self.assertTrue(sc.has_safety_annotation(["// THREAD-SAFE"]))


class TestMakeFinding(unittest.TestCase):
    """Tests for make_finding()."""

    def test_basic_finding(self):
        """Creates a finding with required fields."""
        f = sc.make_finding(
            "test_type",
            classification="RACE",
            severity="HIGH",
            detail="test detail",
        )
        self.assertEqual(f["type"], "test_type")
        self.assertEqual(f["classification"], "RACE")
        self.assertEqual(f["severity"], "HIGH")
        self.assertEqual(f["detail"], "test detail")
        self.assertEqual(f["confidence"], "high")
        self.assertEqual(f["function"], "")
        self.assertEqual(f["line"], 0)

    def test_extra_fields(self):
        """Extra keyword arguments are included."""
        f = sc.make_finding(
            "test",
            classification="PROTECT",
            severity="MEDIUM",
            detail="d",
            variable="my_var",
            lock_name="my_mutex",
        )
        self.assertEqual(f["variable"], "my_var")
        self.assertEqual(f["lock_name"], "my_mutex")

    def test_custom_function_and_line(self):
        """Function and line can be set."""
        f = sc.make_finding(
            "test",
            function="my_func",
            line=42,
            classification="UNSAFE",
            severity="LOW",
            detail="d",
        )
        self.assertEqual(f["function"], "my_func")
        self.assertEqual(f["line"], 42)


if __name__ == "__main__":
    unittest.main()
