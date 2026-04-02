"""Tests for scan_lock_discipline.py — lock discipline analysis."""

import unittest
from helpers import import_script, TempExtension

ld = import_script("scan_lock_discipline")


MISSING_RELEASE = """\
#include <Python.h>

static PyObject *
bad_lock(PyObject *self, PyObject *args)
{
    PyThread_acquire_lock(lock, WAIT_LOCK);
    PyObject *result = do_work();
    /* missing PyThread_release_lock */
    return result;
}
"""

PROPER_LOCK = """\
#include <Python.h>

static PyObject *
good_lock(PyObject *self, PyObject *args)
{
    PyThread_acquire_lock(lock, WAIT_LOCK);
    PyObject *result = do_work();
    PyThread_release_lock(lock);
    return result;
}
"""

MISSING_RELEASE_ON_ERROR = """\
#include <Python.h>

static PyObject *
error_path(PyObject *self, PyObject *args)
{
    PyThread_acquire_lock(lock, WAIT_LOCK);
    PyObject *result = do_work();
    if (result == NULL) {
        return NULL;  /* Lock not released! */
    }
    PyThread_release_lock(lock);
    return result;
}
"""

ERROR_PATH_WITH_RELEASE = """\
#include <Python.h>

static PyObject *
good_error_path(PyObject *self, PyObject *args)
{
    PyThread_acquire_lock(lock, WAIT_LOCK);
    PyObject *result = do_work();
    if (result == NULL) {
        PyThread_release_lock(lock);
        return NULL;
    }
    PyThread_release_lock(lock);
    return result;
}
"""

ERROR_PATH_WITH_GOTO = """\
#include <Python.h>

static PyObject *
goto_cleanup(PyObject *self, PyObject *args)
{
    PyObject *result = NULL;
    PyThread_acquire_lock(lock, WAIT_LOCK);
    PyObject *tmp = do_work();
    if (tmp == NULL) {
        goto cleanup;
    }
    result = tmp;
cleanup:
    PyThread_release_lock(lock);
    return result;
}
"""

NESTED_LOCKS = """\
#include <Python.h>
#include <pthread.h>

static PyObject *
nested(PyObject *self, PyObject *args)
{
    PyThread_acquire_lock(lock_a, WAIT_LOCK);
    pthread_mutex_lock(&mutex_b);
    do_work();
    pthread_mutex_unlock(&mutex_b);
    PyThread_release_lock(lock_a);
    Py_RETURN_NONE;
}
"""

CRITICAL_SECTION_CANDIDATE = """\
#include <Python.h>

typedef struct {
    PyObject_HEAD
    PyObject *data;
    int count;
} MyObj;

static PyObject *
MyObj_get_data(MyObj *self, PyObject *args)
{
    self->count++;
    Py_INCREF(self->data);
    return self->data;
}
"""

ALREADY_CRITICAL_SECTION = """\
#include <Python.h>

typedef struct {
    PyObject_HEAD
    PyObject *data;
} MyObj;

static PyObject *
MyObj_get_data(MyObj *self, PyObject *args)
{
    Py_BEGIN_CRITICAL_SECTION(self);
    Py_INCREF(self->data);
    PyObject *result = self->data;
    Py_END_CRITICAL_SECTION(self);
    return result;
}
"""

ZLIB_LOCK = """\
#include <Python.h>

static PyObject *
compress_data(compobject *self, PyObject *args)
{
    ENTER_ZLIB(self);
    int result = deflate(&self->zst, Z_NO_FLUSH);
    if (result != Z_OK) {
        LEAVE_ZLIB(self);
        return NULL;
    }
    LEAVE_ZLIB(self);
    Py_RETURN_NONE;
}
"""

ZLIB_LOCK_MISSING_LEAVE = """\
#include <Python.h>

static PyObject *
compress_bad(compobject *self, PyObject *args)
{
    ENTER_ZLIB(self);
    int result = deflate(&self->zst, Z_NO_FLUSH);
    if (result != Z_OK) {
        return NULL;  /* Missing LEAVE_ZLIB! */
    }
    LEAVE_ZLIB(self);
    Py_RETURN_NONE;
}
"""

NO_LOCKS = """\
#include <Python.h>

static PyObject *
simple_func(PyObject *self, PyObject *args)
{
    return PyUnicode_FromString("hello");
}
"""


class TestScanLockDiscipline(unittest.TestCase):
    """Test lock discipline analysis."""

    def test_missing_release_detected(self):
        """Missing lock release is detected."""
        with TempExtension({"bad.c": MISSING_RELEASE}) as root:
            result = ld.analyze(str(root))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("missing_release", types)

    def test_proper_lock_no_findings(self):
        """Properly paired lock/unlock has no pairing findings."""
        with TempExtension({"good.c": PROPER_LOCK}) as root:
            result = ld.analyze(str(root))
            pairing = [
                f
                for f in result["findings"]
                if f["type"] in ("missing_release", "missing_release_on_error")
            ]
            self.assertEqual(len(pairing), 0)

    def test_missing_release_on_error(self):
        """Missing release on error return path is detected."""
        with TempExtension({"err.c": MISSING_RELEASE_ON_ERROR}) as root:
            result = ld.analyze(str(root))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("missing_release_on_error", types)

    def test_error_path_with_release_ok(self):
        """Error path that releases lock is not flagged."""
        with TempExtension({"ok.c": ERROR_PATH_WITH_RELEASE}) as root:
            result = ld.analyze(str(root))
            err_findings = [
                f for f in result["findings"] if f["type"] == "missing_release_on_error"
            ]
            self.assertEqual(len(err_findings), 0)

    def test_goto_cleanup_recognized(self):
        """Goto to cleanup label that releases lock is recognized."""
        with TempExtension({"goto.c": ERROR_PATH_WITH_GOTO}) as root:
            result = ld.analyze(str(root))
            err_findings = [
                f for f in result["findings"] if f["type"] == "missing_release_on_error"
            ]
            self.assertEqual(len(err_findings), 0)

    def test_nested_locks_detected(self):
        """Nested lock acquisition is detected."""
        with TempExtension({"nested.c": NESTED_LOCKS}) as root:
            result = ld.analyze(str(root))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("nested_locks", types)

    def test_critical_section_candidate(self):
        """Function accessing self->member without protection is flagged."""
        with TempExtension({"cs.c": CRITICAL_SECTION_CANDIDATE}) as root:
            result = ld.analyze(str(root))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("critical_section_candidate", types)

    def test_already_critical_section_not_flagged(self):
        """Function using Py_BEGIN_CRITICAL_SECTION is not flagged."""
        with TempExtension({"cs_ok.c": ALREADY_CRITICAL_SECTION}) as root:
            result = ld.analyze(str(root))
            cs_findings = [
                f
                for f in result["findings"]
                if f["type"] == "critical_section_candidate"
            ]
            self.assertEqual(len(cs_findings), 0)

    def test_zlib_lock_pattern(self):
        """ENTER_ZLIB/LEAVE_ZLIB pairing is recognized."""
        with TempExtension({"zlib.c": ZLIB_LOCK}) as root:
            result = ld.analyze(str(root))
            err_findings = [
                f for f in result["findings"] if f["type"] == "missing_release_on_error"
            ]
            self.assertEqual(len(err_findings), 0)

    def test_zlib_lock_missing_leave(self):
        """Missing LEAVE_ZLIB on error path is detected."""
        with TempExtension({"zlib_bad.c": ZLIB_LOCK_MISSING_LEAVE}) as root:
            result = ld.analyze(str(root))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("missing_release_on_error", types)

    def test_no_locks_no_findings(self):
        """File with no locks produces no lock-related findings."""
        with TempExtension({"simple.c": NO_LOCKS}) as root:
            result = ld.analyze(str(root))
            lock_findings = [
                f
                for f in result["findings"]
                if f["type"]
                in ("missing_release", "missing_release_on_error", "nested_locks")
            ]
            self.assertEqual(len(lock_findings), 0)

    def test_output_envelope(self):
        """Output has the standard JSON envelope."""
        with TempExtension({"bad.c": MISSING_RELEASE}) as root:
            result = ld.analyze(str(root))
            self.assertIn("project_root", result)
            self.assertIn("scan_root", result)
            self.assertIn("files_analyzed", result)
            self.assertIn("findings", result)
            self.assertIn("summary", result)


if __name__ == "__main__":
    unittest.main()
