#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage: watch-tmux-agents.sh [--once] [--include-idle] [--interval SECONDS] [--lines N] <tmux-target>...

Monitors tmux panes running coding agents and emits state transitions:
  TARGET | AGENT | STATE | SUMMARY

Targets are tmux targets such as session:0.0. Session names also work when tmux
can resolve them unambiguously.
USAGE
}

once=0
include_idle=0
interval=20
lines=80
targets=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --once)
      once=1
      shift
      ;;
    --include-idle)
      include_idle=1
      shift
      ;;
    --interval)
      interval="${2:-}"
      shift 2
      ;;
    --lines)
      lines="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      while [[ $# -gt 0 ]]; do
        targets+=("$1")
        shift
      done
      ;;
    -*)
      echo "unknown option: $1" >&2
      usage
      exit 2
      ;;
    *)
      targets+=("$1")
      shift
      ;;
  esac
done

if [[ ${#targets[@]} -eq 0 && -n "${AGENT_WATCH_TARGETS:-}" ]]; then
  # shellcheck disable=SC2206
  targets=($AGENT_WATCH_TARGETS)
fi

if [[ ${#targets[@]} -eq 0 ]]; then
  usage
  exit 2
fi

if ! [[ "$interval" =~ ^[0-9]+$ ]] || [[ "$interval" -lt 1 ]]; then
  echo "--interval must be a positive integer" >&2
  exit 2
fi

if ! [[ "$lines" =~ ^[0-9]+$ ]] || [[ "$lines" -lt 1 ]]; then
  echo "--lines must be a positive integer" >&2
  exit 2
fi

state_dir="${TMPDIR:-/tmp}/agent-session-monitor-watch.$$"
mkdir -p "$state_dir"
trap 'rm -rf "$state_dir"' EXIT INT TERM

state_file_for_target() {
  local target="$1"
  local safe

  safe="$(printf '%s' "$target" | tr -c '[:alnum:]_.-' '_')"
  printf '%s/%s.state' "$state_dir" "$safe"
}

agent_kind() {
  local target="$1"
  local title command

  title="$(tmux display-message -p -t "$target" '#{pane_title}' 2>/dev/null || true)"
  command="$(tmux display-message -p -t "$target" '#{pane_current_command}' 2>/dev/null || true)"

  if [[ "$title $command" =~ [Cc]odex ]]; then
    printf 'codex'
  elif [[ "$title $command" =~ [Cc]laude ]] || [[ "$command" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    printf 'claude-code'
  else
    printf 'unknown'
  fi
}

contains() {
  local text="$1"
  local pattern="$2"
  printf '%s\n' "$text" | grep -qiE "$pattern"
}

classify_pane() {
  local pane="$1"

  if contains "$pane" 'esc to interrupt|Compacting|Cerebrating|Effecting|Thinking|Working|running|tokens\)|· [0-9]+\.?[0-9]*[ms] ·|↑ [0-9]+ tokens'; then
    printf 'WORKING|busy or not safely interruptible'
    return
  fi

  if contains "$pane" 'API Error: Server is temporarily limiting requests|This request would exceed your account.s rate limit|AIProvider::Errors::RateLimited|rate limited by AI provider|provider.*rate limit|HTTP 429|[^0-9]429[^0-9]'; then
    printf 'THROTTLED_IDLE|provider throttling or quota limit; eligible for approved handoff'
    return
  fi

  if contains "$pane" 'fatal error|panic:|traceback \(most recent|command not found|unknown command|connection (refused|reset)|out of context|auto-compact crashed'; then
    printf 'ERROR|visible error, rejected command, crash, or non-throttle failure'
    return
  fi

  if contains "$pane" 'do you want|would you like|want me to|should I|choose|select an option|\? \(y/n\)|waiting for (your|user)|needs guidance|need guidance|blocked on|not sure'; then
    printf 'ASKING_IDLE|waiting for guidance or a decision'
    return
  fi

  if contains "$pane" 'clear (the )?context|/clear|compact (the )?(conversation|context)|/compact|fresh context|new context|context used|context remaining|resume this session'; then
    printf 'CONTEXT_REQUESTED_IDLE|context management recommended or needed'
    return
  fi

  if contains "$pane" 'REVIEWS\.md|review complete|cross-AI review|cross AI review|plan review.*complete|--reviews'; then
    printf 'REVIEW_DONE_IDLE|review boundary detected'
    return
  fi

  if contains "$pane" 'PLAN COMPLETE|PLAN\.md (written|created|ready)|plan ready|ready to execute|ready for execution|verification loop.*pass'; then
    printf 'PLAN_DONE_IDLE|plan boundary detected'
    return
  fi

  printf 'IDLE|idle with no recognized boundary'
}

emit_if_needed() {
  local target="$1"
  local agent="$2"
  local state="$3"
  local summary="$4"
  local state_file previous

  state_file="$(state_file_for_target "$target")"
  if [[ -f "$state_file" ]]; then
    previous="$(cat "$state_file")"
  else
    previous="INIT"
  fi

  if [[ "$once" == "1" || "$state" != "$previous" ]]; then
    case "$state" in
      WORKING|IDLE)
        if [[ "$once" == "1" || "$include_idle" == "1" ]]; then
          printf '%s | %s | %s | %s\n' "$target" "$agent" "$state" "$summary"
        fi
        ;;
      *)
        printf '%s | %s | %s | %s\n' "$target" "$agent" "$state" "$summary"
        ;;
    esac
    printf '%s' "$state" > "$state_file"
  fi
}

while true; do
  for target in "${targets[@]}"; do
    agent="$(agent_kind "$target")"

    if ! pane="$(tmux capture-pane -t "$target" -p -S "-$lines" 2>/dev/null)"; then
      emit_if_needed "$target" "$agent" "GONE" "tmux target disappeared"
      continue
    fi

    classification="$(classify_pane "$pane")"
    state="${classification%%|*}"
    summary="${classification#*|}"
    emit_if_needed "$target" "$agent" "$state" "$summary"
  done

  if [[ "$once" == "1" ]]; then
    exit 0
  fi

  sleep "$interval"
done
