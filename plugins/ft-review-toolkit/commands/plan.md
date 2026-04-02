---
description: "Produce a phased migration plan for adopting free-threading in a C extension. Runs all analysis agents, then the migration-planner to create an actionable plan. Use when the user asks for a migration plan, how to add free-threading support, or how to get started with free-threading."
argument-hint: "[scope]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Agent"]
---

# Free-Threading Migration Plan

Analyze the extension and produce a phased migration plan.

**Scope:** "$ARGUMENTS" (default: entire project)

**Plugin root:** `<plugin_root>` refers to the `plugins/ft-review-toolkit/` directory. Resolve it relative to this file's location.

## Workflow

1. **Run all analysis agents** to understand the current state:

   Run these scanners (in parallel where possible):
   ```
   python <plugin_root>/scripts/scan_shared_state.py [scope]
   python <plugin_root>/scripts/scan_unsafe_apis.py [scope]
   python <plugin_root>/scripts/scan_lock_discipline.py [scope]
   python <plugin_root>/scripts/scan_atomic_candidates.py [scope]
   python <plugin_root>/scripts/analyze_ft_history.py [scope]
   ```

   If a TSan report is available (second argument), also run:
   ```
   python <plugin_root>/scripts/parse_tsan_report.py [tsan_report_path]
   ```

2. **Gather additional context** via grep:
   - Init style: `PyModule_Create` (single-phase) vs `PyModuleDef_Init` (multi-phase)
   - Type style: `static PyTypeObject` vs `PyType_FromSpec`
   - GIL declaration: `Py_MOD_GIL_NOT_USED` or `Py_mod_gil`
   - Target Python versions: check `setup.py`, `pyproject.toml`, `setup.cfg`

3. **Run the migration-planner agent** with all gathered findings as context.

4. **Produce the migration plan** following the template in the migration-planner agent, tailored to this specific extension.

## Output

A phased migration plan with:
- Current state assessment
- Effort estimate per phase
- Detailed checklists for each phase (populated from actual findings)
- Specific file:line references for each action item
- Code examples for the most common transformations
- TSan verification instructions

## Integration with cext-review-toolkit

If cext-review-toolkit has already been run on this extension, incorporate its findings:
- Module state findings → inform Phase 2a (module state migration)
- Type slot findings → inform Phase 2b (static → heap type conversion)
- GIL discipline findings → inform existing lock patterns

## Usage

```
/ft-review-toolkit:plan                     # Full project
/ft-review-toolkit:plan src/myext/          # Specific directory
/ft-review-toolkit:plan src/ tsan.txt       # With TSan report
```
