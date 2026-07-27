# Agent Working Rules

Generic, tool-agnostic working rules. These are personal preferences for how any coding agent should behave when assisting me.

## Git

- Never run `git commit` or `git push`. Make the changes, validate them, stage only if asked, then output the commit message in a code block for me to run myself.
- "Mark complete", "finish phase X", or a pre-approved commit message is not authorization to commit. If a step seems to need a commit, produce the message and let me run it.
- Commit message shape: ticket number alone on the first line, immediately followed on the next line (no blank line between them) by a markdown bulleted list where every bullet starts with `- ` and a short action verb (Add, Detect, Remove, Fix), one per task. Keep bullets terse and factual, no "so that" rationale. Each bullet must be self-contained and describe the change as it stands in the repo. Do not add a `Co-Authored-By` trailer. Example:

  ```
  ADEVX-2
  - Add pluggable Bedrock connectivity providers
  - Remove OSRE jump-host constants from Python defaults
  ```

- Ticket numbers are sequential per repository: increment from the last committed ticket. They are never derived from research item, phase, or task numbers (research item R7 does not imply `ADEVX-7`).
- Ticket number prefixes by repository: `agentic-dev-platform` uses `ADEVX-<number>`, `agentic-demo-service` uses `ADSERX-<number>`, `agentic-demo-greenfield` uses `ADGRX-<number>`.

## Prose and punctuation

- Never use em-dashes, en-dashes, or space-hyphen-space as sentence connectors or parenthetical separators anywhere (prose, documentation, chat). Rewrite the sentence instead. Do not substitute a plain hyphen for a dash.
- Acceptable hyphen uses: compound words, technical names and identifiers, markdown list bullets, table separators, horizontal rules, and code or SQL operators.
- Do not hard-wrap paragraphs in markdown. Write each paragraph as a single logical line and let editors and viewers handle soft-wrapping. Keep line breaks only where markdown semantics require them.

## Code

- Default to writing zero comments. Only add a comment when removing it would actively mislead a reader: a non-obvious constraint, a workaround for a specific bug (with a reference), or an invariant callers depend on that is not enforced in code.
- Do not add narrative context, forward-looking notes, references to files that do not exist yet, plain restatements of what the code does, or explanations of why a stub or mock exists. Backstory belongs in the PR description, not the source.

## Shell commands

- Write any command meant to be copy-paste-run on a single line. Do not use backslash line continuations, however long the command gets. Multi-line scripts with real logical structure (HEREDOCs, chained control flow) are the exception.

## Scope discipline

- Keep out-of-scope resources out of the diff. Do not edit unrelated resources even to restore them to their original state. If a file mixes in-scope and out-of-scope resources, only touch the in-scope ones and flag the rest to me.
- Curate the output of setup or scaffolding tools before accepting it. After any generator runs, review every file it produced and cut anything that does not fit the project. Treat auto-generated config (CI workflows, pre-commit configs, templated instruction files, gitignore edits) as suspect by default.

## Issue handling

- Never dismiss an issue I explicitly call out as "pre-existing" or "a false positive." Treat any bug, warning, or error I flag as in scope. Investigate it, explain what it actually is, and either fix it or give a concrete reason it cannot or should not be fixed.

## Working style

- I am a technical infrastructure engineer. Prefer engineering terms, CLI commands, and configuration details over plain-language explanations.
