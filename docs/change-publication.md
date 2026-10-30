# Change publication

## The tree the checks ran against is the tree that reaches the pull request

Checks run against the workspace at `services/orchestration/materialization.py`. Publication commits that same workspace's staged tree object and pushes it. No second checkout, no file mapping, and no re-application of edits sits between the two, so there is no step at which the published content can drift from the checked content.

`WorkspaceMaterializer.stage` runs `git add -A` followed by `git write-tree` in the workspace and returns the resulting tree hash. `build_commit_request` puts the workspace path and that tree hash on the `CommitRequest`. Each git host then names that tree directly in `git commit-tree` and pushes the commit it produces.

This replaces an earlier design in which the adapter cloned the repository into a temporary directory and rewrote it from a `dict` of path to text. That mapping could not express a deletion, lost file modes, recorded a rename as an addition without the matching removal, and raised on any file that is not valid UTF-8. Each of those was a way for the pull request to carry something other than what passed the checks.

`git commit-tree` rather than `git commit` for two reasons. It names the parent explicitly, so a workspace materialized at the base still publishes onto a branch that survived from an earlier execution instead of being rejected as a non-fast-forward. It also runs no hook and reads no `commit.gpgsign` setting, either of which has previously reported success while leaving no commit behind when the client repository configured them.

## The verification that enforces it

No caller trusts the commit it just made. Every git host re-reads the destination after pushing and returns the tree now published there as `CommitResult.published_tree`. `GitHubMediator.commit_and_push` compares that value with the tree on the request and raises `PublishedTreeMismatch` when they differ, logging both hashes with the repository, branch and commit.

The comparison sits in the mediator rather than in its callers because every write already routes through it. Placing it in the `commit_and_push` activity would have left `bounded_task_pipeline` publishing unverified, and would have made the invariant something a future caller could forget.

A mismatch is unrecoverable rather than advisory. Reporting a passing check against content the pull request does not carry is the failure this contract exists to prevent, so the publish fails rather than allowing the run to continue.

## Idempotency

A commit replays when the tree already published on the branch equals the tree the workspace staged. Identity is the content, not the execution.

An execution-scoped marker cannot serve here. The repair loop publishes several distinct commits under one execution id, and keying on that id served every later repair the first commit's result. The checks then reported a green run for content that was never pushed.

Keying on the tree also removes the non-fast-forward failure a retry would otherwise cause. A retry that follows a push which succeeded before its acknowledgement was lost finds its own tree at the branch head and replays instead of pushing a rival commit.
