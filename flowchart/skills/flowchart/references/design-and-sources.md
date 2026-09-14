# Design decisions and sources

Read the relevant source when a diagram choice needs explanation or a tool's syntax needs verification. These are guidance and references, not proof that a particular generated diagram is understandable to its audience.

## Process accuracy

[ASQ: Flowchart](https://asq.org/quality-resources/flowchart) describes defining scope and boundaries, sequencing activities, and checking a draft by walking through it with people who know the process. It also explains conventional process, decision, wait, input/output, and terminal symbols. Apply those conventions when they make meaning clearer; visual polish cannot compensate for an incorrect process.

## Direction, ownership, and connectors

[Lucidchart: How to make a flowchart](https://app.lucid.co/diagram/flowchart/how-to-make-a-flowchart) recommends obvious start/end points, consistent direction, restrained color, and swimlanes when ownership matters. A small diagram can label responsibility directly instead of adding lanes.

[draw.io: Work with connectors](https://www.drawio.com/docs/manual/connectors/) explains information/control relationships and fixed versus floating attachment points. Label important transfers with what moves or happens. Recheck connector attachment after moving shapes; a visually adjacent line is not necessarily a connected edge.

## How much to show

[NN/g: Progressive disclosure](https://www.nngroup.com/articles/progressive-disclosure/) distinguishes optional detail from a staged, linear sequence. A walkthrough can explain a sequential process one step at a time; interdependent decisions or concurrent work may need to remain visible together. Keep a useful overview or position indicator so the reader does not lose context.

## Tool choice

- [Mermaid flowchart syntax](https://mermaid.js.org/syntax/flowchart.html): useful for a compact, version-controlled diagram. Check support in the actual renderer, especially newer shapes or interactions, and avoid assuming syntax from a different version.
- draw.io and Lucidchart: useful when people need to rearrange and maintain editable diagrams. Preserve an explicitly requested tool or format.
- HTML/SVG: useful when interaction teaches something static labels cannot. Prefer a small diagram and native controls; do not turn an explanation into an application dashboard.

## Keyboard behavior depends on the surface

[W3C APG: Developing a keyboard interface](https://www.w3.org/WAI/ARIA/apg/practices/keyboard-interface/) explains predictable focus, conventional key assignments, and native-control behavior. The concrete checks below come from an observed export regression, not a claim that APG prescribes one implementation.

In that regression, a keydown listener on a root div worked after tests focused a numbered button. It failed on fresh load and after canvas clicks because focus remained on BODY; an exported iframe also left fresh-page keys in the outer document.

For a standalone page, implement page-level shortcuts only within that page's intended diagram scope. For an embedded visual, activate keyboard focus through interaction with the visual, and keep the surrounding application's shortcuts intact. Do not add global host-page listeners or cross-frame forwarding merely to make an embedded test pass. An iframe is a legitimate boundary; test it explicitly, and avoid adding unnecessary wrappers to standalone exports.

Check these entry paths in the actual delivered artifact:

1. Open the page and press the advertised keys without hidden setup.
2. Click the canvas, heading, explanation, and background, then press them again.
3. Click a form label and confirm its native control keeps focus and key behavior.
4. Navigate in both directions, stop at both ends, and verify each press advances exactly once.
5. Keep focus and control positions stable through step changes; repeated clicks at one position should still hit Next.
6. Leave the visual with Tab or a click outside it; an embedded visual must not keep handling keys intended for its host.
7. Resize across the layout breakpoint and check the generated export independently from the inline version.

Do not fix an automated test's inability to operate a native popup by replacing the user's native control. Distinguish application errors from automation limitations, compare with a plain control where useful, and report the verification limit accurately.
