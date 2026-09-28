# jev-router 0.3: cache-aware delegation

## Goal

The user gives an agent a goal and lets it work for an hour. They want those runs
faster and cheaper, in tokens weighted by API price (a stand-in for subscription usage),
at the same quality. 0.2.0 cannot do that: it decides once per typed message, never
routes a follow-up, and ranks jobs by difficulty. The study below shows the money is
somewhere else.

Success: in the fork experiment (Validation), delegated branches cost less than kept
branches on the jobs the router picks, with no drop in quality, and the router
rarely picks a job where delegating loses.

## What the study found

Source: 30 days of Claude Code transcripts on this machine, 1,673 typed interactive
messages that triggered at least one model call, repriced as Opus 5.5 at OpenRouter
list prices. Charts: the private "Delegation Break-Even" artifact
(https://claude.ai/artifact/6DWAC22HQGB9ueMZv8p3cN). The extraction, cost model and Jev
scoring were reviewed by Codex GPT-6 Astra over four rounds until it returned "sound";
the context comparison in point 5 came after that review.

1. **Job length is the key variable.** The median message triggers 4 model calls, the
   90th percentile 31, the maximum 363. Messages of 11 or more calls are 28% of
   messages and 77% of the cost.
2. **Re-reading the context is the main cost.** The median context when the user types
   is 304k tokens, and cached re-reads are 61% of the cost. Every call re-sends the
   whole conversation; at 1M tokens that is $0.20 a call even when cached.
3. **So a fresh helper pays off on long jobs, whatever its model.** Delegation costs
   the parent a brief and a relay over its full context, then the helper works on a
   small context. Break-even with default assumptions: about 3 calls at 1M context, 5
   at 300k, 8 at 50k. With perfect foresight, delegating the right jobs saves 33% with
   a fresh helper on the same model and 39% with Sonnet 5. These are conditional
   simulations: they assume the helper does the job as well, from a brief.
4. **Follow-ups carry the savings.** 135 of the 151 long jobs in the Jev sample were
   messages Jev flags as follow-ups ("ok go", "iterate until the PR is ready"). 0.2.0
   never routes those.
5. **Jev can estimate job length, with context.** Asked a Score question about steps,
   Jev ranks job length with a rank correlation of 0.36 from the message alone and 0.56
   with the agent's previous reply (AUC for 11+ calls: 0.71 and 0.80). The last six
   messages, the session title and the latest compaction summary add little beyond
   the previous reply (0.58 and 0.59). Latency stays about 250 ms (p95 about 370 ms)
   with 3k tokens of context. Its mistakes are mostly the safe kind: it predicts fewer
   steps than happen far more often than more (49% against 13%).
6. **Jev's size question is a poor length signal.** Only its "large" pick is mostly
   long (74%); "hardest" is long 20% of the time.

## Design

### When the router decides

Unchanged trigger: the per-message hook in each harness (UserPromptSubmit in Claude
Code and Codex, `chat.message` in opencode). Changed scope:

- **Follow-ups are no longer excluded.** A follow-up is where the long jobs are. The
  parent has the context and writes the brief; it keeps the job when the work is a
  conversation with the user (a brainstorm, a review with questions) or cannot be
  separated.
- **Runs started by another agent are skipped**, detected from the environment the
  harness inherits (`CLAUDECODE` / `CLAUDE_CODE_CHILD_SESSION` from Claude Code, and
  the equivalent markers Codex and opencode set, to be verified per harness). This
  replaces "interactive only": cross-AI reviews are agent-launched, so they stay
  untouched, while headless runs the user starts (scripts, evals) are routed.
- **`JEV_ROUTER=on|off` overrides everything** for one run, so evals can run routed and
  unrouted side by side without touching the global switch; `JEV_ROUTER_HOME` gives each
  run its own log.
- Slash commands, skill invocations and task notifications stay excluded.

### What the router reads

From the session transcript (`transcript_path` in the Claude Code and Codex hook
payloads; the session's messages through the SDK client in opencode):

- the current context size: the last call's input, cache reads and cache writes;
- the session's model;
- tokens added and produced per call, averaged over the session's recent calls, in
  place of fixed assumptions;
- the agent's previous reply, for Jev.

The Codex transcript's `token_count` events carry `last_token_usage`
(`input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`,
`reasoning_output_tokens`); opencode messages carry `tokens.{input,output,reasoning}`
and `tokens.cache.{read,write}`. Each harness gets a small reader; if the transcript
cannot be read, the router has no opinion.

### What the router asks Jev

One Decisions API call, provider pinned to TypeSafe, 2 s deadline:

- `steps`, a Score over five buckets (1, 2-3, 4-10, 11-30, 31+ calls), as in the study;
- `size`, the existing Choice, now used only to pick the helper's model and effort.

State: the message and the agent's previous reply. The follow-up question is dropped.

### The decision

For each bucket b with Jev's probability P(b) and a representative length k_b (1, 2, 6,
18, 50 calls), the router prices keeping and delegating with the study's model, using
the session's own numbers: the current context, its model's prices, and its recent
per-call averages. It delegates when the expected saving,
sum over b of P(b) x (keep(k_b) - delegate(k_b)), is above a margin: at least $0.25 and
at least 15% of the expected cost of keeping. The margin exists because the model
leaves out helper failures; the fork experiment sets its final value.

The helper: the tier Jev's size picks, inside the same harness (0.2.0's `tiers.json`).
When the saving comes from a fresh context and not a cheaper model (the size tier is
the session's own model and effort), the helper runs on the session's model.

### What the session is told

When the router delegates: the job, why (expected saving, context size, predicted
length), and the helper to use. The parent writes a self-contained brief: the goal,
the files and decisions that matter, what done looks like. It keeps the job only if
the work needs the user in the loop, or it is certain it already runs on the helper's
model and effort. When the router does not delegate, it says nothing: no keep note,
since the "Done by" sign-off only appears on delegated work.

### Shadow mode

Before it instructs anything, the router runs in shadow mode (`"mode": "shadow"` in
`config.json`): it computes and logs the decision, Jev's probabilities and the inputs,
and tells the session nothing. A report joins each logged decision with the number of
calls the turn really took and its real cost from the transcript. This calibrates the
margin on live sessions with no risk.

## Validation: the fork experiment

Fork points: moments in real sessions where the next message starts a long job the
router would delegate. At each one:

1. Fork the session twice (`claude --resume <id> --fork-session`; `codex fork` or
   `codex exec fork <id>`), each fork in its own git worktree at the same commit, so the
   branches cannot edit each other's files.
2. Send the same message to both. The delegate branch gets the hand-off instruction
   directly, so the experiment measures whether a helper can do the job, apart from
   whether Jev picked the job.
3. Run each side three times, to see run-to-run variance.
4. Measure from the transcripts, helpers included: API-equivalent cost, wall time,
   calls. Judge quality with the project's tests where they exist and a blind Codex
   review that compares the results without knowing which branch made them.

At least ten fork points across Claude Code and Codex, mixed between jobs the router
would and would not delegate. The margin is then set where delegating is cheaper at
equal quality.

## Out of scope

Per-call model switching and API proxies (they lose the cache and do not work with
subscriptions), OpenRouter's `typesafe/jev-router` (API billing, cross-provider),
and a full orchestrator.

## Open questions

- Which environment markers Codex and opencode set in the shells they start.
- Whether opencode can read per-message token usage fast enough inside `chat.message`.
- How often the parent overrides a delegation, and whether that needs its own rule.
