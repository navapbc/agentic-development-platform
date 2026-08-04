# Executive Demo Narration

A spoken script for a recorded walkthrough of an in-progress build. Each act lists the command to run, what the audience sees, and the words to say. The narration reflects what the platform runs today. Where a step is a stand-in for a component still in progress, the script says so plainly, and the closing section lists what is planned.

## Opening (cover slide)

Show the cover slide with the title, subtitle, and your name.

> Thank you all for joining. My name is Joe Robinson, and this is a state of development update on the Agentic Development Platform. In the next few minutes I will introduce what the platform is, show the guarantees it already provides, and walk through it carrying a tracked ticket all the way to a pull request.

## Agenda

Advance to the agenda slide.

> Here is how the session is laid out. First a short platform overview, how it picks up a task from Jira, makes changes in an isolated sandbox development environment, validates them through policy and compliance checks, and opens a draft pull request for review. Next, the guarantees the platform already provides, durability, grounded context, safe concurrency, and auditable end to end delivery. Then the demo itself, where I run the platform end to end and we watch it work.

## High-level overview

Advance to the Platform Overview slide.

> The platform goes through five steps from a tracked Jira ticket to a pull request a person approves. Every step along the way is durable, auditable, and safe to run with many agents at once across many repositories.

> It uses durable orchestration that survives crashes, repository intelligence that grounds the work, an AI coding agent that writes the small, well-defined change in a sandboxed pod, a coordination safety layer that prevents double-work and duplicate side effects, and draft pull request creation on GitHub through those safeguards.

> The design goal is that a team points the platform at their repositories and Jira board. It then behaves like a careful junior engineer who never loses work, never double-commits, and always leaves an evidence trail.

Advance to the What the Platform Already Guarantees slide.

> This demo proves four properties.
>
> 1. Durability means work survives process crashes and resumes exactly where it left off.
> 2. Grounded context means the work is planned against the repository intelligence service.
> 3. Safe concurrency means multiple agents cannot collide, double-claim work, or post duplicate side effects.
> 4. Auditable delivery means a single pull request is produced through correctly-ordered steps, with a full history you can trace.

> The platform is not tied to a single repository. It operates on every repository in its allowlist, so the same deployment serves many teams.

## The major sections of the platform, the technology, and why

Walk the audience through this table on screen, one row at a time. The status column is honest about this build. It shows what runs in the demo, what is built but not shown live here, and what is planned.

| Section | Technology | What we use it for | Status in this demo |
| --- | --- | --- | --- |
| Durable orchestration | Temporal | Runs every execution as a workflow with automatic retry, timeout, and resume. A worker crash does not lose state. The workflow continues from its last durable step. | Live (Acts 1 and 4) |
| Model access | LiteLLM in front of AWS Bedrock | A single doorway to the Claude and Amazon Nova AI models with an ordered backup list, so if one model is down it falls back to the next instead of failing. | Live in Act 1 when the stack and Bedrock path are up |
| Repository intelligence (RIS) | Zoekt code search plus SCIP and LSP code maps, merged by a router | Reads the repository and builds a short briefing pack of the code that matters for this task: the key functions, the files most likely to change, what calls what, the related tests, the libraries involved, and recent changes. The most relevant items come first, and each one is labeled with where it came from. This is how the agent knows where to work instead of blindly searching text. | Live (Act 2) when the target repo is indexed |
| Coding agent | OpenHands agent server in a sandboxed pod | Carries out one small, well-defined task from the briefing pack, under hard limits on cost, tokens, tool calls, and wall-clock time. | Live (Act 4): the sandbox agent authors the change under enforced caps. |
| Ticket integration | Custom Jira adapter for Cloud and Data Center | Jira is the single source of truth. We capture a locked snapshot of the issue, re-check it before opening a PR, and post follow-up links that never duplicate. | Live (Act 4): snapshot, re-check, follow-up link that never duplicates, and status changes against the real Jira Cloud ticket. |
| Source control | GitHub adapter over the REST and git HTTPS APIs | Creates branches, commits, and draft pull requests. The target host is set by configuration, so the same code works against github.com or a company GitHub server without any code changes. | Live (Act 4, real PR) |
| Coordination service | PostgreSQL-backed safety layer: exactly-once ledger, ordering gate, work locks, file-aware scheduler | The safety layer for running many agents at once. It makes sure every outside action, like a comment or a pull request, happens exactly once, throws out stale or out-of-order steps, stops two agents from grabbing the same piece of work, and only runs work in parallel when it touches different files. | Live (Acts 3 and 4), backed by the durable Temporal workflows in Act 4 |
| Policy and review | OPA, plus the rebar review kernel | Enforces policy decisions and review gates before a change is allowed to proceed. | Built as components, not wired into the Act 4 pipeline yet |
| Persistence and audit | PostgreSQL | Stores execution state and an append-only audit trail of every state transition and decision. | Live (Act 1) |
| Cleanup | Temporal scheduled reaper | Closes or deletes expired agent branches and draft PRs so the platform does not leave stray branches and pull requests behind. | Built, not shown in these acts |

## Act 1: durable execution that survives a crash

What happens: the platform starts a durable workflow, saves its state in Postgres, gathers a briefing pack of the most relevant code from the repository (we look at that pack in Act 2), then runs a long task that saves its progress every step. That long task is a stand-in for real work: it counts from 0 to 15, one step per second, and each step is saved as a durable checkpoint before the next one starts. A real change would be many small steps like this. The counter makes the progress visible on screen. While that task is running you restart the worker on camera twice, to show the recovery is repeatable and not a fluke. Each time, the task freezes at the step it had reached, and when the worker comes back the same workflow instance resumes from that exact step. It finishes the task and answers with the AI model using the briefing pack it gathered before the crash. Nothing restarts from the beginning and nothing is lost.

Prerequisite: the stack is up (`inv local-up`), the Bedrock path is up (`inv connectivity-check`), and the repo is indexed by the intelligence service. The crash-and-resume part is pure durability and needs only Temporal and Postgres, but the workflow always ends with the AI model's reply, so the run only completes when the Bedrock path is up. Bring the tunnel up before recording this act.

Say before running the command:

> Let's prove those four properties one at a time, starting with durability. I'll kick off an execution against one of those repositories.

Command:

```
inv demo-durable-task --repo agentic-demo-service
```

Say while it starts up and reads the repository:

> The first thing it does is record its state in Postgres, and behind the scenes it reads the repository to gather a briefing pack it will use for its final answer. We will look at that pack closely in the next act. For now, what matters is whether it can survive a crash.

Say as the count begins to climb:

> Now it is running a long task. To make it easy to see, the task counts from 0 to 15, one step per second, saving each step before it moves on. Think of each number as one small unit of work that the platform records as done. While the count is climbing, I will stop the worker to simulate a crash.

Run the printed restart command in another terminal while the steps are advancing:

```
docker compose -f compose.yaml -f compose.override.yaml restart workflow-worker
```

Say as the worker restarts and the count resumes:

> I just restarted the worker process. In a fragile system, the job would be lost and it would start over from the beginning. Here, the count freezes, because for a moment there is no worker. When the worker comes back, Temporal hands it the same run at the exact step it had reached, and it carries on counting from there.

Run the same restart command a second time, once the count has climbed a little further:

```
docker compose -f compose.yaml -f compose.override.yaml restart workflow-worker
```

Say as it resumes the second time:

> I will do that once more, so you can see the first recovery was not a fluke. Again the count freezes while the worker is gone, and again the same run picks up from the exact step it had reached. However many times the worker is restarted, no work is repeated and nothing is lost.

Say once the count reaches 15 and the saved history and the model's reply have printed:

> Here is the saved history that survived both restarts, and here is the model's reply, grounded in the briefing pack we gathered at the start of the process.

## Act 2: repository intelligence, the briefing pack up close

What happens: this is the same briefing pack the durable workflow just used in Act 1, now on its own so we can slow down and read it. It calls the repository intelligence service directly and prints the pack, most relevant items first, with each item labeled by where it came from, plus a count of how many items of each kind it found. Unlike Act 1, there is no workflow and no AI model here, so this act runs even when Bedrock is down.

Prerequisite: the repository intelligence service is up and the target repo is indexed. If it is not indexed yet, index it before recording. An unindexed repo returns an empty package.

Say before running the command:

> The answer from that last command was grounded in the repository. The repository intelligence service maintains a structural map of the codebase and answers the platform's queries against it. Without it, an agent would have to search the code and map it out from scratch every session to work out where a change belongs and what impacts the change would have. Let me run the same briefing pack on its own, so we can slow down and read what the workflow was working from.

Command:

```
inv demo-repo-intelligence --repo agentic-demo-service
```

Say over the output:

> To build this, it does three things at once. It runs a fast text and symbol search across the whole repository, it reads precomputed code maps that already know where every function and type is defined and everywhere it is used, and a router merges those results, ranks them by relevance, and labels each item with the source it came from. That combination is why it finds the code that matters instead of the code that merely mentions a keyword.

> For this repository and the task we just asked about, the pack lists the functions that matter, the files most likely to change, what calls what around them, the related tests, the libraries involved, and recent related changes. Every item is labeled with where it came from, so we can trust it. This is what the coding agent plans against, and it is why it stays on target instead of guessing.

## Act 3: safe concurrency and no double-work

What happens: this act runs the coordination safety layer against the real stack. It captures a locked snapshot of a real Jira ticket, pins the run to the current commit of the target repository, then exercises the safety primitives directly against Postgres. The key moment is a live race, where several agents try to claim the same piece of work at once and the database itself lets exactly one win. Every result on screen is a real row written to the coordination tables, with content hashes, claim ids, and timestamps that change on every run, so it cannot be a scripted echo. The run takes a few seconds because each step is a real database round trip.

Prerequisite: the stack is up (`inv local-up`) and Jira Cloud credentials are set so it can read the real ticket. No Bedrock is needed for this act.

Say before running the command:

> So far we have run one thing at a time. In production, many agents run at once, and the coordination safety layer is what keeps them from colliding.

Command:

```
inv demo-coordination --task ADSER-13 --repo agentic-demo-service
```

Say over the snapshot and the pinned inputs:

> First it takes a locked snapshot of the Jira ticket and pins the run to the exact commit of the repository, then records those inputs so the run can be reproduced. The content hash on screen is computed from the ticket, and the code version is the commit.

Say over the claim race:

> Now the claim race. Several agents try to claim the same unit of work at the same moment, and the database lets only one of them win and turns the rest away. The screen shows the winning claim with its id and the version and time it was granted. Two units that touch different files are claimed at once because they cannot collide, and a third that overlaps a file already being changed is held back.

Say over the ordering gate and the exactly-once ledger:

> The ordering gate applies steps in order, then discards a manually triggered step that arrives after later ones were applied, writing a discard receipt rather than acting on it. The ledger records outside actions, so a Jira comment happens only once even if the step is retried. The closing summary counts the rows this run wrote to the coordination tables, keyed to this run's id, so you can read them in Postgres afterward.

## Act 4: a real, auditable pull request

What happens: the platform starts a durable Temporal workflow that runs the small, well-defined task end to end on the workers. It claims the work, has the AI coding agent write the change in a sandboxed pod, synchronizes the branch, and opens a real draft pull request on GitHub, with every outside action going through the same exactly-once safety layer. Run the same command a second time and the workflow detects the open PR and reuses it instead of duplicating, proving each action happens exactly once.

Prerequisite: the stack is up (`inv local-up` runs Temporal, Postgres, and the workflow and activity workers), the Bedrock path is up (`inv connectivity-check`), Jira Cloud credentials are set, and `--task` names a real, not-done Jira ticket whose work is not already implemented. Before any GitHub or Jira write, the command runs two quick health checks, one for the AI model path and one for the Jira ticket, and stops cleanly if either fails.

Show the starting state before running the command. Open the Jira ticket ADSER-13 in the browser and point out its status and that no pull request is linked to it. Then open the repository's pull request list on GitHub and show there is no open pull request for this ticket's branch.

Say while showing the starting state:

> Before I run anything, let me show where we are starting from. This is the ticket the platform will pick up, and it is sitting in To Do with nothing linked to it yet. Over on GitHub, the pull request list has nothing open for this ticket's branch.

Say before running the command:

> Now we bring those three properties together and add the fourth, auditable delivery. This single command starts a durable workflow on the platform's Temporal workers that carries a Jira ticket all the way to a pull request. As the platform picks up the ticket it moves it from To Do to In Progress, and once the draft pull request is open it moves it to the review stage. That move happens too fast to catch on screen, so afterward I will open the ticket history to show both transitions. Which statuses the platform moves the ticket through is configurable, set on this command by the start-status and review-status options. Before it changes anything outside the platform, it runs two quick health checks, the model path and the Jira ticket, and stops cleanly if either one is unhealthy.

Command:

```
inv demo-draft-pr --repo agentic-demo-service --task ADSER-15 --start-status "In Progress" --review-status "In Review" --via-temporal
```

Say as the steps complete:

> It moved through a fixed sequence, from a locked snapshot of the ticket, to claiming the work, to the coding agent writing a small, well-defined change in a sandboxed pod, to opening a draft pull request. Each step printed as it completed, so the full sequence is on screen for reference.

Say while showing the pull request in GitHub:

> Here is the draft pull request the platform opened on GitHub.

Say while showing the Jira ticket and its history:

> Back on the Jira ticket, it is now at the review stage, and the platform has posted a link to that same pull request, so the trail connects the ticket to the work. Opening the history, you can see the two moves it made, from To Do to In Progress when it picked up the work, and on to the review stage when the draft pull request was open.

Say before running the exact same command a second time:

> In production the same ticket can get started more than once, by a retry after a timeout, by two operators, or later by an automated trigger firing twice. Without a guard, that would open a second pull request and post a second link on the ticket. Let me run the exact same command again and show what the platform does instead.

Command:

```
inv demo-draft-pr --repo agentic-demo-service --task ADSER-15 --start-status "In Progress" --review-status "In Review" --via-temporal
```

Say over the second run:

> This time, before doing any work, the platform finds the open pull request already tied to this ticket's branch and stops. The guard only prevents duplicate delivery. Whether the change satisfies the ticket is the review and policy gate we are still wiring in.

## Closing (say this last)

Say after the last act, to wrap up the demo:

> That is the complete loop. A ticket became a reviewable pull request without losing work to a crash, without guessing at the code, without two agents colliding, and with a full trail you can audit. Those are the four properties we set out to prove, durability, grounded context, safe concurrency, and auditable delivery, and everything shown runs on the platform. The next milestone is wiring the review and policy gates into this same flow, so a change must pass policy before the pull request opens.

## What runs today versus what is a stand-in

Real and running in this demo:

- Durable Temporal orchestration with crash recovery (Act 1).
- Bedrock model calls through LiteLLM with an ordered backup list (Act 1, stack up).
- Repository intelligence briefing packs, each item labeled by where it came from (Act 2, repo indexed).
- The coordination safety layer against the real Postgres stack, driven by a locked snapshot of a real Jira ticket and the pinned repository commit, with a live claim race the database arbitrates, work locks, file-aware scheduling, an ordering gate, and an exactly-once ledger (Act 3).
- A durable Temporal workflow that claims, implements, syncs, commits, and opens a real GitHub draft PR with a ticket linkback, all through the exactly-once safety layer (Act 4).
- The OpenHands coding agent writing the small, well-defined change in a sandboxed pod under hard limits (Act 4).
- Live Jira Cloud snapshot, re-check, follow-up link that never duplicates, and status changes (Act 4).
- Postgres execution state and the append-only audit trail (Act 1).

Stand-ins in this build:

- The review and policy gates are built and tested as standalone components, but they are not yet wired into the Act 4 delivery flow. Composing them into the pipeline with Temporal approval pauses is Phase 6.

## Future features (planned, not in this demo)

Grounded in the delivery plan:

- AI coding agent hardening: the OpenHands agent already writes the small, well-defined change live in Act 4 under hard limits on cost, tokens, tool calls, and wall-clock time. Remaining work is broader task coverage and reliability (Phase 5).
- Review and policy gates in the main flow: the rebar review kernel and OPA decisions with Temporal approval pauses before a PR opens (Phase 6).
- Automated ticket intake: a Jira poller and inbound webhook that start executions with no operator command, duplicate-safe (Phase 11).
- Per-repository credentials: different owners, tokens, or GitHub hosts per repo from one deployment (Phase 12).
- Observability and evaluation: Langfuse prompt management, OpenTelemetry tracing across stages, and context-accuracy scoring (Phase 7).
- AWS transition: EKS workloads, image-based Lambda handlers, RDS, S3 WORM evidence, Secrets Manager and KMS (Phase 8).

## Setup for the recording

- Durable path for Act 4: bring the stack up with `inv local-up` so Temporal, Postgres, and the workflow and activity workers are running before `inv demo-draft-pr --via-temporal`.
- GitHub target for Act 4: set `GITHUB_API_URL=https://api.github.com`, `GITHUB_GIT_BASE_URL=https://github.com`, `GITHUB_OWNER`, and a `GITHUB_TOKEN` with contents and pull-request write. The same adapter targets an enterprise GitHub host by changing those values. Draft PRs are always allowed on public repositories.
- Multi-repository: the platform operates on every repository in `ALLOWLIST_REPOS`. The demo selects one with `--repo`.
- Trigger: executions are started by an operator command today. Automated Jira intake is planned (Phase 11). Acknowledge this if asked how a ticket auto-starts a run.
