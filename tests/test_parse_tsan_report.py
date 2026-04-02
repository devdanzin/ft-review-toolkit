"""Tests for parse_tsan_report.py — TSan report parsing."""

import unittest
from helpers import import_script, TempExtension

tsan = import_script("parse_tsan_report")

# Real TSan report format from a racy C extension.
SAMPLE_TSAN_REPORT = """\
==================
WARNING: ThreadSanitizer: data race (pid=835840)
  Write of size 4 at 0x7ffff6504134 by thread T2:
    #0 racy_increment /tmp/racy_ext/racymod.c:10:20 (racymod.cpython-314td-x86_64-linux-gnu.so+0x11e0)
    #1 cfunction_vectorcall_NOARGS /home/user/cpython/Objects/methodobject.c:508:24 (python+0x354640)
    #2 _PyObject_VectorcallTstate /home/user/cpython/./Include/internal/pycore_call.h:177:11 (python+0x27a1eb)
    #3 PyObject_Vectorcall /home/user/cpython/Objects/call.c:327:12 (python+0x27bde0)

  Previous write of size 4 at 0x7ffff6504134 by thread T1:
    #0 racy_increment /tmp/racy_ext/racymod.c:10:20 (racymod.cpython-314td-x86_64-linux-gnu.so+0x11e0)
    #1 cfunction_vectorcall_NOARGS /home/user/cpython/Objects/methodobject.c:508:24 (python+0x354640)
    #2 _PyObject_VectorcallTstate /home/user/cpython/./Include/internal/pycore_call.h:177:11 (python+0x27a1eb)

  Location is global 'shared_counter' of size 4 at 0x7ffff6504134 (racymod.cpython-314td-x86_64-linux-gnu.so+0x4134)

  Thread T2 'Thread-2 (hamme' (tid=835843, running) created by main thread at:
    #0 pthread_create <null> (python+0xebeee)
    #1 do_start_joinable_thread /home/user/cpython/Python/thread_pthread.h:289:14 (python+0x68edf0)

  Thread T1 'Thread-1 (hamme' (tid=835842, running) created by main thread at:
    #0 pthread_create <null> (python+0xebeee)
    #1 do_start_joinable_thread /home/user/cpython/Python/thread_pthread.h:289:14 (python+0x68edf0)

SUMMARY: ThreadSanitizer: data race /tmp/racy_ext/racymod.c:10:20 in racy_increment
==================
"""

# Duplicate race (same location, different thread pair).
DUPLICATE_RACE_REPORT = """\
==================
WARNING: ThreadSanitizer: data race (pid=1234)
  Write of size 4 at 0x7fff00001000 by thread T3:
    #0 racy_increment /tmp/racy_ext/racymod.c:10:20 (racymod.so+0x11e0)
    #1 cfunction_vectorcall_NOARGS /home/user/cpython/Objects/methodobject.c:508:24 (python+0x354640)

  Previous write of size 4 at 0x7fff00001000 by thread T4:
    #0 racy_increment /tmp/racy_ext/racymod.c:10:20 (racymod.so+0x11e0)
    #1 cfunction_vectorcall_NOARGS /home/user/cpython/Objects/methodobject.c:508:24 (python+0x354640)

  Location is global 'shared_counter' of size 4 at 0x7fff00001000 (racymod.so+0x1000)

SUMMARY: ThreadSanitizer: data race /tmp/racy_ext/racymod.c:10:20 in racy_increment
==================
==================
WARNING: ThreadSanitizer: data race (pid=1234)
  Write of size 4 at 0x7fff00001000 by thread T5:
    #0 racy_increment /tmp/racy_ext/racymod.c:10:20 (racymod.so+0x11e0)
    #1 cfunction_vectorcall_NOARGS /home/user/cpython/Objects/methodobject.c:508:24 (python+0x354640)

  Previous write of size 4 at 0x7fff00001000 by thread T6:
    #0 racy_increment /tmp/racy_ext/racymod.c:10:20 (racymod.so+0x11e0)
    #1 cfunction_vectorcall_NOARGS /home/user/cpython/Objects/methodobject.c:508:24 (python+0x354640)

  Location is global 'shared_counter' of size 4 at 0x7fff00001000 (racymod.so+0x1000)

SUMMARY: ThreadSanitizer: data race /tmp/racy_ext/racymod.c:10:20 in racy_increment
==================
"""

# CPython-internal-only race (no extension frames).
CPYTHON_ONLY_RACE = """\
==================
WARNING: ThreadSanitizer: data race (pid=5555)
  Read of size 8 at 0x7fff99990000 by thread T1:
    #0 _PyEval_EvalFrameDefault /home/user/cpython/Python/generated_cases.c.h:1621:35 (python+0x5136b4)
    #1 _PyEval_EvalFrame /home/user/cpython/./Include/internal/pycore_ceval.h:120:16 (python+0x5095d6)

  Previous write of size 8 at 0x7fff99990000 by thread T2:
    #0 _PyDict_SetItem_Take2 /home/user/cpython/Objects/dictobject.c:1234:5 (python+0x2a0000)
    #1 PyDict_SetItem /home/user/cpython/Objects/dictobject.c:1500:12 (python+0x2a1000)

SUMMARY: ThreadSanitizer: data race /home/user/cpython/Python/generated_cases.c.h:1621:35 in _PyEval_EvalFrameDefault
==================
"""

# Multiple different races in one report.
MULTI_RACE_REPORT = """\
==================
WARNING: ThreadSanitizer: data race (pid=1000)
  Write of size 4 at 0x7fff00001000 by thread T1:
    #0 update_counter /src/myext/counter.c:25:5 (myext.so+0x1100)
    #1 cfunction_vectorcall_NOARGS /home/user/cpython/Objects/methodobject.c:508:24 (python+0x354640)

  Previous read of size 4 at 0x7fff00001000 by thread T2:
    #0 read_counter /src/myext/counter.c:30:12 (myext.so+0x1200)
    #1 cfunction_vectorcall_NOARGS /home/user/cpython/Objects/methodobject.c:508:24 (python+0x354640)

  Location is global 'g_counter' of size 4 at 0x7fff00001000 (myext.so+0x1000)

SUMMARY: ThreadSanitizer: data race /src/myext/counter.c:25:5 in update_counter
==================
==================
WARNING: ThreadSanitizer: data race (pid=1000)
  Write of size 1 at 0x7fff00002000 by thread T1:
    #0 set_flag /src/myext/flags.c:15:5 (myext.so+0x2100)
    #1 cfunction_vectorcall_NOARGS /home/user/cpython/Objects/methodobject.c:508:24 (python+0x354640)

  Previous read of size 1 at 0x7fff00002000 by thread T3:
    #0 check_flag /src/myext/flags.c:20:9 (myext.so+0x2200)
    #1 cfunction_vectorcall_NOARGS /home/user/cpython/Objects/methodobject.c:508:24 (python+0x354640)

  Location is global 'enabled' of size 1 at 0x7fff00002000 (myext.so+0x2000)

SUMMARY: ThreadSanitizer: data race /src/myext/flags.c:15:5 in set_flag
==================
"""

EMPTY_REPORT = ""

NO_TSAN_OUTPUT = """\
All tests passed.
55 passed in 3.09s
"""


class TestParseTsanReport(unittest.TestCase):
    """Test TSan report parsing."""

    def _write_report(self, root, content):
        """Write report content to a file and return its path."""
        report_path = root / "tsan_report.txt"
        report_path.write_text(content, encoding="utf-8")
        return str(report_path)

    def test_basic_race_parsed(self):
        """Basic data race is parsed correctly."""
        with TempExtension({}) as root:
            path = self._write_report(root, SAMPLE_TSAN_REPORT)
            result = tsan.analyze(path)
            self.assertEqual(result["total_warnings"], 1)
            self.assertEqual(result["unique_races"], 1)
            self.assertEqual(len(result["findings"]), 1)

            finding = result["findings"][0]
            self.assertEqual(finding["race_type"], "data race")
            self.assertTrue(finding["is_extension_race"])
            self.assertFalse(finding["is_cpython_only"])

    def test_accesses_parsed(self):
        """Access descriptors (write/read, thread, frames) are parsed."""
        with TempExtension({}) as root:
            path = self._write_report(root, SAMPLE_TSAN_REPORT)
            result = tsan.analyze(path)
            finding = result["findings"][0]

            self.assertEqual(len(finding["accesses"]), 2)
            # First access is a Write.
            self.assertIn("Write", finding["accesses"][0]["access_type"])
            # Both have frames.
            self.assertGreater(len(finding["accesses"][0]["frames"]), 0)
            self.assertGreater(len(finding["accesses"][1]["frames"]), 0)

    def test_stack_frames_parsed(self):
        """Stack frames include file, line, function."""
        with TempExtension({}) as root:
            path = self._write_report(root, SAMPLE_TSAN_REPORT)
            result = tsan.analyze(path)
            finding = result["findings"][0]
            frame = finding["accesses"][0]["frames"][0]

            self.assertEqual(frame["function"], "racy_increment")
            self.assertEqual(frame["file"], "/tmp/racy_ext/racymod.c")
            self.assertEqual(frame["line"], 10)
            self.assertEqual(frame["col"], 20)

    def test_location_parsed(self):
        """Global variable location info is parsed."""
        with TempExtension({}) as root:
            path = self._write_report(root, SAMPLE_TSAN_REPORT)
            result = tsan.analyze(path)
            finding = result["findings"][0]

            self.assertIsNotNone(finding["location"])
            self.assertIn("shared_counter", finding["location"]["description"])

    def test_global_variable_critical(self):
        """Race on global variable gets CRITICAL severity."""
        with TempExtension({}) as root:
            path = self._write_report(root, SAMPLE_TSAN_REPORT)
            result = tsan.analyze(path)
            finding = result["findings"][0]

            self.assertEqual(finding["classification"], "RACE")
            self.assertEqual(finding["severity"], "CRITICAL")

    def test_deduplication(self):
        """Duplicate races (same location pair) are deduplicated."""
        with TempExtension({}) as root:
            path = self._write_report(root, DUPLICATE_RACE_REPORT)
            result = tsan.analyze(path)
            self.assertEqual(result["total_warnings"], 2)
            self.assertEqual(result["unique_races"], 1)
            self.assertEqual(result["findings"][0]["frequency"], 2)

    def test_cpython_only_race(self):
        """CPython-internal-only race is identified."""
        with TempExtension({}) as root:
            path = self._write_report(root, CPYTHON_ONLY_RACE)
            result = tsan.analyze(path)
            self.assertEqual(result["extension_races"], 0)
            self.assertEqual(result["cpython_internal_races"], 1)
            self.assertTrue(result["findings"][0]["is_cpython_only"])

    def test_multiple_different_races(self):
        """Multiple different races are parsed as separate findings."""
        with TempExtension({}) as root:
            path = self._write_report(root, MULTI_RACE_REPORT)
            result = tsan.analyze(path)
            self.assertEqual(result["unique_races"], 2)

            # Both should be extension races.
            self.assertEqual(result["extension_races"], 2)

            funcs = {
                f["summary"]["function"] for f in result["findings"] if f.get("summary")
            }
            self.assertIn("update_counter", funcs)
            self.assertIn("set_flag", funcs)

    def test_empty_report(self):
        """Empty report produces zero findings."""
        with TempExtension({}) as root:
            path = self._write_report(root, EMPTY_REPORT)
            result = tsan.analyze(path)
            self.assertEqual(result["total_warnings"], 0)
            self.assertEqual(len(result["findings"]), 0)

    def test_no_tsan_output(self):
        """Non-TSan output produces zero findings."""
        with TempExtension({}) as root:
            path = self._write_report(root, NO_TSAN_OUTPUT)
            result = tsan.analyze(path)
            self.assertEqual(result["total_warnings"], 0)

    def test_missing_file(self):
        """Missing report file returns error."""
        result = tsan.analyze("/nonexistent/path/report.txt")
        self.assertIn("error", result)

    def test_summary_structure(self):
        """Output has expected summary structure."""
        with TempExtension({}) as root:
            path = self._write_report(root, SAMPLE_TSAN_REPORT)
            result = tsan.analyze(path)
            self.assertIn("report_path", result)
            self.assertIn("total_warnings", result)
            self.assertIn("unique_races", result)
            self.assertIn("extension_races", result)
            self.assertIn("cpython_internal_races", result)
            self.assertIn("findings", result)
            self.assertIn("summary", result)
            self.assertIn("actionable", result["summary"])

    def test_thread_info_parsed(self):
        """Thread creation info is parsed."""
        with TempExtension({}) as root:
            path = self._write_report(root, SAMPLE_TSAN_REPORT)
            result = tsan.analyze(path)
            finding = result["findings"][0]
            self.assertGreater(len(finding["thread_info"]), 0)


class TestParseStackFrame(unittest.TestCase):
    """Test individual frame parsing."""

    def test_frame_with_file_line_col(self):
        """Frame with file:line:col is parsed correctly."""
        line = "    #0 racy_increment /tmp/racymod.c:10:20 (racymod.so+0x11e0)"
        frame = tsan._parse_stack_frame(line)
        self.assertIsNotNone(frame)
        self.assertEqual(frame["function"], "racy_increment")
        self.assertEqual(frame["file"], "/tmp/racymod.c")
        self.assertEqual(frame["line"], 10)
        self.assertEqual(frame["col"], 20)

    def test_frame_with_null_location(self):
        """Frame with <null> location is parsed."""
        line = "    #0 pthread_create <null> (python+0xebeee)"
        frame = tsan._parse_stack_frame(line)
        self.assertIsNotNone(frame)
        self.assertEqual(frame["function"], "pthread_create")

    def test_cpython_frame_detected(self):
        """CPython internal frame is detected."""
        frame = {
            "location": "/home/user/cpython/Objects/methodobject.c:508:24",
            "module": "python+0x354640",
        }
        self.assertTrue(tsan._is_cpython_frame(frame))

    def test_extension_frame_not_cpython(self):
        """Extension frame is not detected as CPython."""
        frame = {
            "location": "/tmp/racy_ext/racymod.c:10:20",
            "module": "racymod.so+0x11e0",
        }
        self.assertFalse(tsan._is_cpython_frame(frame))


if __name__ == "__main__":
    unittest.main()
