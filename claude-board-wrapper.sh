#!/bin/bash
# ── Claude Board Wrapper ─────────────────────────────────────
# source ~/.claude-board/claude-board-wrapper.sh
# ──────────────────────────────────────────────────────────────

BOARD_SERVER="${BOARD_SERVER:-http://localhost:8080}"

if [ -z "$REAL_CLAUDE" ]; then
  REAL_CLAUDE="$(command -v claude 2>/dev/null)"
fi

claude() {
  local folder_name
  folder_name="$(basename "$(pwd)")"
  local branch
  branch="$(git symbolic-ref --short HEAD 2>/dev/null)"
  local task_name
  if [ -n "$branch" ]; then
    task_name="${folder_name} (${branch})"
  else
    task_name="${folder_name}"
  fi

  local task_id
  if command -v uuidgen &>/dev/null; then
    task_id=$(uuidgen | tr '[:upper:]' '[:lower:]')
  else
    task_id="task-$$-$(date +%s)"
  fi

  # Register task with shell PID for server-side CPU monitoring
  local task_cwd
  task_cwd="$(pwd)"
  curl -sf -X POST "$BOARD_SERVER/task" \
    -H "Content-Type: application/json" \
    -d "{\"id\": \"$task_id\", \"name\": \"$task_name\", \"shell_pid\": $$, \"cwd\": \"$task_cwd\"}" > /dev/null 2>&1

  # Trap ctrl+c so we still report done
  trap "curl -sf -X PATCH '$BOARD_SERVER/task/$task_id' -H 'Content-Type: application/json' -d '{\"status\":\"DONE\",\"exit_code\":130}' > /dev/null 2>&1; trap - INT; return 130" INT

  # Run claude
  "$REAL_CLAUDE" "$@"
  local exit_code=$?

  # Clean trap
  trap - INT

  # Report done
  local status="DONE"
  [ $exit_code -ne 0 ] && status="FAILED"
  curl -sf -X PATCH "$BOARD_SERVER/task/$task_id" \
    -H "Content-Type: application/json" \
    -d "{\"status\": \"$status\", \"exit_code\": $exit_code}" > /dev/null 2>&1

  return $exit_code
}
