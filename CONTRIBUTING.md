# Contributing

This guide covers how to set up a development environment, the standards your changes must meet, and how to get a change ready for review. Start with [README.md](README.md) to stand up the stack, then come back here to make changes.

## Ground rules

- Never commit secrets. `.env` holds local values and stays out of version control.
- Inference goes through the LiteLLM container to Amazon Bedrock.
- The `invoke` task runner and Python under `scripts/` are the primary developer surface.

## Development environment

1. Follow [First-time setup](README.md#first-time-setup) in the README to create the virtual environment and the stack.
2. Install the locked dependencies with `uv sync --locked --all-extras`. The review path depends on `rebar` (distribution `nava-rebar`) from PyPI through the `review` extra, which `--all-extras` includes. The platform image installs the `worker` extra and the repository-intelligence image the `dev` extra, both from the same lock.
3. Change a dependency by editing `pyproject.toml` and running `uv lock`, or bump one package within its range with `uv lock --upgrade-package <name>`. Commit `uv.lock` with the change. CI, `inv sast`, and both image builds install with `--locked` and fail when the lock is stale. A security fix is one change, `uv lock --upgrade-package <name>` and the updated lock. The lock records when each release was uploaded, and `inv sast` lists every locked release younger than seven days with its PyPI provenance, so review that list in the scan output before merging a dependency change.

## Branches and commits

- Branch from `main`. If an agent authors the branch, prefix it with `agent/` so the orphan reaper can recognize and clean it up later.
- Use a ticket number to group related commits. This repository uses the `ADEVX-<number>` prefix, but this is subject to change. One ticket often covers several commits, so keep the same number until that piece of work is done.
- Write the commit message as the ticket number on the first line, followed straight away by a bulleted list with no blank line in between. Start each bullet with an action verb such as Add, Remove, or Fix, and keep it short and factual.

```text
ADEVX-2
- Add pluggable Bedrock connectivity providers
- Remove jump-host constants from the Python defaults
```

## Coding standards

- Format and lint with ruff. The line length is 100. Run `ruff check .` and `ruff format .` before you push.
- Type-check with `basedpyright` on the packages you touch.
- Default to writing no comments. Add a comment only when removing it would mislead a reader, for example a non-obvious constraint or an invariant callers depend on that the code does not enforce.
- Keep your change focused. Do not refactor, rename, or add abstractions beyond the scope of your ticket, and keep out-of-scope files out of the diff.

## Tests

- Run the suite with `inv test`, which runs `pytest`. Unit tests live under `tests/unit`.
- Async tests run under `asyncio_mode = auto`, so you do not annotate coroutine tests.
- Add or update tests with any behavior change. Keep them deterministic and independent of test ordering.
- Measure coverage with `coverage` when you need it.

## Database migrations

- Migrations use Alembic and SQLAlchemy Core and are scoped to the `agent_platform` schema. Do not write migrations for the Temporal or Langfuse schemas.
- Create a revision, then apply it with `inv db-migrate`. Rebuild the schema from scratch with `inv db-reset` when a local database drifts.

## License

This project is licensed under the [Apache License, Version 2.0](LICENSE). Under section 5 of that license, a contribution you intentionally submit for inclusion is licensed under the same terms, with no additional sign-off or agreement. If you do not want section 5 to apply to a contribution, say so in the pull request before it is reviewed.

Authorship is recorded by the git author on each commit and is shown in the repository's contributor graph. Commit with a name and email that identify you.

## Before you open a pull request

1. `ruff check .` and `ruff format --check .` are clean.
2. `basedpyright` passes on the packages you changed.
3. `inv test` passes.
4. `inv smoke-test` passes against a running stack.
5. The diff contains only in-scope files, and `.env` and other secrets are not staged.
6. The commit message follows the shape above.

