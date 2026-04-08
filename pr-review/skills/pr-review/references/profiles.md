# Review Profiles

Profiles control model selection and effort level per AI provider. The orchestrating LLM reads the active profile and injects the corresponding CLI flags into each reviewer command.

Default profile: **balanced** (unless the user specifies otherwise, e.g., "review PR #365 with quality profile").

## Profile Definitions

```yaml
quality:
  description: "Maximum thoroughness — slower, higher cost"
  claude: "--model opus --effort max"
  gemini: "-m gemini-2.5-pro"
  codex: ""

balanced:
  description: "Default — good tradeoff of speed vs quality"
  claude: "--model sonnet --effort high"
  gemini: "-m gemini-2.5-flash"
  codex: ""

budget:
  description: "Fastest, cheapest"
  claude: "--model haiku --effort medium"
  gemini: "-m gemini-2.5-flash"
  codex: ""
```

## Usage

The skill reads this file at the start of Step 2, determines the active profile, and extracts the flags. The flags are substituted into each reviewer's CLI command.

Users can override by saying: "use quality profile", "review with budget", etc.

## Notes

- **Codex** uses its default model from `~/.codex/config.toml` (no `-m` flag). ChatGPT-authenticated accounts don't support all model names via `-m`. If using API key auth, you can pass `-m o4-mini` etc. as an override.
