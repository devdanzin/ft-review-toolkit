"""Tests for scan_stw_safety.py — StopTheWorld safety analysis."""

import unittest
from helpers import import_script, TempExtension

stw = import_script("scan_stw_safety")


STW_WITH_UNSAFE_CALL = """\
#include <Python.h>

static PyObject *
traverse_heap(PyObject *self, PyObject *args)
{
    PyInterpreterState *interp = _PyInterpreterState_GET();
    _PyEval_StopTheWorld(interp);

    /* UNSAFE: PyErr_Format with %R invokes PyObject_Repr — always unsafe */
    PyErr_Format(PyExc_RuntimeError, "bad: %R", args);

    _PyEval_StartTheWorld(interp);
    return NULL;
}
"""

STW_WITH_PYTHON_CALL = """\
#include <Python.h>

static PyObject *
bad_stw(PyObject *self, PyObject *args)
{
    PyInterpreterState *interp = _PyInterpreterState_GET();
    _PyEval_StopTheWorld(interp);

    /* UNSAFE: PyObject_Str invokes __str__ */
    PyObject *s = PyObject_Str(args);

    _PyEval_StartTheWorld(interp);
    return s;
}
"""

STW_WITH_ALLOCATION = """\
#include <Python.h>

static PyObject *
alloc_during_stw(PyObject *self, PyObject *args)
{
    PyInterpreterState *interp = _PyInterpreterState_GET();
    _PyEval_StopTheWorld(interp);

    /* SAFE on 3.14+: PyList_New allocates but does NOT trigger GC
       (GC runs only on eval breaker, not during allocation) */
    PyObject *result = PyList_New(0);

    _PyEval_StartTheWorld(interp);
    return result;
}
"""

STW_SAFE_ONLY = """\
#include <Python.h>

static PyObject *
safe_traverse(PyObject *self, PyObject *args)
{
    PyInterpreterState *interp = _PyInterpreterState_GET();
    _PyEval_StopTheWorld(interp);

    /* SAFE: direct struct access, type checks, refcounting */
    PyObject *item = PyTuple_GET_ITEM(args, 0);
    Py_INCREF(item);
    int is_long = PyLong_Check(item);

    _PyEval_StartTheWorld(interp);
    return item;
}
"""

STW_WITH_INTERNAL_UNSAFE = """\
#include <Python.h>

static void helper_that_calls_python(PyObject *obj)
{
    PyObject *s = PyObject_Str(obj);
    Py_XDECREF(s);
}

static PyObject *
stw_calls_helper(PyObject *self, PyObject *args)
{
    PyInterpreterState *interp = _PyInterpreterState_GET();
    _PyEval_StopTheWorld(interp);

    /* UNSAFE transitively: helper_that_calls_python calls PyObject_Str */
    helper_that_calls_python(args);

    _PyEval_StartTheWorld(interp);
    Py_RETURN_NONE;
}
"""

STW_WITH_UNKNOWN_CALL = """\
#include <Python.h>

extern void unknown_external_function(PyObject *obj);

static PyObject *
stw_unknown(PyObject *self, PyObject *args)
{
    PyInterpreterState *interp = _PyInterpreterState_GET();
    _PyEval_StopTheWorld(interp);

    /* UNKNOWN: can't determine if this invokes Python */
    unknown_external_function(args);

    _PyEval_StartTheWorld(interp);
    Py_RETURN_NONE;
}
"""

NO_STW = """\
#include <Python.h>

static PyObject *
normal_func(PyObject *self, PyObject *args)
{
    return PyUnicode_FromString("hello");
}
"""

STW_MISSING_START = """\
#include <Python.h>

static PyObject *
missing_start(PyObject *self, PyObject *args)
{
    PyInterpreterState *interp = _PyInterpreterState_GET();
    _PyEval_StopTheWorld(interp);

    PyObject *item = PyTuple_GET_ITEM(args, 0);
    /* Missing _PyEval_StartTheWorld! */
    return item;
}
"""


class TestScanStwSafety(unittest.TestCase):
    """Test StopTheWorld safety analysis."""

    def test_unsafe_exception_detected(self):
        """PyErr_Format (always unsafe) in STW region is detected."""
        with TempExtension({"stw.c": STW_WITH_UNSAFE_CALL}) as root:
            result = stw.analyze(str(root))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("stw_exception_during_stw", types)
            finding = next(
                f for f in result["findings"] if f["type"] == "stw_exception_during_stw"
            )
            self.assertEqual(finding["severity"], "CRITICAL")

    def test_python_call_in_stw_detected(self):
        """PyObject_Str in STW region is detected."""
        with TempExtension({"stw.c": STW_WITH_PYTHON_CALL}) as root:
            result = stw.analyze(str(root))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("stw_unsafe_call", types)
            finding = next(
                f for f in result["findings"] if f["type"] == "stw_unsafe_call"
            )
            self.assertEqual(finding["api_call"], "PyObject_Str")

    def test_allocation_in_stw_safe_on_314(self):
        """PyList_New in STW region is safe on 3.14+ (no GC trigger)."""
        with TempExtension({"stw.c": STW_WITH_ALLOCATION}) as root:
            result = stw.analyze(str(root))
            alloc_findings = [
                f
                for f in result["findings"]
                if f["type"] == "stw_allocation_during_stw"
            ]
            self.assertEqual(len(alloc_findings), 0)

    def test_safe_stw_no_findings(self):
        """Safe operations in STW region produce no findings."""
        with TempExtension({"stw.c": STW_SAFE_ONLY}) as root:
            result = stw.analyze(str(root))
            unsafe = [
                f
                for f in result["findings"]
                if f["type"].startswith("stw_unsafe")
                or f["type"].startswith("stw_exception")
                or f["type"].startswith("stw_allocation")
            ]
            self.assertEqual(len(unsafe), 0)

    def test_transitive_unsafe_detected(self):
        """Function calling helper that invokes Python is detected."""
        with TempExtension({"stw.c": STW_WITH_INTERNAL_UNSAFE}) as root:
            result = stw.analyze(str(root))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("stw_unsafe_call", types)
            # Should flag the helper call, not just the leaf PyObject_Str.
            findings = [f for f in result["findings"] if f["type"] == "stw_unsafe_call"]
            self.assertTrue(
                any("helper_that_calls_python" in f["api_call"] for f in findings)
            )

    def test_unknown_call_flagged(self):
        """Unknown external function in STW is flagged as PROTECT."""
        with TempExtension({"stw.c": STW_WITH_UNKNOWN_CALL}) as root:
            result = stw.analyze(str(root))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("stw_unknown_call", types)
            finding = next(
                f for f in result["findings"] if f["type"] == "stw_unknown_call"
            )
            self.assertEqual(finding["classification"], "PROTECT")

    def test_no_stw_no_findings(self):
        """File without STW produces no findings."""
        with TempExtension({"normal.c": NO_STW}) as root:
            result = stw.analyze(str(root))
            self.assertEqual(len(result["findings"]), 0)

    def test_stw_functions_reported(self):
        """Functions containing STW are listed."""
        with TempExtension({"stw.c": STW_SAFE_ONLY}) as root:
            result = stw.analyze(str(root))
            self.assertGreater(len(result["stw_functions"]), 0)
            self.assertEqual(result["stw_functions"][0]["function"], "safe_traverse")

    def test_function_classifications(self):
        """Call graph classifications are produced."""
        with TempExtension({"stw.c": STW_WITH_INTERNAL_UNSAFE}) as root:
            result = stw.analyze(str(root))
            # Should have classifications for the file.
            self.assertTrue(len(result["function_classifications"]) > 0)
            for file_classifs in result["function_classifications"].values():
                # helper_that_calls_python should be classified as unsafe.
                self.assertEqual(
                    file_classifs.get("helper_that_calls_python"), "unsafe"
                )

    def test_output_envelope(self):
        """Output has the standard JSON envelope."""
        with TempExtension({"stw.c": STW_WITH_UNSAFE_CALL}) as root:
            result = stw.analyze(str(root))
            self.assertIn("project_root", result)
            self.assertIn("scan_root", result)
            self.assertIn("files_analyzed", result)
            self.assertIn("findings", result)
            self.assertIn("summary", result)
            self.assertIn("stw_functions", result)
            self.assertIn("function_classifications", result)


if __name__ == "__main__":
    unittest.main()
