---
name: flowchart
description: Use when asked to explain a process with a flowchart, improve a confusing flow diagram, or build an interactive step-by-step process walkthrough. Covers diagram clarity and keyboard usability, not statistical charts or implementing the production workflow being illustrated.
---

# Clear flowcharts and walkthroughs

Help the reader answer a concrete process question. Preserve the user's requested format and scope; drawing a deployment does not authorize deploying it.

## Choose the right representation

- Establish the audience, the question, the process boundaries, and whether the diagram describes current behavior or a proposal. Infer these from available context before asking questions.
- Use a small static flowchart when labeled steps and arrows answer the question. Mermaid is useful for diagrams kept as text; verify its syntax against the renderer version when needed.
- Use editable draw.io or Lucidchart documents when the user wants to maintain the diagram in that tool. Do not replace their chosen format with HTML.
- Use an HTML walkthrough when stepping, selecting scenarios, or inspecting details materially helps understanding. Do not add interaction only for decoration.
- If an available visualization skill owns the requested inline surface, follow its rendering contract. Otherwise produce the requested file or diagram using available tools; this skill does not require a particular host or browser library.

Read `references/design-and-sources.md` for source-backed design decisions, format details, or interactive keyboard checks.

## Establish the process before arranging shapes

1. Extract real steps, dependencies, decision points, responsibilities, and outcomes from the supplied evidence. Identify missing facts rather than inventing connections.
2. Distinguish chronological order from data transfers and commands. A file store does not send a deployment command merely because it appears earlier in the process.
3. Identify start and finish points. Give decision exits meaningful labels, including failed or waiting outcomes when relevant to the question.
4. Walk through the proposed sequence against the evidence. Preserve important branches and concurrency; do not force every process into a linear walkthrough.

## Make the diagram understandable

- Use a consistent left-to-right or top-to-bottom direction. Align related shapes and avoid crossing or overlapping connectors.
- Name actions with short verbs. Explain unfamiliar terms in ordinary language before using technical labels alone.
- Identify who acts and what moves. For an internal action, show meaningful internal components or one process node; do not invent a second external party to satisfy a layout.
- Use conventional shapes when their meaning helps, and use color sparingly with text or shape cues so color is never the only signal.
- Make the result inviting to read: give it breathing room, a clear visual hierarchy, and a readable content width with outer gutters. Use restrained, consistent color to identify roles or outcomes, with readable neutral labels. Avoid edge-to-edge rows that separate related labels at wide browser sizes.
- Reflow the process at narrow widths instead of hiding most of it in a horizontally scrolling canvas or shrinking a fixed SVG until labels become tiny. Keep labels readable at their actual rendered size; stack explanatory fields when columns leave only a few words per line.
- Show essential context and the current position. When global relationships matter, provide an overview of the process alongside step details; progressive disclosure must not hide the structure the reader needs to understand.
- Keep proposed, observed, pending, succeeded, and failed states distinct. An explanatory scenario is not live telemetry.
- Do not imply success after a failure, automatic recovery that does not exist, or an outcome that the evidence cannot verify.

## Interactive walkthroughs

- Choose the number of steps from the process, not a fixed template. Let users go backward, forward, and directly to a step where useful.
- If overview and detail views coexist, preserve selection when switching and keep focus on visible controls. Avoid redundant navigation; a final recap may deliberately link back to its detailed steps.
- For a teaching walkthrough, consider a final summary frame that reconnects the smaller steps into the whole process. Keep an overview available earlier when it helps orientation. The summary is a presentation frame, not an extra process action or proof that actions completed; show only the selected scenario's reachable path and truthful outcome.
- Define summary boundaries, backward navigation, direct-step jumps, mode changes, and scenario resets. Keep focus visible when a recap link opens a detail, and announce the summary and its outcome without inventing another numbered action.
- Keep navigation controls in a stable position with constant labels. Display start/end status separately; do not silently wrap unless that behavior is requested.
- Put Previous and Next before variable-height explanations or summaries so their screen position stays stable. Preserve existing focused buttons when updating selection; if elements must be replaced, explicitly move focus to the corresponding visible control. A persistent parent does not preserve focus when its children are rebuilt.
- Match keyboard behavior to the delivery surface. A standalone walkthrough should accept its advertised arrows on a fresh page, while an embedded diagram must not capture the surrounding application's shortcuts.
- Make clicking the diagram canvas or explanatory text activate its keyboard controls when appropriate. Preserve native controls, label-to-control focus, text selection, modifier shortcuts, visible focus, and a way to Tab out.
- When a scenario changes the step list, define where selection moves and announce the new position. Do not leave focus on removed elements.
- Use native controls and a concise accessible announcement of the current step. Avoid duplicate screen-reader narration and unrequested automatic playback.

## Verify the delivered result

A hidden focus-setup test is not end-to-end validation.

- Open the exact delivered file or embedded surface. Test the ordinary first action before programmatically focusing a control.
- For advertised arrow navigation, test fresh load, canvas/text/background clicks, both directions, boundaries, and one advancement per key. Test native selects and inputs separately from step navigation.
- Check repeated mouse clicks at the same coordinates, not only locator-based clicks that find a moving button again.
- Exercise every relevant branch/scenario, narrow layouts, live resizing, and supported themes. Inspect text clipping, connector direction, focus behavior, and JavaScript errors.
- Capture and inspect the actual delivered layout at the user's browser width as well as narrow widths. Judge spacing, grouping, readable line lengths, and useful color, not just the absence of overflow. Use measured content widths for responsive assertions when gutters or maximum widths apply.
- Test inline and exported versions separately; iframe wrappers and focus boundaries can change keyboard behavior even when the diagram code is identical.
- For multiple views, check the overview's complete sequence, selection/focus across view changes, and stable navigation positions across views and scenarios.
- Inspect internal diagram overflow as well as page overflow. After direct-step activation, verify the actual focused element, then continue with the keyboard; a working global arrow handler can hide focus lost to BODY.
- Record what was actually tested and any access or browser limitation. Do not describe a test browser as the user's live tab, or synthetic key events as proof of native popup behavior.
- Follow the session's review and delivery rules. Do not introduce a new release, installation, or approval policy solely because this skill was used.

When maintaining this skill, forward-test changed guidance on a separate realistic process with minimal prompting. Inspect the generated diagram and exercise its advertised interactions; source review alone does not establish output quality. Record the example and limitations rather than claiming one successful sample proves reliability.
