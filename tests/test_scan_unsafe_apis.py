"""Tests for scan_unsafe_apis.py — thread-unsafe API detection."""

import unittest
from helpers import import_script, TempExtension

ua = import_script("scan_unsafe_apis")


API_WITHOUT_GIL = """\
#include <Python.h>

static PyObject *
bad_func(PyObject *self, PyObject *args)
{
    Py_BEGIN_ALLOW_THREADS
    PyObject *obj = PyLong_FromLong(42);
    Py_END_ALLOW_THREADS
    return obj;
}
"""

SAFE_GIL_RELEASED = """\
#include <Python.h>
#include <unistd.h>

static PyObject *
good_func(PyObject *self, PyObject *args)
{
    int result;
    Py_BEGIN_ALLOW_THREADS
    result = sleep(1);
    Py_END_ALLOW_THREADS
    return PyLong_FromLong(result);
}
"""

BORROWED_REF = """\
#include <Python.h>

static PyObject *
borrowed_func(PyObject *self, PyObject *args)
{
    PyObject *dict = PyDict_New();
    PyDict_SetItemString(dict, "key", Py_None);

    PyObject *item = PyDict_GetItem(dict, PyUnicode_FromString("key"));
    /* No Py_INCREF — borrowed ref is unprotected */
    PyObject *result = PyObject_Str(item);
    Py_DECREF(dict);
    return result;
}
"""

BORROWED_REF_PROTECTED = """\
#include <Python.h>

static PyObject *
protected_func(PyObject *self, PyObject *args)
{
    PyObject *dict = PyDict_New();
    PyObject *item = PyDict_GetItem(dict, PyUnicode_FromString("key"));
    Py_XINCREF(item);
    /* item is now protected */
    PyObject *result = PyObject_Str(item);
    Py_XDECREF(item);
    Py_DECREF(dict);
    return result;
}
"""

GILSTATE_USAGE = """\
#include <Python.h>

static void
callback_func(void *data)
{
    PyGILState_STATE gstate = PyGILState_Ensure();
    PyObject *result = PyLong_FromLong(42);
    Py_XDECREF(result);
    PyGILState_Release(gstate);
}
"""

CONTAINER_MUTATION_GLOBAL = """\
#include <Python.h>

static PyObject *cache = NULL;

static PyObject *
add_to_cache(PyObject *self, PyObject *args)
{
    PyObject *item;
    if (!PyArg_ParseTuple(args, "O", &item))
        return NULL;
    PyList_Append(cache, item);
    Py_RETURN_NONE;
}
"""

DEPRECATED_API = """\
#include <Python.h>

static PyObject *
old_error_handling(PyObject *self, PyObject *args)
{
    PyObject *type, *value, *tb;
    PyErr_Fetch(&type, &value, &tb);
    if (type != NULL) {
        PyErr_Restore(type, value, tb);
    }
    Py_RETURN_NONE;
}
"""

CLEAN_EXTENSION = """\
#include <Python.h>

static PyObject *
simple_func(PyObject *self, PyObject *args)
{
    return PyUnicode_FromString("hello");
}
"""


class TestScanUnsafeApis(unittest.TestCase):
    """Test thread-unsafe API detection."""

    def test_api_without_gil_detected(self):
        """Python API call in GIL-released region is detected."""
        with TempExtension({"bad.c": API_WITHOUT_GIL}) as root:
            result = ua.analyze(str(root))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("unsafe_api_without_gil", types)
            finding = next(
                f for f in result["findings"] if f["type"] == "unsafe_api_without_gil"
            )
            self.assertEqual(finding["severity"], "CRITICAL")
            self.assertEqual(finding["api_call"], "PyLong_FromLong")

    def test_safe_gil_released_not_flagged(self):
        """Pure C calls in GIL-released region are not flagged."""
        with TempExtension({"good.c": SAFE_GIL_RELEASED}) as root:
            result = ua.analyze(str(root))
            unsafe = [
                f for f in result["findings"] if f["type"] == "unsafe_api_without_gil"
            ]
            self.assertEqual(len(unsafe), 0)

    def test_borrowed_ref_detected(self):
        """Borrowed reference without Py_INCREF is detected."""
        with TempExtension({"borrowed.c": BORROWED_REF}) as root:
            result = ua.analyze(str(root))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("borrowed_ref_unprotected", types)

    def test_borrowed_ref_protected_not_flagged(self):
        """Borrowed reference with Py_XINCREF is not flagged."""
        with TempExtension({"protected.c": BORROWED_REF_PROTECTED}) as root:
            result = ua.analyze(str(root))
            unprotected = [
                f for f in result["findings"] if f["type"] == "borrowed_ref_unprotected"
            ]
            self.assertEqual(len(unprotected), 0)

    def test_gilstate_detected(self):
        """PyGILState_Ensure/Release usage is detected."""
        with TempExtension({"gilstate.c": GILSTATE_USAGE}) as root:
            result = ua.analyze(str(root))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("gilstate_noop", types)

    def test_container_mutation_global(self):
        """Container mutation on global PyObject* is detected."""
        with TempExtension({"mutate.c": CONTAINER_MUTATION_GLOBAL}) as root:
            result = ua.analyze(str(root))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("container_mutation_unprotected", types)

    def test_deprecated_api_detected(self):
        """Deprecated thread-unsafe APIs are detected."""
        with TempExtension({"deprecated.c": DEPRECATED_API}) as root:
            result = ua.analyze(str(root))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("deprecated_thread_api", types)
            dep_findings = [
                f for f in result["findings"] if f["type"] == "deprecated_thread_api"
            ]
            api_calls = {f["api_call"] for f in dep_findings}
            self.assertIn("PyErr_Fetch", api_calls)

    def test_clean_extension_no_findings(self):
        """Clean extension produces no unsafe API findings."""
        with TempExtension({"clean.c": CLEAN_EXTENSION}) as root:
            result = ua.analyze(str(root))
            self.assertEqual(len(result["findings"]), 0)

    def test_output_envelope(self):
        """Output has the standard JSON envelope."""
        with TempExtension({"bad.c": API_WITHOUT_GIL}) as root:
            result = ua.analyze(str(root))
            self.assertIn("project_root", result)
            self.assertIn("scan_root", result)
            self.assertIn("files_analyzed", result)
            self.assertIn("functions_analyzed", result)
            self.assertIn("findings", result)
            self.assertIn("summary", result)
            self.assertIn("skipped_files", result)


if __name__ == "__main__":
    unittest.main()
