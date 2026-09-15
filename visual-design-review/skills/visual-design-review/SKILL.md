---
name: visual-design-review
description: Use when a user wants to review a system design or specification visually or steer consequential design choices without reading a long document. Organizes decisions and records feedback using available visualization skills; drawing a single diagram without a design review belongs to its diagram skill.
---

# Visual design review

Help an experienced reader see the whole design, challenge its assumptions, and steer the choices that matter. The review interface is concise and visual; the specification remains the implementation reference.

This skill organizes the review. It does not supply a rendering framework, replace the planning workflow, or authorize implementing the system being discussed.

## Establish what needs a decision

- Read the relevant existing spec, evidence, and conversation. Identify the objective, constraints, current versus proposed behavior, and decisions already made. Preserve accepted choices. Flag new evidence that challenges them without changing their status; ask the user to revisit a choice when its consequences require a changed decision.
- Distinguish verified facts, estimates, assumptions, and unresolved questions. Attach source references to claims and decisions so the reader can inspect the evidence on demand.
- Treat behavior the source does not mention as unknown, not as absent or forbidden. Describe a proposed mechanism's intended benefit without promising a guarantee that has not been established, especially across crashes, retries, or concurrent work.
- Surface choices with meaningful consequences for behavior, system boundaries, cost, security, operations, or reversibility. Use judgment about consequence; do not turn every implementation detail into a checkpoint.
- Give each decision a stable identifier, such as D1, that survives view and document changes. Define the actual question and its affected components before choosing a visual.
- If no consequential choice is open, say so and show only the requested explanation or compact recap. Do not manufacture alternatives or ask for another approval.

## Compose with available capabilities

- Discover the skills and rendering tools available in the current environment before selecting the delivery surface. Respect the user's requested format.
- For a process flowchart or interactive process walkthrough, load the shared `flowchart` skill when available. Let it own process layout and keyboard guidance instead of copying those rules here.
- For an inline surface owned by an available visualization skill, load that skill and follow its rendering contract. Codex's bundled `visualize` skill is one such capability; do not assume it or its host APIs exist in Claude Code.
- A standalone browser document or a supported Mermaid diagram can be selected up front when that matches the request and available capabilities. Neither requires a Codex-specific host. Missing explicitly requested skills or unsupported chosen formats must be reported; do not silently substitute, auto-install dependencies, or switch delivery surfaces after a failure.
- Choose the representation that answers the question. Do not require every review to contain every diagram type:
  - Architecture diagrams: boundaries, responsibilities, trust, and what changes.
  - Flowcharts: decisions, process order, and reachable outcomes.
  - Sequence diagrams: who communicates with whom, timing, and failure handling.
  - State diagrams: lifecycle changes and valid transitions.
  - Tables: a few alternatives and qualitative tradeoffs.
  - Quantitative charts: measured or explicitly estimated cost, latency, capacity, or other quantities. Show units, sources, and assumptions; do not fabricate precision or turn qualitative judgments into arbitrary scores.
  - Mockups: user-visible behavior or interaction choices.

## Present the review

1. Start with a compact overview: the objective, major components or stages, and where the open decisions sit. Keep it at component or stage level; put branch-specific mechanics in their decision views instead of one exhaustive graph. Make current and proposed behavior visibly distinguishable.
2. For each consequential open decision, show the question, viable alternatives, the recommendation and its rationale, material costs or disadvantages, and remaining uncertainty. Use a visual where it clarifies the consequence of choosing differently; a short table can be enough. Do not invent alternatives merely to fill a comparison.
3. Let the reader inspect one choice at a time while retaining access to the overview and evidence. Keep presentation length proportional to the decisions, not to the source document's length; do not convert every paragraph into a slide.
4. End with a recap of accepted choices, proposals, rejected or deferred options, and unresolved questions. This is a record of decisions, not a claim that the proposed system exists or has been deployed.

Ask a concrete question when human input is needed. Preserve the user's free-text correction or alternative; do not restrict them to the agent's options. Do not make them read the full specification as the primary review interface unless they request it.

## Carry feedback into the design

- Record each decision's question, status, current recommendation or choice, rationale, evidence, and affected spec section. Use statuses such as proposed, accepted, rejected, deferred, and open consistently; keep the alternatives' dispositions clear when a choice is accepted.
- Mark a decision accepted only when the user's statements or existing authorization establish acceptance. Viewing a diagram, clicking Next, reaching Summary, or an AI review does not establish human approval. A local diagram control is not a submitted decision unless a real delivery mechanism confirms it.
- Update the visual recap and the corresponding spec or decision record from the same choices. When feedback changes an earlier choice, preserve why it changed and identify dependent assumptions or decisions that need revisiting. Do not silently regenerate already accepted choices into different ones.
- Accepting a mechanism does not settle its open sub-decisions. Leave those branches explicitly unresolved in both the visual and the spec instead of filling them with a convenient default.
- If the relevant spec cannot be edited, provide the precise proposed update and state that it has not been applied. Keep proposed and applied updates distinct.
- Follow the session's authorization and review rules. Do not add another approval gate for routine work or already approved decisions. Ask for material unresolved input while continuing independent authorized work; do not implement work dependent on an unanswered required decision.

## Verify before handing over

- Walk each depicted path and alternative against the source. Check ownership, failure outcomes, omitted dependencies, and that uncertainty remains visible rather than being polished away.
- Keep alternatives distinct in the visual. A proposed shortcut or dotted edge must still represent a meaningful transition; styling does not excuse a path that skips required work or combines incompatible options.
- Apply the selected rendering skill's QA to the actual delivered surface. For a browser review, exercise the advertised controls and inspect screenshots at the user's window width and a narrow width before opening it for the user. Check that navigation changes presentation only and does not imply acceptance.
- Compare the visual recap and spec against the user's actual answers. Check stable IDs, statuses, explicit deferrals, and that accepted choices changed only when the user revised them.
- Open the verified review artifact when the environment supports it. State delivery limitations accurately; do not describe an inaccessible file or an unrendered diagram as tested.
- When maintaining this skill, forward-test on a small realistic design with both settled constraints and open choices, then supply a user correction and check the resulting record. Evaluate decision quality and fidelity, not the presence of particular headings or phrases.
