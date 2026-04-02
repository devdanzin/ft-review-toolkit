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
| `stress-test` | tsan-stress-generator only |

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

### Group C: Qualitative (if available — Phase 3 agents)

6. **tsan-report-analyzer**: If TSan report provided, triage it
7. **stop-the-world-advisor**: Identify operations needing StopTheWorld
8. **tsan-stress-generator**: Generate concurrent stress test script for TSan

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

## RACE Findings (fix immediately)

### 1. [Title]
- **File**: `path/to/file.c:123`
- **Severity**: CRITICAL
- **Description**: ...
- **Fix**: ...

## UNSAFE Findings (fix before declaring free-threading support)

### 1. [Title]
...

## PROTECT Findings (add synchronization)

### 1. [Title]
...

## MIGRATE Findings (structural changes)

### 1. [Title]
...

## SAFE Patterns (confirmed safe)

- [Pattern 1]: Thread-local storage used correctly
- [Pattern 2]: Per-module state with proper init

## Recommendations

1. [Highest priority action]
2. [Next]
3. [Next]

For a phased migration plan:
  /ft-review-toolkit:plan [scope]
```

## Deduplication Rules

When the same issue is flagged by multiple agents, count it once:
- `non_atomic_shared_flag` (shared-state) + `non_atomic_shared_bool` (atomics) → report once under atomics
- `unprotected_global_pyobject` (shared-state) + `container_mutation_unprotected` (unsafe-apis) → combine into one finding
- `critical_section_candidate` (locks) + shared state findings → enhance the lock finding with shared state context

## Usage

```
/ft-review-toolkit:explore                  # Full project, all agents
/ft-review-toolkit:explore src/             # Specific directory
/ft-review-toolkit:explore . shared-state   # Just shared state
/ft-review-toolkit:explore . locks          # Just lock discipline
/ft-review-toolkit:explore . atomics        # Just atomic candidates
/ft-review-toolkit:explore . unsafe-apis    # Just unsafe APIs
/ft-review-toolkit:explore . history        # Just git history
/ft-review-toolkit:explore . stress-test    # Generate TSan stress test script
```
