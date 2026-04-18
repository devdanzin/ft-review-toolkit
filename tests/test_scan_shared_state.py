"""Tests for scan_shared_state.py — shared mutable state detection."""

import unittest
from helpers import import_script, TempExtension

ss = import_script("scan_shared_state")


GLOBAL_PYOBJECT = """\
#include <Python.h>

static PyObject *cache = NULL;
static PyObject *exception_type = NULL;

static PyObject *
get_cache(PyObject *self, PyObject *args)
{
    if (cache == NULL) {
        cache = PyDict_New();
    }
    Py_XINCREF(cache);
    return cache;
}

PyMODINIT_FUNC
PyInit_mymod(void)
{
    exception_type = PyErr_NewException("mymod.Error", NULL, NULL);
    return PyModule_Create(&module_def);
}
"""

THREAD_LOCAL = """\
#include <Python.h>

static __thread int per_thread_counter = 0;
static _Py_thread_local PyObject *tls_obj = NULL;

static PyObject *
get_counter(PyObject *self, PyObject *args)
{
    per_thread_counter++;
    return PyLong_FromLong(per_thread_counter);
}
"""

STATIC_TYPE = """\
#include <Python.h>

typedef struct {
    PyObject_HEAD
    int value;
} MyObj;

static PyTypeObject MyObjType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "mymod.MyObj",
    .tp_basicsize = sizeof(MyObj),
    .tp_flags = Py_TPFLAGS_DEFAULT,
};

PyMODINIT_FUNC
PyInit_mymod(void)
{
    PyType_Ready(&MyObjType);
    return PyModule_Create(&module_def);
}
"""

MODULE_STATE_GLOBAL = """\
#include <Python.h>

static struct PyModuleDef module_def = {
    PyModuleDef_HEAD_INIT,
    "mymod",
    NULL,
    -1,
    NULL
};

PyMODINIT_FUNC
PyInit_mymod(void)
{
    return PyModule_Create(&module_def);
}
"""

MODULE_STATE_PER_MODULE = """\
#include <Python.h>

typedef struct {
    PyObject *error_type;
} module_state;

static struct PyModuleDef module_def = {
    PyModuleDef_HEAD_INIT,
    "mymod",
    NULL,
    sizeof(module_state),
    NULL
};
"""

NON_ATOMIC_FLAG = """\
#include <Python.h>

static bool tracking_enabled = false;
static int active_count = 0;

static PyObject *
enable_tracking(PyObject *self, PyObject *args)
{
    tracking_enabled = true;
    return Py_None;
}

static PyObject *
do_work(PyObject *self, PyObject *args)
{
    active_count++;
    /* do work */
    active_count--;
    return Py_None;
}
"""

CONST_GLOBALS = """\
#include <Python.h>

static const char *module_name = "mymod";
static const int VERSION = 1;
"""

LOCK_PROTECTED = """\
#include <Python.h>
#include <pthread.h>

static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static PyObject *shared_dict = NULL;

static PyObject *
update_dict(PyObject *self, PyObject *args)
{
    pthread_mutex_lock(&lock);
    shared_dict = PyDict_New();
    pthread_mutex_unlock(&lock);
    Py_RETURN_NONE;
}
"""

INIT_ONLY_PRIMITIVE = """\
#include <Python.h>

static int initialized = 0;

PyMODINIT_FUNC
PyInit_mymod(void)
{
    initialized = 1;
    return PyModule_Create(&module_def);
}
"""


class TestScanSharedState(unittest.TestCase):
    """Test shared mutable state detection."""

    def test_global_pyobject_detected(self):
        """Global PyObject* variables are detected."""
        with TempExtension({"mymod.c": GLOBAL_PYOBJECT}) as root:
            result = ss.analyze(str(root))
            types = [f["type"] for f in result["findings"]]
            self.assertIn("unprotected_global_pyobject", types)
            # cache has non-init writes
            cache_findings = [f for f in result["findings"] if f["variable"] == "cache"]
            self.assertTrue(len(cache_findings) > 0)

    def test_thread_local_not_flagged(self):
        """Thread-local variables are classified as SAFE."""
        with TempExtension({"tls.c": THREAD_LOCAL}) as root:
            result = ss.analyze(str(root))
            safe = [f for f in result["findings"] if f["type"] == "thread_local_safe"]
            self.assertTrue(len(safe) > 0)
            # No PROTECT/RACE findings for thread-local vars.
            protect_findings = [
                f
                for f in result["findings"]
                if f["classification"] in ("PROTECT", "RACE")
                and f["variable"] in ("per_thread_counter", "tls_obj")
            ]
            self.assertEqual(len(protect_findings), 0)

    def test_static_type_detected(self):
        """Static PyTypeObject is detected as MIGRATE."""
        with TempExtension({"typed.c": STATIC_TYPE}) as root:
            result = ss.analyze(str(root))
            type_findings = [
                f for f in result["findings"] if f["type"] == "static_type_object"
            ]
            self.assertTrue(len(type_findings) > 0)
            self.assertEqual(type_findings[0]["classification"], "MIGRATE")

    def test_module_state_globals(self):
        """PyModuleDef with m_size=-1 is detected."""
        with TempExtension({"mod.c": MODULE_STATE_GLOBAL}) as root:
            result = ss.analyze(str(root))
            ms_findings = [
                f for f in result["findings"] if f["type"] == "module_state_in_globals"
            ]
            self.assertTrue(len(ms_findings) > 0)
            self.assertEqual(ms_findings[0]["classification"], "MIGRATE")

    def test_per_module_state_not_flagged(self):
        """PyModuleDef with positive m_size is not flagged."""
        with TempExtension({"mod.c": MODULE_STATE_PER_MODULE}) as root:
            result = ss.analyze(str(root))
            ms_findings = [
                f for f in result["findings"] if f["type"] == "module_state_in_globals"
            ]
            self.assertEqual(len(ms_findings), 0)

    def test_non_atomic_flag_detected(self):
        """Non-atomic shared bool/int are detected."""
        with TempExtension({"flags.c": NON_ATOMIC_FLAG}) as root:
            result = ss.analyze(str(root))
            flag_findings = [
                f
                for f in result["findings"]
                if f["type"] == "non_atomic_shared_flag" and f["severity"] == "HIGH"
            ]
            self.assertTrue(len(flag_findings) > 0)

    def test_const_globals_not_flagged(self):
        """Const static variables are not flagged."""
        with TempExtension({"const.c": CONST_GLOBALS}) as root:
            result = ss.analyze(str(root))
            # Should have no findings (consts are skipped).
            non_safe = [f for f in result["findings"] if f["classification"] != "SAFE"]
            self.assertEqual(len(non_safe), 0)

    def test_init_only_write_lower_severity(self):
        """Variables written only in init functions get lower severity."""
        with TempExtension({"init.c": INIT_ONLY_PRIMITIVE}) as root:
            result = ss.analyze(str(root))
            init_findings = [
                f for f in result["findings"] if f["variable"] == "initialized"
            ]
            self.assertTrue(len(init_findings) > 0, "Expected init-only findings")
            self.assertIn(init_findings[0]["severity"], ("LOW",))

    def test_lock_protected_noted(self):
        """Lock-protected variables are noted in findings."""
        with TempExtension({"locked.c": LOCK_PROTECTED}) as root:
            result = ss.analyze(str(root))
            locked = [f for f in result["findings"] if f.get("lock_protected")]
            self.assertTrue(len(locked) > 0)

    def test_output_envelope(self):
        """Output has the standard JSON envelope."""
        with TempExtension({"mymod.c": GLOBAL_PYOBJECT}) as root:
            result = ss.analyze(str(root))
            self.assertIn("project_root", result)
            self.assertIn("scan_root", result)
            self.assertIn("files_analyzed", result)
            self.assertIn("functions_analyzed", result)
            self.assertIn("findings", result)
            self.assertIn("summary", result)
            self.assertIn("skipped_files", result)
            # Scanner actually processed input (silent-failure guard).
            self.assertGreater(result["files_analyzed"], 0)


if __name__ == "__main__":
    unittest.main()
