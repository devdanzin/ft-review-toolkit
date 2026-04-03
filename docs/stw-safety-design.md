# StopTheWorld Safety Analysis — Design Document

## Motivation

Extensions like guppy3 that do heap traversal need `_PyEval_StopTheWorld` to safely walk object graphs without the GIL. But during a STW pause, all other threads are suspended — any operation that could block (waiting for another thread) or invoke Python code (which requires thread scheduling) is unsafe. Reasoning about what's safe during STW is extremely hard.

This design adds tools to help extension authors:
1. Know which CPython APIs are safe to call during STW
2. Verify that functions called during STW don't transitively invoke Python code
3. Check that STW/StartTheWorld are properly paired on all code paths

## 1. STW-Safe API Classification (`data/stw_safe_apis.json`)

During a STW pause, only operations that are **purely C-level** are safe. The key constraints:

- **No Python code execution**: No `PyObject_Call*`, no `PyObject_GetAttr` (could trigger `__getattr__`), no `PyObject_Str` (triggers `__str__`), no `PyErr_*` (some can trigger callbacks)
- **No GC invocation**: No allocation that could trigger GC (`PyObject_GC_New`, `PyList_New` with GC tracking). Raw `PyMem_Malloc` is safe (no GC). `PyList_New(0)` technically allocates but doesn't trigger GC collection.
- **No exception setting**: CPython's own GC calls `_PyEval_StartTheWorld` before `PyErr_NoMemory()` (see `gc_free_threading.c:2223-2224`), confirming exceptions are NOT safe during STW
- **No lock acquisition**: Other threads are stopped and may hold locks — acquiring any lock risks deadlock

### Safe during STW:
- Direct memory reads/writes (the whole point of STW)
- `Py_INCREF`/`Py_DECREF` (atomic refcount ops, no GC trigger in STW context)
- `Py_TYPE(obj)`, `PyTuple_GET_ITEM`, `PyList_GET_ITEM` (direct struct access, no bounds check calls)
- `PyLong_AsLong`, `PyFloat_AsDouble` (read existing values, no allocation)
- Raw pointer arithmetic on `ob_item` arrays
- `memcpy`, `memset`, pointer comparison
- `_Py_atomic_*` operations
- Reading `ob_refcnt`, `ob_type`, `ob_size`

### Unsafe during STW:
- `PyObject_Call*`, `PyObject_GetAttr*`, `PyObject_SetAttr*` — invoke Python code
- `PyObject_Str`, `PyObject_Repr`, `PyObject_Hash` — invoke Python code
- `PyErr_SetString`, `PyErr_Format`, `PyErr_NoMemory` — exception machinery
- `PyList_New`, `PyDict_New`, `PyTuple_New` — allocation may trigger GC
- `PyObject_GC_New`, `PyObject_GC_Track` — GC interaction
- `PyDict_GetItem`, `PyDict_SetItem` — may invoke `__hash__`/`__eq__`
- `PyImport_ImportModule` — triggers arbitrary code
- `Py_BEGIN_CRITICAL_SECTION` — acquires per-object lock (other threads may hold it... wait, they're stopped. Actually this IS safe since no other thread can hold it. Need to verify.)

### Gray area (safe with caveats):
- `Py_VISIT` macro — safe (just reads and calls visit function with the pointer)
- `PyMem_Malloc`/`PyMem_Free` — safe (raw allocator, no GC)
- `PyObject_Malloc` — depends on pymalloc state
- Simple type checks: `PyLong_Check`, `PyUnicode_Check` — safe (just compare `ob_type`)

## 2. Call Graph "Might Invoke Python" Scanner (`scan_stw_safety.py`)

### Approach

Build an intra-file call graph using Tree-sitter:

1. **Extract all functions** from the file
2. **Find all call expressions** in each function body
3. **Classify leaf calls** (calls to external functions):
   - Known STW-safe: from `stw_safe_apis.json`
   - Known STW-unsafe: from `stw_safe_apis.json`
   - Internal: calls to other functions in the same file → follow the graph
   - Unknown: calls to functions not in our classification → flag as UNKNOWN
4. **Propagate "might invoke Python"** up the call graph: if function A calls function B, and B might invoke Python, then A might invoke Python
5. **Detect STW violations**: if a function is called between `_PyEval_StopTheWorld` and `_PyEval_StartTheWorld`, and it might invoke Python → RACE finding

### Output

For each function, classify as:
- `stw_safe`: Only calls STW-safe APIs and other stw_safe functions
- `stw_unsafe`: Transitively calls a Python-invoking API
- `stw_unknown`: Calls at least one unclassified function (needs manual review)

For each STW region (code between Stop/Start), report:
- Functions called that are `stw_unsafe` → RACE/CRITICAL
- Functions called that are `stw_unknown` → PROTECT/MEDIUM

### Finding types:
- `stw_unsafe_call`: Function called during STW that may invoke Python
- `stw_exception_during_stw`: PyErr_* call during STW
- `stw_allocation_during_stw`: PyObject allocation during STW
- `stw_missing_start`: _PyEval_StopTheWorld without matching _PyEval_StartTheWorld

### Limitations:
- **Intra-file only**: Can't trace calls across .c files without a compilation database. Functions from other files are classified by the API tables or flagged as UNKNOWN.
- **No function pointer tracking**: If a function is called through a pointer (`callback(arg)`), we can't determine what it calls. Flag as UNKNOWN.
- **Conservative**: Unknown = flagged. This means some false positives on functions that call extension-internal helpers not in our API tables. The agent triages these.

## 3. STW Pairing in Lock Discipline Scanner

Add `_PyEval_StopTheWorld` / `_PyEval_StartTheWorld` to `lock_macros.json` as a pair. The existing `scan_lock_discipline.py` handles:
- Missing `_PyEval_StartTheWorld` (STW held forever → deadlock)
- Return between Stop and Start without Start (same bug pattern as missing lock release)
- Nested STW (double stop → deadlock)

Type: `stop_the_world`.

## 4. Enhanced stop-the-world-advisor Agent

Add STW contract guidance:
- Reference `stw_safe_apis.json` for what's safe
- Reference `scan_stw_safety.py` output for call-graph analysis
- Specific guidance on YiFei's questions:
  - "Are exceptions safe during STW?" → No, CPython's GC explicitly calls StartTheWorld before PyErr_NoMemory
  - "Can I call Py_INCREF during STW?" → Yes, atomic refcount ops are safe
  - "Can I call PyList_New during STW?" → No, allocation may trigger GC
  - "How to structure STW code?" → Do traversal/reads during STW, then StartTheWorld, then process results

## 5. New stw-safety-checker Agent

Script-backed agent using `scan_stw_safety.py`:
1. Run scanner to get function classifications and STW region violations
2. Triage each finding with context (40+ lines)
3. Verify call-graph classifications against actual function behavior
4. Report with specific fix recommendations
