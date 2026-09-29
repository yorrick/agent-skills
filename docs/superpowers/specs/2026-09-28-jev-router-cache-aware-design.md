# jev-router 0.3: cache-aware delegation

## Goal

The user gives an agent a goal and lets it work for an hour. They want those runs
faster and cheaper, in tokens weighted by API price (a stand-in for subscription usage),
at the same quality. 0.2.0 cannot do that: it decides once per typed message, never
routes a follow-up, and ranks jobs by difficulty. The study below shows the money is
somewhere else.

The design aims for the simplest thing that reaches that goal: the harness's own
subagents, one Jev call per message, and a small real-world check.

Success, measured in the fork check (Validation) on the next 20 jobs the router selects
that the user marks safe to replay (skips counted), with its rule frozen beforehand,
and with the router's own cost and added
latency on every message of the check period counted against delegation: delegating
costs at least 10% less in total than keeping; delegated results are as good (no
delegated result fails a test or its stated outcome where the kept one passes, and a
blind judge prefers the kept result no more often than the delegated one); and
delegating is not slower in total (summed wall time, subagents included).

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
   job is unmeasured; the fork check measures it.
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

Claude Code and Codex. The helper is the harness's own subagent, started with a fresh
context (What the session is told), so it buys the fresh-context saving without any new
process or runner. Both harnesses start in shadow mode. Claude Code delegates live once
its fork check passes. Codex has no transcripts in the study, so it first gets its own
calibration from its shadow logs, then its own fork check. opencode keeps 0.2.0's
behaviour; its task subagents could carry the same design later.

### When the router decides

Unchanged trigger: UserPromptSubmit in Claude Code and Codex. Changed scope:

- **Follow-ups are no longer excluded.** They hold the long jobs. The parent has the
  context and writes the brief; it keeps the job when the work needs the user in the
  loop (a brainstorm, a review with questions) or cannot be briefed.
- **Headless runs stay unrouted by default**, detected as in 0.2.0, so a review started
  from another harness is never routed. A headless run the user wants routed, such as
  an eval, opts in with `JEV_ROUTER=on`; `JEV_ROUTER=off` forces routing off in any run.
- Slash commands, skill invocations and task notifications stay excluded.

### What the router reads

From the session transcript (`transcript_path` in both hook payloads), read from the
end of the file only, so a 30 MB transcript costs the same as a small one:

- the current context size, per harness:
  - Claude Code: the last main-thread call's `input_tokens + cache_read_input_tokens +
    cache_creation_input_tokens` (all three are disjoint);
  - Codex: the last `token_count` event's `last_token_usage.input_tokens`, which already
    includes `cached_input_tokens`;
- the session's model;
- tokens added and produced per call, averaged over the session's last 20 calls;
- the agent's previous reply, capped at 1,500 characters.

If any of it cannot be read, the router has no opinion.

### What the router asks Jev

One Decisions API call, provider pinned to TypeSafe, 2 s deadline, with the message
(capped at 4,000 characters) and the previous reply as state:

- `steps`, a Score over five buckets (1, 2-3, 4-10, 11-30, 31+ calls), as in the study;
- `size`, the existing Choice, now used only to pick the helper's tier.

The follow-up question is dropped. The privacy notice in `on` and `status` changes to
say that the agent's previous reply is sent along with the message.

### Calibration

Jev's bucket probabilities run short, so they are not used as they come. A calibration
table, shipped as a data file next to `tiers.json`, maps Jev's step score to the
observed distribution of call counts. For Claude Code it is fitted on the study's
labelled turns, with half held out for checking; for Codex, the same way once its
shadow logs hold enough turns. The saving is priced over that empirical distribution,
so the long tail (the 31+ bucket averages 93 calls) is priced as it really is.

### The decision

For the calibrated distribution of the job's length k, the router prices keeping and
delegating with the study's model, using the session's current context, its model's
prices and its recent per-call averages. It delegates only when the expected saving
E[keep(k) - delegate(k)] is at least $0.25 and at least 15% of E[keep(k)], and the
probability that delegating costs more is at most 20%. The second gate matters because
a rare very long job can make the expected saving positive while most such jobs lose.
The margins stand in for what the model leaves out (a helper re-reading files, a failed
hand-off); they are tuned on shadow logs and a few trial forks, then frozen before the
fork check.

The helper is the tier Jev's size picks (`tiers.json`). The saving often comes from the
fresh context alone, so a helper on the session's own model and effort is a valid
choice, and matching models is no reason to keep a job.

### What the session is told

When the router delegates, it adds a short note: the job, why (expected saving, context
size, predicted length) and the helper to use. The parent writes a self-contained brief
(the goal, the files and decisions that matter, what done looks like) and hands it to a
subagent with a fresh context:

- **Claude Code:** the tier subagent 0.2.0 already ships (`tiny`, `everyday`, `large`,
  `hardest`), whose frontmatter sets the model and effort. Never the `fork` subagent
  type, which copies the whole conversation and would erase the saving.
- **Codex:** `spawn_agent` with the tier's `model` and `reasoning_effort` and
  `fork_turns: "none"`. The default, `all`, copies the whole conversation.

The parent keeps the job only when the work needs the user in the loop or cannot be
briefed. The helper runs with the session's own permissions, like any subagent the user
already runs. The brief tells it not to commit, push, open pull requests or deploy, and
to report what is left; the parent reviews the changes and does those steps after the
relay. When the router does not delegate, it says nothing.

### Shadow mode

Before it instructs anything, the router runs in shadow mode (`"mode": "shadow"` in
`config.json`): it computes and logs the decision, Jev's score, the calibrated
distribution and the inputs, and tells the session nothing. A report joins each logged
decision with the number of calls the turn really took and its real cost from the
transcript. Shadow mode supplies the large sample: calibration, how often the router
would select a job, and how often the cost model says a selected job loses. It cannot
show whether a subagent working from a brief does the job as well; the fork check does.

## Validation: the fork check

It measures what shadow mode cannot: the real cost, speed and quality of delegating
the jobs the router selects, on the user's own work.

1. **Snapshot selected jobs.** With the rule frozen and `"fork_check": true`, whenever
   the router (still in shadow mode) selects a job, the hook saves a snapshot before the
   turn runs: a copy of the transcript ending just before the message, the message, and
   the working copy's state (HEAD, uncommitted changes and untracked files) with a hash
   of its ignored setup files and installed dependencies. A snapshot is small; no clone
   or fork exists yet. The user works as usual; the real session is never touched.
2. **The user picks what is safe to replay.** A replay has the same access as the
   user's session, so it would repeat whatever the real turn did outside the machine.
   After the real turn finishes, the runner lists each snapshot with those actions, read
   from the real turn's transcript (pushes, pull requests, MCP writes, deploys, messages
   sent), and the user marks which ones to replay. The list is a guide, not a promise:
   a replay can do something the real turn did not, so the user marks a snapshot only
   if two replays in a row can run without an external effect they would mind, and
   without the first changing the second's task. The check takes the next 20 marked
   jobs, follow-ups included; the report counts the skipped ones and why.
3. **Replay both ways.** A runner restores each marked snapshot into two full clones.
   Nothing is installed: the working copy's ignored files (setup files and installed
   dependencies) are copied in, and if their hash no longer matches the snapshot's, the
   job is skipped. It saves the transcript copy as a new session of each clone, with its
   own id, so the user's real session is never resumed, and resumes it headless with
   `JEV_ROUTER=off` and the same message. The delegate side also gets exactly the note
   the live router would add; the keep side gets nothing. Replays run like the user's
   session: bypass permissions, the same MCP servers, network and credentials. The one
   difference is that each clone's `origin` is a local bare copy, so a normal push stays
   local (a push to an explicit URL or another remote still could not be stopped). The
   two sides run one after the other in random order. When both are done, the runner
   pushes the two results to a private GitHub fork of the repository (one per
   repository, or a private copy where forking is not allowed) as `replay/<job>/keep`
   and `replay/<job>/delegate`, and opens a pull request between them, so the user can
   compare the results side by side.
4. **Measure** API-equivalent cost, wall time and calls from the transcripts, subagents
   included, pricing every call by its recorded categories (cache reads, cache writes,
   uncached input, output). Right before each side runs, a one-line throwaway fork of the
   same transcript, with no tools, warms the cache, and a pair is scored only when both
   sides' first calls read the prefix from cache; otherwise it is rerun. Every run is scored on what
   it really cost and took, including a parent that kept a job it was told to delegate;
   the report counts these overrides. The shadow log's Jev cost and latency over every
   message in the check period are added to the delegate side.
5. **Judge** quality by criteria fixed in advance: the project's tests pass where they
   exist, the job's stated outcome is met, and a blind review compares the two results
   without knowing which side made them. The blind reviewer is never the harness that
   did the work: Codex judges Claude Code jobs, and Claude judges Codex jobs.

The check passes when all of the conditions in Goal hold over the 20 jobs. Twenty jobs
is a practical sample for a personal tool, not a statistical guarantee, so the report
also shows every job: its predicted and real length, both costs, both times and the
verdict. The 10% condition applies to the 20 selected jobs marked safe to replay;
skipped jobs have no measured counterfactual. The report also states
the saving as a share of the whole period's cost (from the shadow log), without a
threshold, since that share depends on how the user's work mixes long and short jobs.
Codex gets the same check (`codex exec fork`) once it is calibrated.

## Out of scope

Per-call model switching and API proxies (they lose the cache and do not work with
subscriptions), OpenRouter's `typesafe/jev-router` (API billing, cross-provider), a full
orchestrator, separate helper processes and sandboxes (the harness's subagents do the
job), and delegation in opencode.

## Open questions

- How often the parent overrides a delegation, and whether that needs its own rule.
