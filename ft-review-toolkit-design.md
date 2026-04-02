# ft-review-toolkit — Design Document

## 1. Project Identity

**Name:** ft-review-toolkit
**Purpose:** A Claude Code plugin for analyzing and migrating CPython C extensions to free-threaded Python (PEP 703). Finds thread-safety bugs, plans migrations, and triages ThreadSanitizer reports.

**Tagline:** *Make your C extension free-threading safe.*

### 1.1 Relationship to Sibling Projects

| Project | Target | Key Concern |
|---------|--------|-------------|
| code-review-toolkit | Python source | Logic errors, dead code, test gaps |
| cpython-review-toolkit | CPython runtime C code | Refcount leaks, GIL, NULL safety |
| cext-review-toolkit | C extensions (correctness) | API misuse, borrowed ref lifetime, type slots, ABI |
| **ft-review-toolkit** | **C extensions (thread safety)** | **Data races, lock discipline, shared state, free-threading migration** |

**Key distinction from cext-review-toolkit:** cext-review-toolkit answers "does my extension have bugs?" ft-review-toolkit answers "is my extension safe without the GIL, and how do I make it safe?" These are complementary — an extension should pass cext-review-toolkit first (no correctness bugs), then ft-review-toolkit (no thread-safety bugs).

**Shared infrastructure:** ft-review-toolkit imports from cext-review-toolkit's `tree_sitter_utils.py` and `scan_common.py`. It uses the same Tree-sitter parsing layer, the same `analyze()` + `main()` script convention, and the same `unittest`-based test framework. It does NOT duplicate cext-review-toolkit's scanners — it adds new scanners for thread-safety concerns.

### 1.2 Audience

- C extension authors preparing for free-threaded Python (3.13t, 3.14t+)
- Maintainers who have declared `Py_MOD_GIL_NOT_USED` and need to verify correctness
- Developers debugging data races found by ThreadSanitizer
- Teams planning the migration from GIL-protected to free-threaded code

### 1.3 Non-Goals

- Analyzing pure Python threading bugs (use code-review-toolkit or standard Python tools)
- Replacing ThreadSanitizer (we triage its output, not reproduce its analysis)
- Analyzing CPython's own free-threading implementation (use cpython-review-toolkit)
- Requiring a free-threaded Python build to run (must work on raw source files; TSan integration is optional)


## 2. Architecture

### 2.1 Parsing Layer

Same as cext-review-toolkit: Tree-sitter for C/C++ parsing via `tree_sitter_utils.py`. All scripts import from cext-review-toolkit's shared modules.

**Dependencies:**
- `tree-sitter` and `tree-sitter-c` (required)
- `tree-sitter-cpp` (optional, for C++ extensions)
- cext-review-toolkit (required — shared parsing and discovery infrastructure)

### 2.2 TSan Integration (Optional)

ThreadSanitizer reports are the highest-signal input for finding real data races. ft-review-toolkit can consume TSan reports but does not require them.

**Input formats:**
- TSan text output (stderr capture from running tests under a TSan-enabled Python)
- TSan JSON/XML if available via LLVM tooling

**Pipeline:**
```
TSan report → parse_tsan_report.py → structured findings JSON
     ↓
Source code → scan_shared_state.py → candidate shared state JSON
     ↓
tsan-report-analyzer agent → triaged findings with fixes
```

**What the user provides:** A TSan report file (e.g., from running `python -m pytest` with a TSan-enabled CPython build). The toolkit provides guidance on how to generate this:
```bash
# Example: run extension tests under TSan Python
cd /path/to/extension
/path/to/tsan-python -m pytest tests/ 2> tsan_report.txt
```

### 2.3 Data Files

```
data/
├── thread_safe_apis.json       # CPython APIs safe to call without the GIL
├── critical_section_apis.json  # APIs that use per-object critical sections (3.13+)
├── atomic_patterns.json        # _Py_atomic_* patterns and their C11 equivalents
├── lock_macros.json            # Known lock macro patterns (ENTER_ZLIB, etc.)
└── ft_migration_checklist.json # Structured migration steps with prerequisites
```

### 2.4 Classification System

Thread-safety findings use a different classification than cext-review-toolkit:

| Classification | Meaning | Example |
|----------------|---------|---------|
| **RACE** | Confirmed or highly likely data race | TSan-reported race on shared PyObject*, non-atomic flag read/write |
| **UNSAFE** | Operation that is unsafe without the GIL | Python API call in GIL-released section, borrowed ref without protection |
| **PROTECT** | Shared state that needs protection mechanism | Global PyObject*, module-level mutable state, static type mutation |
| **MIGRATE** | Code pattern that needs updating for free-threading | Single-phase init, static types, PyDict_GetItem (not thread-safe) |
| **SAFE** | Confirmed safe pattern | Read-only after init, properly locked, per-thread state |

Severity within each classification:
- **CRITICAL**: Crash, corruption, or security-relevant race
- **HIGH**: Likely data race under concurrent access
- **MEDIUM**: Potential race under specific access patterns
- **LOW**: Theoretical concern, defensive improvement


## 3. Agents

### 3.1 Agent Overview

8 agents total — 5 script-backed, 2 qualitative, 1 history-based.

#### Thread-Safety Analysis (script-backed)

| Agent | What It Finds | Script |
|-------|--------------|--------|
| **shared-state-auditor** | Global/static PyObject*, non-atomic shared flags, unprotected singletons, module state without m_size | `scan_shared_state.py` |
| **lock-discipline-checker** | Lock acquire/release pairing, missing release on error paths, nested lock risks, critical section candidates | `scan_lock_discipline.py` |
| **atomic-candidate-finder** | Shared bool/int flags that should be atomic, counter variables accessed across threads | `scan_atomic_candidates.py` |
| **tsan-report-analyzer** | Triage TSan data races, map to source, classify severity, suggest fixes | `parse_tsan_report.py` |
| **unsafe-api-detector** | Python API calls in GIL-released sections, borrowed refs without protection, thread-unsafe API usage | `scan_unsafe_apis.py` |

#### Qualitative (no script)

| Agent | What It Finds |
|-------|--------------|
| **stop-the-world-advisor** | Operations requiring StopTheWorld vs per-object locks vs critical_section |
| **migration-planner** | Phased migration plan from GIL-protected to free-threaded |

#### History (script-backed)

| Agent | What It Finds | Script |
|-------|--------------|--------|
| **ft-history-analyzer** | Free-threading related commits, incomplete migrations, past TSan fixes, reverted attempts | `analyze_ft_history.py` |

### 3.2 Agent Details

#### 3.2.1 shared-state-auditor

The foundational agent. Every thread-safety analysis starts with "what state is shared?"

**What it scans:**

1. **Global `PyObject*` variables.** `static PyObject *ExceptionClass;` — written during module init, read throughout. Safe if truly write-once-read-many, but needs verification. Tree-sitter finds all file-scope `static` declarations with `PyObject*` type.

2. **Non-atomic shared flags.** `static bool tracking_enabled;` — read and written from multiple threads. Must be `_Py_atomic_int` or `std::atomic<bool>`. Tree-sitter finds `static bool/int` at file scope, then checks for writes outside init functions.

3. **Module-level mutable state.** `PyModuleDef` with `m_size = -1` or `m_size = 0` — no per-module state struct. All "module state" lives in globals. Blocks safe free-threading.

4. **Static `PyTypeObject`.** Static types are shared across all interpreters and all threads. Mutation of any field (including internal fields like `tp_dict`) is a data race under free-threading.

5. **Singleton patterns.** `static FooObject *the_instance;` — global singleton with lazy init is a classic race condition.

6. **Thread-local vs truly global.** Distinguish between `static __thread` / `_Py_thread_local` (safe) and `static` (shared).

**Script:** `scan_shared_state.py`
- Uses Tree-sitter to find all `static` declarations at file scope
- Classifies each as: PyObject*, primitive type, struct, function pointer
- Checks for writes outside `PyInit_*` / `PyMODINIT_FUNC` functions
- Outputs: variable name, type, file:line, write locations, classification (PROTECT/SAFE/MIGRATE)

**Agent prompt focus:** For each candidate, the agent reads the surrounding code to determine:
- Is this truly write-once-read-many? (SAFE after init)
- Is this written from multiple threads? (RACE/PROTECT)
- Is this a static type that should become a heap type? (MIGRATE)
- Is this already protected by a lock? (SAFE if lock discipline is correct)

#### 3.2.2 lock-discipline-checker

Analyzes existing lock usage for correctness and completeness.

**What it scans:**

1. **Lock pairing.** For every `PyThread_acquire_lock` / `ENTER_ZLIB` / `pthread_mutex_lock` / `critical_section_begin`, verify there's a matching release on all paths (including error paths). This is the same pattern as cext-review-toolkit's GIL checker but generalized to any lock.

2. **Missing release on error paths.** The classic bug: lock acquired, error detected, `return NULL` without releasing. Tree-sitter finds all `return` statements between acquire and release.

3. **Nested lock risk.** Two different locks acquired in the same function — potential deadlock if another function acquires them in the opposite order.

4. **Critical section candidates.** Functions that access `self->member` after releasing the GIL — should use `Py_BEGIN_CRITICAL_SECTION(self)` (3.13+) instead of manual locking.

5. **Lock-free operations on shared data.** Reads/writes to shared fields without any lock held — data races under free-threading.

**Script:** `scan_lock_discipline.py`
- Uses Tree-sitter to find lock acquire/release calls (configurable macro list from `data/lock_macros.json`)
- Tracks lock state through each function's control flow
- Outputs: lock name, acquire line, all release lines, unreleased paths, nested locks

**Agent prompt focus:** The agent reads each finding and determines:
- Is the missing release a real bug or is cleanup handled by a destructor/RAII?
- Is the nested lock a real deadlock risk or are they always acquired in the same order?
- Would `Py_BEGIN_CRITICAL_SECTION` be appropriate here?

#### 3.2.3 atomic-candidate-finder

Finds shared variables that should use atomic operations.

**What it scans:**

1. **Shared `bool`/`int` flags.** `static bool s_tracking_enabled;` — if read by one thread and written by another, must be atomic. Even "benign" races (where tearing doesn't matter) are UB in C and trigger TSan.

2. **Reference-count-like counters.** `static int active_count;` incremented/decremented from multiple threads.

3. **Pointer-width values.** `static void *current_handler;` — pointer writes are atomic on most architectures but not guaranteed by C standard.

4. **Already-atomic variables.** `_Py_atomic_int` or `std::atomic<>` — verify they use the correct memory ordering.

**Script:** `scan_atomic_candidates.py`
- Uses Tree-sitter to find `static` primitive-type declarations at file scope
- Cross-references with write locations found by `scan_shared_state.py`
- Outputs: variable name, type, read/write locations, suggested atomic type

#### 3.2.4 tsan-report-analyzer

The highest-value agent when TSan reports are available. TSan reports are notoriously verbose — a single test run can produce thousands of lines of stack traces. This agent triages them into actionable findings.

**Input:** Parsed TSan report from `parse_tsan_report.py`

**Script:** `parse_tsan_report.py`
- Parses TSan text output into structured findings
- Groups races by shared memory location (same address = same race, different manifestations)
- Deduplicates (same source location pair = same finding, even if different threads)
- Maps stack frames to source files using debug info or heuristics
- Separates extension code races from CPython internal races (the user can't fix CPython races)
- Outputs: structured JSON with race pairs (read location, write location), involved threads, frequency, stack traces

**Agent prompt focus:** For each deduplicated race:
- Is this in extension code or CPython internals? (only extension code is actionable)
- Is this a benign race (flag check, performance counter) or dangerous (PyObject* mutation, container modification)?
- What's the fix? (atomic, mutex, critical_section, stop-the-world, restructure)
- Has this race been fixed before in this codebase? (cross-reference with ft-history-analyzer)

#### 3.2.5 unsafe-api-detector

Finds Python/C API calls that are unsafe under free-threading.

**What it scans:**

1. **Python API calls in GIL-released sections.** Code between `Py_BEGIN_ALLOW_THREADS` and `Py_END_ALLOW_THREADS` that calls any Python API. Under the GIL, this crashes immediately. Under free-threading, the GIL release macros are no-ops, so this "works" but the API call itself may not be thread-safe.

2. **Borrowed references without protection.** `PyDict_GetItem` returns a borrowed ref that, under GIL, is guaranteed stable until the next Python API call. Under free-threading, another thread could modify the dict at any time, invalidating the borrowed ref. Must use `PyDict_GetItemRef` (3.13+) or `PyDict_GetItemWithError` + immediate `Py_INCREF`.

3. **Thread-unsafe APIs.** APIs that use internal global state: `PyErr_Fetch`/`PyErr_Restore` (use `PyErr_GetRaisedException`), `PySys_GetObject` (uses borrowed ref from global dict), `PyImport_ImportModule` (may trigger code execution).

4. **Container mutation without protection.** `PyList_Append`, `PyDict_SetItem`, etc. on shared containers without a lock or critical section.

5. **`PyGILState_*` usage.** Under free-threading, `PyGILState_Ensure`/`PyGILState_Release` are no-ops that don't provide any synchronization. Code relying on them for thread safety needs real locks.

**Script:** `scan_unsafe_apis.py`
- Uses Tree-sitter to find API calls and their context (inside GIL-released section? inside lock?)
- Cross-references with `data/thread_safe_apis.json` for API classification
- Outputs: API call, location, context (GIL state, lock state), risk level

#### 3.2.6 stop-the-world-advisor (qualitative)

Some operations are inherently global — they need ALL threads to stop. This agent identifies them and recommends the appropriate synchronization mechanism.

**What it looks for:**

1. **GC-interacting operations.** Walking the GC object list, modifying `tp_traverse`, anything that touches `gc_refs`.

2. **Interpreter state traversal.** Walking thread states, frame stacks, import system state.

3. **Global registry modifications.** Codec registries, type registries, import hooks.

4. **Allocation hook installation.** `PyMem_SetAllocator`, tracemalloc-style hooks.

**Recommendation hierarchy:**
- `Py_BEGIN_CRITICAL_SECTION(obj)` — per-object lock, lowest overhead, for operations on a single object
- `PyMutex` — lightweight mutex for protecting shared data structures
- `PyInterpreterState_StopTheWorld` — nuclear option, for operations that truly need global quiescence
- Restructure to avoid sharing — best when possible (per-thread state, immutable data)

#### 3.2.7 migration-planner (qualitative)

The most complex agent. Reads the full extension source, consumes findings from all other agents, and produces a phased migration plan.

**Inputs:**
- shared-state-auditor findings (what state needs protection)
- lock-discipline-checker findings (what locking exists)
- unsafe-api-detector findings (what API calls need updating)
- ft-history-analyzer findings (what's been tried before)
- cext-review-toolkit module-state-checker findings (init style, type style)
- TSan findings if available

**Output:** A structured migration plan with phases:

**Phase 0: Prerequisites**
- Fix correctness bugs first (cext-review-toolkit findings)
- Ensure tests pass on standard (GIL) Python
- Set up TSan testing infrastructure

**Phase 1: Declare Intent**
- Add `Py_MOD_GIL_NOT_USED` (or `Py_mod_gil` slot)
- Verify extension compiles with `Py_GIL_DISABLED`
- Run tests on free-threaded Python (expect failures)

**Phase 2: Protect Shared State**
- Convert global `PyObject*` to module state (requires multi-phase init)
- Convert static types to heap types
- Add atomics for shared flags
- Add per-object locks or critical sections for mutable object state

**Phase 3: Update API Usage**
- Replace `PyDict_GetItem` → `PyDict_GetItemRef`
- Replace `PyErr_Fetch/Restore` → `PyErr_GetRaisedException/SetRaisedException`
- Replace borrowed refs with owned refs where needed
- Add `Py_BEGIN_CRITICAL_SECTION` around container mutations

**Phase 4: Verify**
- Run full test suite under TSan
- Triage TSan findings with tsan-report-analyzer
- Fix remaining races
- Iterate until TSan-clean

**Phase 5: Maintain**
- Add TSan CI job
- Document thread-safety invariants
- Review new code for thread safety

#### 3.2.8 ft-history-analyzer

A specialized version of cext-review-toolkit's git-history-analyzer, focused exclusively on free-threading related commits.

**Search terms:** `free-thread`, `nogil`, `GIL_DISABLED`, `Py_MOD_GIL`, `critical_section`, `_Py_atomic`, `std::atomic`, `thread-safe`, `thread safe`, `data race`, `race condition`, `TSan`, `TSAN`, `ThreadSanitizer`, `PyMutex`, `stop.the.world`, `StopTheWorld`, `Py_BEGIN_CRITICAL_SECTION`, `per.interpreter`, `subinterpreter`

**Extended parameters:**
- Default to `--days 730` (2 years) or `--max-commits 2000` — free-threading work spans CPython 3.12-3.14 development
- Focus on the extension's own repo AND optionally CPython's repo for context on API changes

**What it produces:**

1. **Migration timeline.** When did free-threading work start? How many commits? Is it ongoing or stalled?

2. **Fix patterns.** What kinds of races were found and fixed? What synchronization primitives were used?

3. **Incomplete migrations.** "Added `critical_section` to `Type_method_a` and `Type_method_b` but not `Type_method_c`." Same similar-bug-detection as the original history agent but focused on thread-safety patterns.

4. **Reverted attempts.** Commits that added free-threading support then were reverted — what went wrong?

5. **TSan fix commits.** Commits that reference TSan or data races — what patterns were found and fixed? Are there similar unfixed patterns?

**Script:** `analyze_ft_history.py`
- Reuses `analyze_history.py`'s git parsing infrastructure
- Adds free-threading-specific commit classification
- Adds incomplete-migration detection for lock/atomic/critical-section additions
- Outputs: timeline, fix patterns, incomplete migrations, similar unfixed patterns


## 4. Commands

### 4.1 `explore`

**Purpose:** Find thread-safety bugs and produce a report with findings and reproducers.

**What it runs:**
1. **Group A (foundation):** shared-state-auditor + ft-history-analyzer (in parallel)
2. **Group B (analysis):** lock-discipline-checker + atomic-candidate-finder + unsafe-api-detector (in parallel, uses Group A output for context)
3. **Group C (triage):** tsan-report-analyzer (if TSan report provided) + stop-the-world-advisor (in parallel, uses Groups A+B)
4. **Group D (synthesis):** Agent synthesizes all findings into a report

**Output:** A report with:
- Executive summary (how far from free-threading safe)
- RACE findings (confirmed or likely data races)
- UNSAFE findings (API usage that breaks under free-threading)
- PROTECT findings (shared state needing protection)
- MIGRATE findings (patterns needing structural changes)
- Reproducers where possible (concurrent test scripts)

**Aspects** (run subset of agents):
- `explore . shared-state` — just the shared-state-auditor
- `explore . locks` — just the lock-discipline-checker
- `explore . tsan path/to/report.txt` — just the TSan triage
- `explore . all` — all agents

### 4.2 `plan`

**Purpose:** Produce a phased migration plan for adopting free-threading.

**What it runs:**
1. All agents from `explore` (to understand current state)
2. migration-planner agent (produces the plan)

**Output:** A structured, phased migration plan (see Section 3.2.7).

**Inputs:**
- Extension source directory
- Optional: TSan report file
- Optional: cext-review-toolkit report (for correctness findings)
- Optional: target Python version (defaults to latest free-threading release)

### 4.3 `assess`

**Purpose:** Quick scored dashboard — how close is this extension to free-threading safe?

**What it runs:** All agents in summary mode (faster, less detailed).

**Output:** A scorecard:
```
Free-Threading Readiness: 35/100

[ ] Multi-phase init (single-phase, global state)
[ ] Heap types (3 static types)
[x] No GIL-released Python API calls
[ ] Shared state protected (5 unprotected globals)
[ ] Thread-safe API usage (12 PyDict_GetItem calls)
[x] No lock discipline issues
[ ] Atomic shared flags (2 non-atomic bools)
[ ] TSan clean (no TSan report provided)
```


## 5. Integration with cext-review-toolkit

### 5.1 Shared Code

ft-review-toolkit imports directly from cext-review-toolkit:
- `tree_sitter_utils.py` — all parsing
- `scan_common.py` — project root detection, file discovery, arg parsing
- `discover_extension.py` — extension layout detection

It does NOT import cext-review-toolkit's analysis scripts (scan_refcounts.py, etc.) — those are correctness-focused, not thread-safety-focused.

### 5.2 Complementary Workflow

The recommended workflow:
1. Run `cext-review-toolkit:explore` — find and fix correctness bugs first
2. Run `ft-review-toolkit:assess` — see how far from free-threading safe
3. Run `ft-review-toolkit:plan` — get a migration plan
4. Implement the plan
5. Run `ft-review-toolkit:explore` with TSan report — verify and iterate

### 5.3 Cross-Agent Data Flow

ft-review-toolkit agents can optionally consume cext-review-toolkit findings:
- module-state-checker findings → migration-planner (knows about init style, type style)
- gil-discipline-checker findings → lock-discipline-checker (knows about existing GIL patterns)
- type-slot-checker findings → shared-state-auditor (knows about static vs heap types)

This is optional — ft-review-toolkit works standalone, but produces richer results with cext-review-toolkit context.


## 6. Project Structure

```
ft-review-toolkit/
├── ft-review-toolkit-design.md     # This file
├── CLAUDE.md                       # Development guide
├── README.md                       # User-facing documentation
├── CHANGELOG.md                    # Keep a Changelog format
├── LICENSE                         # MIT
├── plugins/ft-review-toolkit/      # The actual plugin
│   ├── .claude-plugin/plugin.json  # Plugin metadata
│   ├── agents/                     # 8 agent prompt definitions
│   │   ├── shared-state-auditor.md
│   │   ├── lock-discipline-checker.md
│   │   ├── atomic-candidate-finder.md
│   │   ├── tsan-report-analyzer.md
│   │   ├── unsafe-api-detector.md
│   │   ├── stop-the-world-advisor.md
│   │   ├── migration-planner.md
│   │   └── ft-history-analyzer.md
│   ├── commands/                   # 3 command definitions
│   │   ├── explore.md
│   │   ├── plan.md
│   │   └── assess.md
│   ├── scripts/                    # 6 Python scripts
│   │   ├── scan_shared_state.py
│   │   ├── scan_lock_discipline.py
│   │   ├── scan_atomic_candidates.py
│   │   ├── scan_unsafe_apis.py
│   │   ├── parse_tsan_report.py
│   │   └── analyze_ft_history.py
│   └── data/                       # 5 JSON data files
│       ├── thread_safe_apis.json
│       ├── critical_section_apis.json
│       ├── atomic_patterns.json
│       ├── lock_macros.json
│       └── ft_migration_checklist.json
└── tests/                          # unittest test suite
    ├── helpers.py                  # Shared test utilities (import from cext-review-toolkit)
    ├── test_scan_shared_state.py
    ├── test_scan_lock_discipline.py
    ├── test_scan_atomic_candidates.py
    ├── test_scan_unsafe_apis.py
    ├── test_parse_tsan_report.py
    └── test_analyze_ft_history.py
```


## 7. Implementation Sequence

### Phase 1: Foundation (scripts + basic agents)
1. `scan_shared_state.py` + `shared-state-auditor` — the most fundamental analysis
2. `scan_unsafe_apis.py` + `unsafe-api-detector` — find thread-unsafe API usage
3. `analyze_ft_history.py` + `ft-history-analyzer` — understand past efforts
4. `assess` command — quick dashboard from these 3 agents
5. Data files: `thread_safe_apis.json`, `lock_macros.json`

### Phase 2: Lock and atomic analysis
6. `scan_lock_discipline.py` + `lock-discipline-checker`
7. `scan_atomic_candidates.py` + `atomic-candidate-finder`
8. `explore` command — full analysis with all script-backed agents
9. Data files: `atomic_patterns.json`, `critical_section_apis.json`

### Phase 3: TSan integration and planning
10. `parse_tsan_report.py` + `tsan-report-analyzer`
11. `stop-the-world-advisor` (qualitative)
12. `migration-planner` (qualitative, consumes all other agents)
13. `plan` command
14. Data file: `ft_migration_checklist.json`

### Phase 4: Testing on real extensions
15. Test on guppy3 (deeply coupled to CPython internals — hardest case)
16. Test on python-isal/zlib-ng (lock-based, moderate difficulty)
17. Test on memray (sophisticated GIL handling, good test case)
18. Test on apsw (high-quality codebase, when Roger is ready)
19. Iterate agent prompts based on real-world findings


## 8. Key Design Decisions

### 8.1 Why a Separate Plugin?

Free-threading analysis is a different concern from correctness analysis. The agents think differently (thread-safety vs single-threaded correctness), the classification system is different (RACE/UNSAFE/PROTECT/MIGRATE vs FIX/CONSIDER/POLICY), and the workflow is different (iterative with TSan vs one-shot report). Keeping them separate prevents the cext-review-toolkit agents from getting confused by thread-safety concerns they're not designed to evaluate, and vice versa.

### 8.2 Why TSan Triage Instead of TSan Replacement?

TSan is a runtime tool that instruments actual execution. We can't replicate what it does from static analysis alone — data races depend on actual thread interleavings, timing, and workload. What we CAN do is make TSan output actionable: deduplicate, map to source, classify severity, suggest fixes, and find similar unfixed patterns. This is where human time is wasted — not in running TSan, but in understanding its output.

### 8.3 Why Extended History Range?

Free-threading work in the Python ecosystem started in earnest with PEP 703 acceptance (2023) and CPython 3.13t (2024). A 90-day window (cext-review-toolkit's default) would miss the full arc of a project's migration. 2 years captures the entire PEP 703 era.

### 8.4 Why Per-Object Critical Sections Over Global Locks?

The migration-planner recommends `Py_BEGIN_CRITICAL_SECTION` as the default protection mechanism because:
- It's per-object, so no global contention
- It's a CPython API, so it's maintained by the CPython team
- It automatically handles the case where the GIL is present (no-op)
- It's the pattern CPython itself uses for its built-in types

Global mutexes (`PyMutex`) are recommended only for truly global state. `StopTheWorld` is the last resort for operations that need global quiescence.
