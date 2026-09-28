# jev-router 0.3: cache-aware delegation

## Goal

The user gives an agent a goal and lets it work for an hour. They want those runs
faster and cheaper, in tokens weighted by API price (a stand-in for subscription usage),
at the same quality. 0.2.0 cannot do that: it decides once per typed message, never
routes a follow-up, and ranks jobs by difficulty. The study below shows the money is
somewhere else.

Success, measured in the fork experiment (Validation) with a rule frozen beforehand: on
the jobs the router selects, delegating costs less than keeping; quality does not drop
by the criteria fixed in advance; the 95% upper bound on the share of selected jobs
where delegating loses money or quality is at most 15%; and delegating is not slower,
with a median wall-time ratio (delegate over keep, helpers included) of at most 1.0 and
no job more than 1.5 times slower.

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
4. **Follow-ups hold the opportunity.** 135 of the 151 long jobs in the Jev sample were
   messages Jev flags as follow-ups ("ok go", "iterate until the PR is ready"). 0.2.0
   never routes those. Whether a brief carries enough of the conversation for such a
   job is unmeasured; the fork experiment measures it.
5. **Jev can rank job length, with context, but is not calibrated.** Asked a Score
   question about steps, Jev ranks job length with a rank correlation of 0.36 from the
   message alone and 0.56 with the agent's previous reply (AUC for 11+ calls: 0.71 and
   0.80). The last six messages, the session title and the latest compaction summary
   add little beyond the previous reply (0.58 and 0.59). Latency stays about 250 ms
   (p95 about 370 ms) with 3k tokens of context. Its probabilities run short: it gives
   the 31+ bucket 4.9% on average while 12.9% of jobs land there, and those jobs average
   93 calls.
6. **Jev's size question is a poor length signal.** Only its "large" pick is mostly
   long (74%); "hardest" is long 20% of the time.

## Design

### Scope

Claude Code and Codex, where a helper is a subagent with its own fresh context. Claude
Code can delegate once shadow mode and the fork experiment pass. Codex runs in shadow
mode only, because the study's transcripts are all Claude Code: it delegates live only
after its own calibration (from its shadow logs), its own pilot and its own frozen
validation pass, with the same fork protocol run through `codex exec fork`. opencode is excluded from
delegation: its hook moves the message onto another model in
the same session, which keeps the whole context and so buys none of the fresh-context
saving. opencode keeps 0.2.0's behaviour until an isolated helper session exists there.

### When the router decides

Unchanged trigger: UserPromptSubmit in Claude Code and Codex. Changed scope:

- **Follow-ups are no longer excluded.** They hold the long jobs. The parent has the
  context and writes the brief; it keeps the job when the work needs the user in the
  loop (a brainstorm, a review with questions) or cannot be briefed.
- **Headless runs stay unrouted by default**, detected as in 0.2.0. A run the user wants
  routed, such as an eval, opts in per command with `JEV_ROUTER=on`. `JEV_ROUTER=off`
  forces routing off in any run. The cross-AI review commands in
  `~/.claude/rules/cross-ai-review.md` and `~/.codex/AGENTS.md` gain `JEV_ROUTER=off`,
  on top of Codex's `--disable hooks`, so a review is never routed even if it is
  started by hand or its harness looks interactive.
- **`JEV_ROUTER_LOG=<path>`** sends one run's log elsewhere, so routed and unrouted eval
  runs can be compared. Config and key stay in the router home.
- Slash commands, skill invocations and task notifications stay excluded.

### What the router reads

From the session transcript (`transcript_path` in both hook payloads), read from the
end of the file only, so a 30 MB transcript costs the same as a small one:

- the current context size, per harness:
  - Claude Code: the last main-thread call's `input_tokens + cache_read_input_tokens +
    cache_creation_input_tokens` (all three are disjoint);
  - Codex: the last `token_count` event's `last_token_usage.input_tokens`, which already
    includes `cached_input_tokens` (uncached input is the difference);
- the session's model;
- tokens added and produced per call, averaged over the session's last 20 calls;
- the agent's previous reply, capped at 1,500 characters.

If any of it cannot be read, the router has no opinion.

### What the router asks Jev

One Decisions API call, provider pinned to TypeSafe, 2 s deadline, with the message
(capped at 4,000 characters) and the previous reply as state:

- `steps`, a Score over five buckets (1, 2-3, 4-10, 11-30, 31+ calls), as in the study;
- `size`, the existing Choice, now used only to pick the helper's model and effort.

The follow-up question is dropped. The privacy notice in `on` and `status` changes to
say that the agent's previous reply is sent along with the message.

### Calibration

Jev's bucket probabilities are not used as they come. A calibration table maps Jev's
step score to the observed distribution of call counts, per harness: for Claude Code,
fitted on the study's labelled turns with half held out for checking; for Codex, fitted
the same way once its shadow logs hold enough labelled turns. The saving is priced over that
empirical distribution, not over one representative length per bucket, so the long
tail (the 31+ bucket averages 93 calls) is priced as it really is. The table ships as a
data file next to `tiers.json` and is refitted from shadow-mode logs.

### The decision

For the calibrated distribution of the job's length k, the router prices keeping and
delegating with the study's model, using the session's current context, its model's
prices and its recent per-call averages, and computes the expected saving
E[keep(k) - delegate(k)], and the probability that delegating costs more,
P(delegate(k) > keep(k)). It delegates only when both gates pass: the expected saving is
at least $0.25 and at least 15% of E[keep(k)], and the probability of losing money is at
most 20%. The second gate matters because a rare very long job can make the expected
saving positive while most such jobs lose. The margins stand in for what the model
leaves out (helper failures, re-reading files); they are tuned on shadow logs and pilot
forks, then frozen before the validation forks.

The helper is the tier Jev's size picks, inside the same harness (`tiers.json`). The
saving often comes from the fresh context alone, so a helper on the session's own model
and effort is a valid choice, and matching models is no reason to keep a job.

### What the session is told

When the router delegates: the job, why (expected saving, context size, predicted
length) and the helper to use. The parent writes a self-contained brief: the goal, the
files and decisions that matter, and what done looks like. Helpers never write outside
the repository: the brief tells the helper to stop before any push, pull request,
deploy or write to an external service and report what is left, and the parent does
those steps itself after the relay. This holds whatever the message says, so no
question about external effects is needed, and it is what the fork experiment
validates. It keeps the job only when
the work needs the user in the loop or cannot be briefed. When the router does not
delegate, it says nothing.

### Shadow mode

Before it instructs anything, the router runs in shadow mode (`"mode": "shadow"` in
`config.json`): it computes and logs the decision, Jev's score, the calibrated
distribution and the inputs, and tells the session nothing. A report joins each logged
decision with the number of calls the turn really took and its real cost from the
transcript. Shadow mode supplies the large sample: calibration, how often the router
would select a job, and how often the cost model says a selected job loses. It cannot
measure whether a helper does the job well; the fork experiment does.

## Validation: the fork experiment

It measures what shadow mode cannot: the real cost, speed and quality of delegating a
job the router selects. It runs in two phases: a pilot, whose forks and shadow logs
tune the margins, and then a validation phase with the rule frozen, on new jobs only.

1. **Select before sending.** When the user is about to send a message in a Claude Code
   session, an experiment command asks the router for its decision on that message
   without sending it. Only messages the router selects go on, taken as they come,
   including follow-ups and borderline selections. The sampling unit is one selected
   message, and each validation point comes from a different session, so outcomes are
   independent.
2. **Fork before the turn.** The runner forks the session twice (`claude --resume <id>
   --fork-session`) before the message is sent anywhere, and checks that both forks'
   transcripts end at the same entry. It copies the full working state into two
   worktrees, including uncommitted and untracked files, and checks that both copies
   hash the same. Both forks run with pushes, pull requests, deploys and other writes
   outside the repository denied, and both are told to stop before such steps, so the
   two forks do the same local work and neither can change what the other sees. A
   helper that tries such a write fails the point.
3. **Run.** Both forks run with `JEV_ROUTER=off`, so the router never fires inside the
   experiment. The same message goes to both; the delegate fork also gets exactly the
   hand-off text the live router would add. The runner checks in its transcript that the
   named helper ran; if the parent kept the job, that is an override, and it counts as a
   loss (retries are for the pilot only). The two forks run one after the other, in
   random order. Each side runs once; for five points both sides run twice more to
   measure run-to-run variance, and the first run is the one scored.
4. **Measure** API-equivalent cost, wall time and calls from the transcripts, helpers
   included, plus any cross-AI review the work triggers (its CLI session logs, matched
   by the fork's worktree path). Costs are priced from token counts, not from what was
   billed: the shared pre-fork transcript at the cache-read price and every new token
   (the message, the hand-off, everything after) at the cache-write price, so which
   fork ran first, or whether the cache had expired, cannot change the comparison.
5. **Judge** quality by criteria fixed before any run: the project's tests pass where
   they exist, the job's stated outcome is met, and a blind review comparing the two
   results without knowing which fork made them does not prefer the kept one. The blind
   reviewer is never the harness that did the work: Codex judges Claude Code forks, and
   Claude or DeepSeek judges Codex forks.

A validation point counts as a loss when delegating costs more, fails a quality
criterion, or is overridden. The validation phase is fixed in advance at 30 points and passes with at
most one loss: the exact one-sided 95% upper bound on the loss rate is then 14.9%. It
does not stop early or add points.

## Out of scope

Per-call model switching and API proxies (they lose the cache and do not work with
subscriptions), OpenRouter's `typesafe/jev-router` (API billing, cross-provider), a full
orchestrator, and delegation in opencode.

## Open questions

- How often the parent overrides a delegation, and whether that needs its own rule.
- Whether Codex's calibration needs its own study, since the transcripts studied are
  Claude Code only.
