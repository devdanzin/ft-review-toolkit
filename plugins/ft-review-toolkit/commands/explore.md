---
description: "Full free-threading thread-safety analysis. Finds data races, unprotected shared state, unsafe API usage, lock discipline issues, and atomic candidates. Use when the user wants a comprehensive analysis or to find thread-safety bugs."
argument-hint: "[scope] [aspect]"
allowed-tools: ["Bash", "Glob", "Grep", "Read", "Agent"]
---

# Free-Threading Thread-Safety Analysis

Run all agents in phased groups to produce a comprehensive thread-safety report.

**Scope:** First argument of "$ARGUMENTS" (default: entire project)
**Aspect:** Optional second argument to run a subset of agents.

**Plugin root:** `<plugin_root>` refers to the `plugins/ft-review-toolkit/` directory. Resolve it relative to this file's location.

## Aspects

| Aspect | Agents Run |
|--------|-----------|
| `all` (default) | All agents in all groups |
| `shared-state` | shared-state-auditor only |
| `locks` | lock-discipline-checker only |
| `atomics` | atomic-candidate-finder only |
| `unsafe-apis` | unsafe-api-detector only |
| `history` | ft-history-analyzer only |
| `tsan [report.txt]` | tsan-report-analyzer with provided report |
| `stw-safety` | stw-safety-checker only (for extensions using _PyEval_StopTheWorld) |
| `stress-test` | tsan-stress-generator only (generates script, does NOT run it) |

## Full Workflow (aspect = all)

### Group A: Foundation (run in parallel)

These establish the baseline — what state is shared, what's the project's ft history.

1. **shared-state-auditor**: Run `scan_shared_state.py`, triage findings
2. **ft-history-analyzer**: Run `analyze_ft_history.py`, identify migration status

### Group B: Analysis (run in parallel, uses Group A context)

These analyze specific aspects using Group A's findings for context.

3. **lock-discipline-checker**: Run `scan_lock_discipline.py`, verify lock pairing
4. **atomic-candidate-finder**: Run `scan_atomic_candidates.py`, find atomic candidates
5. **unsafe-api-detector**: Run `scan_unsafe_apis.py`, find unsafe API usage

### Group C: Qualitative (uses Groups A+B context)

6. **tsan-report-analyzer**: Triage TSan report — **only runs if a TSan report path is provided** as an additional argument (e.g., `explore . all tsan_report.txt`)
7. **stop-the-world-advisor**: Identify operations needing StopTheWorld
8. **stw-safety-checker**: Run `scan_stw_safety.py` — **only runs if the extension uses `_PyEval_StopTheWorld`**. Builds call graphs and detects unsafe operations during STW.

Note: **tsan-stress-generator is NOT part of the explore pipeline.** It produces a script that must be executed externally (by the user or labeille) before its output is useful. Use `explore . stress-test` as a standalone step. The intended workflow is:
1. `explore . stress-test` → generates `tsan_stress_<name>.py`
2. User/labeille runs script under TSan Python → produces `tsan_report.txt`
3. `explore . tsan tsan_report.txt` or `explore . all tsan_report.txt` → triages the report

### Group D: Synthesis

Combine all findings into a structured report.

## Output Format

```markdown
# Free-Threading Analysis Report

## Extension: [name] ([N] C files, [N] functions)

## Migration Status
[From ft-history-analyzer: timeline, commit count, status]

## Executive Summary
- **Readiness**: [Far / Moderate / Close / Ready]
- **RACE findings**: [N] — confirmed or likely data races
- **UNSAFE findings**: [N] — operations unsafe without GIL
- **PROTECT findings**: [N] — shared state needing protection
- **MIGRATE findings**: [N] — structural changes needed

## Findings by Priority

**Use global non-restarting numbering**: number ALL findings sequentially across
all sections. RACE findings first (1-N), then UNSAFE (N+1-M), then PROTECT
(M+1-P), then MIGRATE (P+1-Q). Use these same numbers in the Recommendations
section. This makes it easy to reference "Finding 12" in issue trackers and emails.

### RACE Findings (fix immediately) — N

| # | Finding | File:Line | Severity | Agents |
|---|---------|-----------|----------|--------|
| 1 | [Description] | [file:line] | CRITICAL/HIGH | [which agents found it] |

### UNSAFE Findings (fix before declaring free-threading support) — M

| # | Finding | File:Line | Severity |
|---|---------|-----------|----------|
| N+1 | [Description] | [file:line] | HIGH/MEDIUM |

### PROTECT Findings (add synchronization) — P

| # | Finding | File:Line | Severity |
|---|---------|-----------|----------|
| M+1 | [Description] | [file:line] | HIGH/MEDIUM |

### MIGRATE Findings (structural changes) — Q

| # | Finding | File:Line | Severity |
|---|---------|-----------|----------|
| P+1 | [Description] | [file:line] | MEDIUM/LOW |

## SAFE Patterns (confirmed safe)

- [Pattern 1]: Thread-local storage used correctly
- [Pattern 2]: Per-module state with proper init

## Recommendations

Reference findings by their global number:

### Immediate (RACE + UNSAFE items)
1. [Fix Finding N — description]
2. [Fix Finding M — description]

### Short-term (PROTECT items)
3. [Finding P — description]

### Longer-term (MIGRATE items)
4. [Finding Q — description]

For a phased migration plan:
  /ft-review-toolkit:plan [scope]
```

## Deduplication Rules

When the same issue is flagged by multiple agents, count it once under the
highest-priority classification. Note which agents found it in the "Agents"
column to show cross-validation.

**Cross-agent dedup:**
- `non_atomic_shared_flag` (shared-state) + `non_atomic_shared_bool` (atomics) → report once under atomics
- `unprotected_global_pyobject` (shared-state) + `container_mutation_unprotected` (unsafe-apis) → combine into one finding
- `critical_section_candidate` (locks) + shared state findings → enhance the lock finding with shared state context

**Intra-agent dedup:**
- Same bug pattern repeated in template-generated code (e.g., 4 dtype-specialized variants of the same function) → report once with `duplicate_count: 4` and list all locations
- Same lazy-init pattern across N functions → report as one finding ("N lazy-init static string caches") with a table of locations, not N separate findings

**TSan dedup:**
- Same `(file:line, file:line)` race pair appearing multiple times → count as one unique race
- Same root cause manifesting in different functions → group under one finding with a frequency count

**Nearby comments:**
When flagging a finding, check if nearby source comments (within ±5 lines)
contain safety annotations like "intentional", "safe because", "by design",
"not a bug", "deliberately". If found, lower the confidence to "low" and
note the annotation in the finding. The scanners' `extract_nearby_comments()`
and `has_safety_annotation()` functions support this.

## Usage

```
/ft-review-toolkit:explore                  # Full project, all agents
/ft-review-toolkit:explore src/             # Specific directory
/ft-review-toolkit:explore . shared-state   # Just shared state
/ft-review-toolkit:explore . locks          # Just lock discipline
/ft-review-toolkit:explore . atomics        # Just atomic candidates
/ft-review-toolkit:explore . unsafe-apis    # Just unsafe APIs
/ft-review-toolkit:explore . history        # Just git history
/ft-review-toolkit:explore . tsan report.txt  # Triage a TSan report
/ft-review-toolkit:explore . all report.txt   # Full analysis + TSan triage
/ft-review-toolkit:explore . stw-safety    # STW call-graph safety analysis
/ft-review-toolkit:explore . stress-test    # Generate TSan stress test script (standalone)
```
