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

- A ticket number identifies a unit of grouped work, like a Jira ticket, not a single commit. One ticket can span multiple commits, so reuse the same number for every commit that belongs to that unit of work. Increment to the next number only when starting a new unit of work. Numbers are sequential per repository and are never derived from research item, phase, or task numbers (research item R7 does not imply `ADEVX-7`).
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

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **agentic-dev-platform** (2685 symbols, 5422 relationships, 232 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `gitnexus_impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `gitnexus_detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `gitnexus_query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `gitnexus_context({name: "symbolName"})`.

## Never Do

- NEVER edit a function, class, or method without first running `gitnexus_impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `gitnexus_rename` which understands the call graph.
- NEVER commit changes without running `gitnexus_detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/agentic-dev-platform/context` | Codebase overview, check index freshness |
| `gitnexus://repo/agentic-dev-platform/clusters` | All functional areas |
| `gitnexus://repo/agentic-dev-platform/processes` | All execution flows |
| `gitnexus://repo/agentic-dev-platform/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
