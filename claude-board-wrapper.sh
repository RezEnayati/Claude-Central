#!/bin/bash
BOARD_SERVER="${BOARD_SERVER:-http://localhost:8080}"

if [ -z "$REAL_CLAUDE" ]; then
  # whence -p (zsh) skips functions and finds the actual binary
  REAL_CLAUDE="$(whence -p claude 2>/dev/null || command -v claude 2>/dev/null)"
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
  task_id=$(uuidgen | tr '[:upper:]' '[:lower:]')
  local machine
  machine="$(hostname -s)"

  curl -sf -X POST "$BOARD_SERVER/task" \
    -H "Content-Type: application/json" \
    -d "{\"id\":\"$task_id\",\"name\":\"$task_name\",\"shell_pid\":$$,\"cwd\":\"$(pwd)\",\"hostname\":\"$machine\"}" > /dev/null 2>&1

  trap "curl -sf -X PATCH '$BOARD_SERVER/task/$task_id' -H 'Content-Type: application/json' -d '{\"status\":\"DONE\",\"exit_code\":130}' > /dev/null 2>&1; trap - INT; return 130" INT

  # Mark as RUNNING before launching claude
  curl -sf -X PATCH "$BOARD_SERVER/task/$task_id" \
    -H "Content-Type: application/json" \
    -d '{"status":"RUNNING"}' > /dev/null 2>&1

  "$REAL_CLAUDE" "$@"
  local exit_code=$?

  trap - INT

  local status="DONE"
  [ $exit_code -ne 0 ] && status="FAILED"
  curl -sf -X PATCH "$BOARD_SERVER/task/$task_id" \
    -H "Content-Type: application/json" \
    -d "{\"status\":\"$status\",\"exit_code\":$exit_code}" > /dev/null 2>&1

  return $exit_code
}
