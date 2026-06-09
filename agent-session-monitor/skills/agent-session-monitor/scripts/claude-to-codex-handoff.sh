#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage: claude-to-codex-handoff.sh [--dry-run] --target <tmux-target> --prompt-file <path>

Exits a throttled Claude Code pane, then starts Codex in the same pane using:
  codex --sandbox danger-full-access --ask-for-approval on-request

This is an explicit handoff workflow, not a retry loop. If "exit" does not
return the pane to a shell, the script stops and does not try alternate exits.
USAGE
}

dry_run=0
target=""
prompt_file=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      dry_run=1
      shift
      ;;
    --target)
      target="${2:-}"
      shift 2
      ;;
    --prompt-file)
      prompt_file="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [[ -z "$target" || -z "$prompt_file" ]]; then
  usage
  exit 2
fi

if [[ ! -f "$prompt_file" ]]; then
  echo "prompt file not found: $prompt_file" >&2
  exit 1
fi

if ! tmux display-message -p -t "$target" '#{session_name}:#{window_index}.#{pane_index}' >/dev/null 2>&1; then
  echo "tmux target not found: $target" >&2
  exit 1
fi

cwd="$(tmux display-message -p -t "$target" '#{pane_current_path}')"
before_cmd="$(tmux display-message -p -t "$target" '#{pane_current_command}')"
safe_target="$(printf '%s' "$target" | tr -c '[:alnum:]_.-' '-')"
runner="/tmp/agent-session-monitor-codex-${safe_target}-$$.sh"

if [[ "$dry_run" == "1" ]]; then
  echo "target=$target"
  echo "cwd=$cwd"
  echo "before_cmd=$before_cmd"
  echo "runner=$runner"
  echo "would send: exit"
  echo "would send: bash $runner"
  exit 0
fi

cat > "$runner" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec codex --sandbox danger-full-access --ask-for-approval on-request --cd "$cwd" "\$(cat "$prompt_file")"
EOF
chmod +x "$runner"

tmux send-keys -t "$target" "exit"
sleep 1
tmux send-keys -t "$target" Enter

current_cmd=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
  sleep 1
  current_cmd="$(tmux display-message -p -t "$target" '#{pane_current_command}' 2>/dev/null || true)"
  case "$current_cmd" in
    fish|bash|zsh|sh)
      tmux send-keys -t "$target" "bash $runner"
      sleep 1
      tmux send-keys -t "$target" Enter
      exit 0
      ;;
  esac
done

echo "exit did not return $target to a shell; current command: ${current_cmd:-unknown}" >&2
echo "runner left in place: $runner" >&2
exit 1
