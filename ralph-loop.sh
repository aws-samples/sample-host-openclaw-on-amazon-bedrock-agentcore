#!/bin/bash
# Ralph Loop runner for openclaw-on-agentcore
# Runs Claude Code iteratively until STATUS: COMPLETE

PROJECT="/home/ec2-user/projects/openclaw-on-agentcore"
MAX_ITERATIONS=15
ITERATION=0

cd "$PROJECT"

echo "🔴 Ralph Loop starting — max $MAX_ITERATIONS iterations"
echo "📋 Plan: $PROJECT/IMPLEMENTATION_PLAN.md"
echo ""

while [ $ITERATION -lt $MAX_ITERATIONS ]; do
  ITERATION=$((ITERATION + 1))
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "🔁 ITERATION $ITERATION / $MAX_ITERATIONS — $(date -u '+%Y-%m-%d %H:%M UTC')"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

  # Check completion before running
  if grep -qE "^STATUS: COMPLETE$" IMPLEMENTATION_PLAN.md 2>/dev/null; then
    echo "✅ STATUS: COMPLETE detected — loop finished!"
    openclaw system event --text "✅ Ralph Loop COMPLETE: Bedrock Guardrails + redteam/ fully implemented after $ITERATION iterations" --mode now
    exit 0
  fi

  echo "🤖 Running Claude Code..."
  claude --permission-mode bypassPermissions --print "$(cat PROMPT.md)"
  EXIT_CODE=$?
  echo ""
  echo "Claude Code exited with code: $EXIT_CODE"

  # Check completion after run
  if grep -qE "^STATUS: COMPLETE$" IMPLEMENTATION_PLAN.md 2>/dev/null; then
    echo "✅ STATUS: COMPLETE — all tasks done!"
    openclaw system event --text "✅ Ralph Loop COMPLETE: Bedrock Guardrails + redteam/ fully implemented after $ITERATION iterations" --mode now
    exit 0
  fi

  echo "⏳ Iteration $ITERATION done. Restarting..."
  sleep 3
done

echo "⚠️  Max iterations ($MAX_ITERATIONS) reached without STATUS: COMPLETE"
openclaw system event --text "⚠️ Ralph Loop hit max iterations ($MAX_ITERATIONS) — check IMPLEMENTATION_PLAN.md for remaining tasks" --mode now
