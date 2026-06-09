# GSD Workflow Adapter

Read this only when monitoring panes that are actively using the GSD workflow.

## User-Approved Local Mapping

In the user's current Claude Code GSD setup, commands are hyphenated:

```text
/gsd-review <phase> --all
/gsd-plan-phase <phase> --reviews
/gsd-execute-phase
```

The colon form can appear in agent prose, for example:

```text
/gsd:plan-phase 9 --reviews
```

In this environment that form was observed to fail with an "Unknown command"
message and a suggestion to use the hyphenated form.

Use the hyphenated mapping only when the current workflow has explicitly
approved it or the installed commands have been verified on disk. Otherwise,
surface the mismatch and ask.

## Typical Boundary Sequence

When a GSD plan finishes and the user has approved full auto-advance:

1. Send `/gsd-review <phase> --all`.
2. When review is done and the pane is idle, send
   `/gsd-plan-phase <phase> --reviews`.
3. When review findings are incorporated and the pane is idle, clear context
   only if approved and the agent is not asking for guidance.
4. Send `/gsd-execute-phase`.
5. After execution completes, run the user-approved cross-AI code review flow.

Do not use `/gsd-code-review` as a substitute for external Codex/Gemini CLI
review unless the user explicitly chooses that internal reviewer.
