"""Tests for scan_atomic_candidates.py — atomic candidate detection."""

import unittest
from helpers import import_script, TempExtension

ac = import_script("scan_atomic_candidates")


NON_ATOMIC_BOOL = """\
#include <Python.h>

static bool tracking_enabled = false;

static PyObject *
enable_tracking(PyObject *self, PyObject *args)
{
    tracking_enabled = true;
    Py_RETURN_NONE;
}

static PyObject *
is_tracking(PyObject *self, PyObject *args)
{
    if (tracking_enabled) {
        Py_RETURN_TRUE;
    }
    Py_RETURN_FALSE;
}
"""

NON_ATOMIC_INT = """\
#include <Python.h>

static int active_count = 0;

static PyObject *
start_work(PyObject *self, PyObject *args)
{
    active_count++;
    Py_RETURN_NONE;
}

static PyObject *
end_work(PyObject *self, PyObject *args)
{
    active_count--;
    Py_RETURN_NONE;
}

static PyObject *
get_count(PyObject *self, PyObject *args)
{
    return PyLong_FromLong(active_count);
}
"""

NON_ATOMIC_POINTER = """\
#include <Python.h>

static void *current_handler = NULL;

static PyObject *
set_handler(PyObject *self, PyObject *args)
{
    current_handler = (void *)0x1234;
    Py_RETURN_NONE;
}

static PyObject *
get_handler(PyObject *self, PyObject *args)
{
    if (current_handler != NULL) {
        Py_RETURN_TRUE;
    }
    Py_RETURN_FALSE;
}
"""

ALREADY_ATOMIC = """\
#include <Python.h>

static _Py_atomic_int initialized = 0;

static PyObject *
check_init(PyObject *self, PyObject *args)
{
    if (_Py_atomic_load_int(&initialized) == 0) {
        _Py_atomic_store_int(&initialized, 1);
    }
    Py_RETURN_NONE;
}
"""

THREAD_LOCAL_VAR = """\
#include <Python.h>

static __thread int per_thread_count = 0;

static PyObject *
increment(PyObject *self, PyObject *args)
{
    per_thread_count++;
    return PyLong_FromLong(per_thread_count);
}
"""

INIT_ONLY_VAR = """\
#include <Python.h>

static int module_version = 0;

PyMODINIT_FUNC
PyInit_mymod(void)
{
    module_version = 42;
    return PyModule_Create(&module_def);
}
"""

CONST_VAR = """\
#include <Python.h>

static const int MAX_SIZE = 1024;
"""

PYOBJECT_GLOBAL = """\
#include <Python.h>

static PyObject *cache = NULL;

static PyObject *
set_cache(PyObject *self, PyObject *args)
{
    cache = PyDict_New();
    Py_RETURN_NONE;
}
"""


class TestScanAtomicCandidates(unittest.TestCase):
    """Test atomic candidate detection."""

    def test_non_atomic_bool_detected(self):
        """Non-atomic shared bool is detected."""
        with TempExtension({"flag.c": NON_ATOMIC_BOOL}) as root:
            result = ac.analyze(str(root))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("non_atomic_shared_bool", types)
            finding = next(
                f for f in result["findings"] if f["type"] == "non_atomic_shared_bool"
            )
            self.assertEqual(finding["classification"], "PROTECT")
            self.assertIn("_Py_atomic_int", finding["suggested_atomic"])

    def test_non_atomic_int_detected(self):
        """Non-atomic shared int with increment/decrement is detected."""
        with TempExtension({"counter.c": NON_ATOMIC_INT}) as root:
            result = ac.analyze(str(root))
            protect = [
                f for f in result["findings"] if f["classification"] == "PROTECT"
            ]
            self.assertTrue(len(protect) > 0)
            # Should detect writes from multiple functions.
            finding = protect[0]
            self.assertGreater(len(finding.get("write_functions", [])), 1)

    def test_non_atomic_pointer_detected(self):
        """Non-atomic shared pointer is detected."""
        with TempExtension({"ptr.c": NON_ATOMIC_POINTER}) as root:
            result = ac.analyze(str(root))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("non_atomic_shared_pointer", types)

    def test_already_atomic_ok(self):
        """Already-atomic variable is classified as SAFE."""
        with TempExtension({"atomic.c": ALREADY_ATOMIC}) as root:
            result = ac.analyze(str(root))
            safe = [f for f in result["findings"] if f["type"] == "existing_atomic_ok"]
            self.assertTrue(len(safe) > 0)

    def test_thread_local_not_flagged(self):
        """Thread-local variable is not flagged as needing atomics."""
        with TempExtension({"tls.c": THREAD_LOCAL_VAR}) as root:
            result = ac.analyze(str(root))
            protect = [
                f for f in result["findings"] if f["classification"] == "PROTECT"
            ]
            self.assertEqual(len(protect), 0)

    def test_init_only_not_flagged(self):
        """Init-only primitive variable is not flagged as atomic candidate."""
        with TempExtension({"init.c": INIT_ONLY_VAR}) as root:
            result = ac.analyze(str(root))
            findings = [
                f for f in result["findings"] if f["variable"] == "module_version"
            ]
            self.assertEqual(len(findings), 0)

    def test_const_not_flagged(self):
        """Const variable is not flagged."""
        with TempExtension({"const.c": CONST_VAR}) as root:
            result = ac.analyze(str(root))
            self.assertEqual(len(result["findings"]), 0)

    def test_pyobject_skipped(self):
        """PyObject* globals are skipped (handled by shared-state-auditor)."""
        with TempExtension({"obj.c": PYOBJECT_GLOBAL}) as root:
            result = ac.analyze(str(root))
            # Should have no atomic findings for PyObject*.
            atomic_findings = [
                f for f in result["findings"] if f["type"].startswith("non_atomic")
            ]
            self.assertEqual(len(atomic_findings), 0)

    def test_output_envelope(self):
        """Output has the standard JSON envelope."""
        with TempExtension({"flag.c": NON_ATOMIC_BOOL}) as root:
            result = ac.analyze(str(root))
            self.assertIn("project_root", result)
            self.assertIn("scan_root", result)
            self.assertIn("files_analyzed", result)
            self.assertIn("functions_analyzed", result)
            self.assertIn("findings", result)
            self.assertIn("summary", result)
            self.assertIn("skipped_files", result)
            # Scanner actually processed input (silent-failure guard).
            self.assertGreater(result["files_analyzed"], 0)
            self.assertGreater(result["functions_analyzed"], 0)


if __name__ == "__main__":
    unittest.main()
