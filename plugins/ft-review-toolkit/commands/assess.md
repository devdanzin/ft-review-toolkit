---
description: "Quick free-threading readiness scorecard. Use when the user asks for a quick overview, readiness score, or assessment of how close a C extension is to being free-threading safe."
argument-hint: "[scope]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Agent"]
---

# Free-Threading Readiness Assessment

Run the Phase 1 agents in summary mode to produce a quick readiness scorecard.

**Scope:** "$ARGUMENTS" (default: entire project)

**Plugin root:** `<plugin_root>` refers to the `plugins/ft-review-toolkit/` directory. Resolve it relative to this file's location.

## Workflow

1. Run the three Phase 1 scanners in parallel:
   ```
   python <plugin_root>/scripts/scan_shared_state.py [scope]
   python <plugin_root>/scripts/scan_unsafe_apis.py [scope]
   python <plugin_root>/scripts/analyze_ft_history.py [scope]
   ```

2. Collect and deduplicate findings across all scanners.

3. Check for key migration indicators:
   - **Multi-phase init**: grep for `PyModuleDef_Init` (multi-phase) vs `PyModule_Create` (single-phase)
   - **Heap types**: grep for `PyType_FromSpec` (heap) vs `PyType_Ready` + static `PyTypeObject` (static)
   - **Py_MOD_GIL_NOT_USED**: grep for this declaration
   - **Critical sections**: grep for `Py_BEGIN_CRITICAL_SECTION`
   - **Atomics**: grep for `_Py_atomic` or `std::atomic`

4. Synthesize into a readiness scorecard:

```markdown
# Free-Threading Readiness Assessment

## Extension: [name] ([N] C files, [N] lines)

### Readiness Score: [X]/100

### Checklist

| Item | Status | Impact | Details |
|------|--------|--------|---------|
| Multi-phase init | PASS/FAIL | Required | [single-phase / multi-phase] |
| Heap types | PASS/FAIL | Required | [N static types need conversion] |
| No unprotected globals | PASS/FAIL | Critical | [N unprotected global PyObject*] |
| Thread-safe API usage | PASS/FAIL | High | [N borrowed ref issues, N container mutations] |
| No GIL-released API calls | PASS/FAIL | Critical | [N unsafe calls] |
| Lock discipline | N/A | Medium | [assessed in explore, not assess] |
| Atomic shared flags | PASS/FAIL | High | [N non-atomic shared flags] |
| PyGILState not relied on | PASS/WARN | Medium | [N PyGILState usages] |
| Py_MOD_GIL_NOT_USED | PASS/FAIL | Required | [declared / not declared] |
| TSan clean | N/A | Ideal | [no TSan report provided] |

### Migration Status

[From ft-history-analyzer: not_started / active / paused / stalled]
[N] free-threading related commits found.

### Top 3 Priorities

1. [Most impactful improvement with classification and severity]
2. [Next]
3. [Next]

### Next Steps

For detailed analysis:
  /ft-review-toolkit:explore [scope]

For a migration plan:
  /ft-review-toolkit:plan [scope]
```

## Scoring Rubric

The readiness score (0-100) is computed as:

| Criterion | Points | Condition |
|-----------|--------|-----------|
| Multi-phase init | 15 | PyModuleDef_Init used |
| Heap types | 15 | No static PyTypeObject |
| No unprotected globals | 20 | No PROTECT/RACE findings for globals |
| Thread-safe APIs | 15 | No borrowed ref or container mutation findings |
| No GIL-released API calls | 15 | No unsafe_api_without_gil findings |
| Atomic shared flags | 10 | No non-atomic shared primitives |
| Py_MOD_GIL_NOT_USED declared | 10 | Grep finds declaration |

Deductions:
- Each CRITICAL finding: -5
- Each HIGH finding: -3
- Each MEDIUM finding: -1

Floor at 0, cap at 100.

## Usage

```
/ft-review-toolkit:assess                # Full project
/ft-review-toolkit:assess src/myext/     # Specific directory
```
