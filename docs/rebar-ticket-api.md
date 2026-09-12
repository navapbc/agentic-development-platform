# Rebar Ticket API Reference

This reference documents the in-process rebar Python API that the platform's rebar ticket backend calls. It is written against installed `nava-rebar` 0.12.0 and grounds implementation units U2 and U7 of the pluggable ticket and source-control backends plan.

## Model

Rebar stores tickets as an event log inside a git-backed directory. Every call takes a `repo_root` keyword that points at the git working tree holding the ticket store. Reads compile the event log to current state, and writes append events through a locked write path. The S3 sync is rebar's own remote via `git-remote-s3` and is not driven from these calls. The platform consumes an already-mounted store and does not modify rebar's storage mechanism.

Ticket identifiers are 16-hex canonical ids such as `9dec-9a24-3939-41a5`. Aliases and short ids resolve on read.

## Initialization

`init_repo(*, repo_root=None, force_new_store=False) -> None` mounts the ticket system explicitly. Remote discovery fails closed by default. `force_new_store=True` permits bootstrap only while reachability is unknown. Other library calls do not auto-init and require this to have run first. The platform seeds a store from an operator-provided Excel export through rebar's own import capability, then this backend reads and writes the mounted store.

## Read operations

`show_ticket(ticket_id, *, repo_root=None, include_inbound=False) -> TicketState` returns the compiled ticket state as a dict. `include_inbound=True` adds an `inbound_deps` key derived at read time from other tickets' stored link events.

`list_tickets(*, status=None, ticket_type=None, priority=None, parent=None, has_tag=None, without_tag=None, include_archived=False, exclude_deleted=False, min_children=None, blocking_state="", with_children_count=False, sort=None, full=True, repo_root=None) -> list[TicketState]` lists tickets with optional filters. `status` accepts a comma-separated set such as `"open,in_progress"`. `sort` orders by `priority|created|updated|id|status` with a `-` prefix for descending.

`search(query, *, status=None, ticket_type=None, has_tag=None, include_archived=False, sort=None, full=False, repo_root=None)` returns bounded discovery results by default. The query accepts field predicates such as `status:`, `type:`, `priority:`, `assignee:`, `tag:`, and `parent:`. Pass `full=True` for full ticket states.

`ready(*, sort=None, repo_root=None) -> list[TicketState]` returns tickets whose blockers are all closed.

## Write operations

`create_ticket(ticket_type, title, *, parent=None, priority=None, assignee=None, description=None, tags=None, source=None, bridge_project=None, repos=None, return_alias=False, repo_root=None) -> str | CreateResult` creates a ticket and returns the canonical id by default. `source` carries import provenance keys `source_id`, `source_created_at`, `source_author`, and `source_env`, recorded on the create event.

`transition(ticket_id, current_status, target_status, *, force=False, reason="", force_close=None, close_class="", caused_by="", ref=None, repo_root=None) -> TransitionResult` moves a ticket's status under optimistic concurrency. It raises `ConcurrencyError` when the ticket's actual status no longer matches `current_status`, and `RebarError` for other failures. The `open -> in_progress` transition is gated by the plan-review claim gate. Pass `force=True` with an optional `reason` to bypass that gate. `TransitionResult` is a dict with keys `ticket_id`, `from`, `to`, and `newly_unblocked`.

`comment(ticket_id, body, *, source=None, repo_root=None, allow_secret_pattern="") -> None` appends a comment. `source` carries per-comment provenance. Comments read back as dicts with `body`, `author`, `author_email`, and `timestamp`.

`edit_ticket(ticket_id, *, repo_root=None, **fields) -> str | None` edits `title`, `priority`, `assignee`, `ticket_type`, and `description`. Tags mutate through `add_tags`, `remove_tags`, or `set_tags`.

`claim(ticket_id, *, assignee=None, force=None, repo_root=None) -> ClaimResult` atomically moves an open ticket to `in_progress` and sets its assignee in one locked section. It raises `ConcurrencyError` when the ticket is not open.

## Import and export

`import_tickets(source, *, dry_run=False, repo_root=None) -> dict` imports tickets from a rebar export NDJSON `source`, which is a file path, a file object, or an iterable of lines or dicts. Each imported ticket gets a fresh local id and fresh timestamps with the source identity preserved as `source_*` fields.

`export_tickets(*, out=None, status=None, ticket_type=None, parent=None, strip_external=False, include_session_logs=False, exclude_archived=False, include_deleted=False, repo_root=None) -> dict` exports the store as NDJSON, one full ticket per line, streaming through `reduce_ticket` for bounded memory.

The Excel seeding path is rebar's own capability. This backend reads and writes an already-seeded store. Import is documented here because the seeding stand-in and any operator reseed run through it.

## Change detection

`reduce_ticket(ticket_dir_path, *, include_retired=False, event_files_override=None) -> dict | None` compiles all events in one ticket directory to current state. Replay is in filename order with UUID dedup, so a duplicate event file applies exactly once.

`reduce_all_tickets(tracker_dir, *, exclude_archived=False, exclude_deleted=False, exclude_session_logs=False) -> list[dict]` batch-reduces every ticket in the `.tickets-tracker` directory.

For the U7 poll cursor, the tickets-branch head commit sha is the lossless watermark for one branch-head snapshot, and the per-ticket reduced state from `reduce_ticket` gives a stable per-ticket revision for field-change detection and idempotency before side effects.

## Identity

`resolve_current_identity(*, repo_root=None) -> str | None` resolves the current self-identity, preferring the `.rebar/current_identity` pointer, else a case-insensitive `git config user.email` match against identity tickets. It returns `None` on any miss and never raises.

`identity_email(local_assignee, *, repo_root=None) -> str | None` returns the matched identity's email, else `None`.

## Status vocabulary

The local status vocabulary is `idea`, `open`, `in_progress`, `closed`, `blocked`, `cancelled`, and `deleted`. `archived` is a separate flag rather than a status. A fresh ticket starts at `open`. The terminal statuses from the platform's lifecycle are `closed` and `cancelled`. `deleted` is a tombstone written together with an archive marker.

The platform's logical lifecycle stages resolve to this vocabulary through the status-flow map. The default resolution is `work_started -> in_progress`, `ready_for_review -> in_progress` where the store has no distinct review state, and the terminal stage to `closed`. The terminal status is an explicit field in the status-flow config so an unknown stage is skipped rather than invented.

## TicketState shape

`show_ticket` and `list_tickets` return dicts with these keys.

`ticket_id`, `alias`, `title`, `description`, `ticket_type`, `status`, `priority`, `assignee`, `author`, `author_email`, `authorship`, `created_at`, `updated_at`, `parent_id`, `deps`, `tags`, `repos`, `comments`, `file_impact`, `file_impact_scope`, `no_file_impact_reason`, `verify_commands`, `signature`, `managed_refs`, `bridge_project`, `bridge_alerts`, `env_id`, `creation_channel`, `claim_harness`, `claim_remote_session`, `claimed_session`, `plan_review_phase`, `preconditions_summary`, `keyring`, `reverts`.

Comments are a list of dicts with `body`, `author`, `author_email`, and `timestamp`.

## Concurrency

Transitions and claims use optimistic concurrency. A stale `current_status` on `transition`, or a non-open ticket on `claim`, raises `ConcurrencyError` with engine exit code 10. The backend maps this onto an idempotent no-op when the observed terminal state already matches the intended outcome, and surfaces it otherwise.

## Mapping to the TicketBackend contract

The rebar backend maps the `TicketBackend` methods onto this API.

- `get_issue` calls `show_ticket`.
- `get_status` reads the `status` field from `show_ticket`.
- `transition` calls `transition` with the observed `current_status` and the stage-resolved target.
- `add_comment` calls `comment`.
- `list_comment_bodies` reads the `comments` field and returns each `body`.
- `search_issues` calls `list_tickets` or `search` with the mapped predicates.
- `create_issue` calls `create_ticket`.
