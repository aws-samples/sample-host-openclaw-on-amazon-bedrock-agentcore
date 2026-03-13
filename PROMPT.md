# Ralph Loop — Red Team Test Suite Overhaul

You are running a Ralph BUILDING loop for the **openclaw-on-agentcore** project.

## Required reading (every iteration)
- `CLAUDE.md` — project rules
- `AGENTS.md` — build commands
- `docs/redteam-audit.md` — full audit with EXACT prompts, assertions, and fixes for every task
- `IMPLEMENTATION_PLAN.md` — current task list

## Rules
- NEVER push to remote git. Local commits only.
- One task per iteration. Pick the top incomplete task.
- After each task: update IMPLEMENTATION_PLAN.md, commit.
- Run `cd redteam && npx promptfoo@latest eval --config evalconfig.yaml --dry-run 2>&1` after TASK-9 to verify.

## Key reference
The audit doc (`docs/redteam-audit.md`) contains the EXACT YAML for every new test case and every assertion fix. Do not invent new content — copy from the audit doc precisely. Sections to use:
- Section 3.1 → TASK-3, TASK-4 (Critical tests)
- Section 3.2 → TASK-5, TASK-6 (High priority tests)
- Section 3.3 → TASK-7, TASK-8 (Medium priority tests)
- Section 4.1 → TASK-1 (assertion fixes)
- Section 5 → TASK-2 (Bedrock grader config)

## Loop
1. Read IMPLEMENTATION_PLAN.md → pick top incomplete task
2. Read the relevant section of `docs/redteam-audit.md` for exact content
3. Implement
4. Update IMPLEMENTATION_PLAN.md → mark done
5. Commit: `git add -A && git commit -m "<type>(redteam): <description>"`
6. Stop

## Completion
When ALL tasks done, replace `IN_PROGRESS` with `STATUS: COMPLETE` on its own line and stop.
