#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage: send-to-tmux-agent.sh [--no-enter] <tmux-target> <text>

Sends literal text to a tmux pane, then sends Enter separately by default.
USAGE
}

send_enter=1
if [[ "${1:-}" == "--no-enter" ]]; then
  send_enter=0
  shift
fi

if [[ $# -lt 2 ]]; then
  usage
  exit 2
fi

target="$1"
shift
text="$*"

if ! tmux display-message -p -t "$target" '#{session_name}:#{window_index}.#{pane_index}' >/dev/null 2>&1; then
  echo "tmux target not found: $target" >&2
  exit 1
fi

tmux send-keys -t "$target" "$text"

if [[ "$send_enter" == "1" ]]; then
  sleep 1
  tmux send-keys -t "$target" Enter
fi
