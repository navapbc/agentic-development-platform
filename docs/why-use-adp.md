# The Agentic Development Platform on Government Projects

**Audience:** Nava PBC executive leadership
**Prepared:** 27 October 2026
**Status:** Evidence from seven supervised pilot runs against the MA PFML accessibility backlog

---

## Summary

Nava's AI-Enabled Delivery charter already names the Agentic Development Platform as one of six systems expected to make delivery measurably faster, higher quality, and more consistent. This document reports what ADP does, what it does not do, what we measured when we ran it against a client backlog, and why the next step should be putting it on engineer workstations rather than into a client environment.

The argument has three parts. First, ADP produces a cryptographically verifiable record of who changed what, under which model version, under which policy, that no competing option produces. Second, it reduces risk by refusing work rather than improvising, which we observed three separate times in seven runs. Third, the fastest way to find out whether it earns a place in client delivery is to run it locally, where the trust boundary is already ours and no procurement decision is required.

We are not asking for a platform commitment. We are asking for engineer time on a local pilot and a decision on whether to pursue a client deployment after that pilot reports.

---

## The problem, with a denominator

The MA PFML backlog carries 132 open tickets. 105 of them are accessibility defects. The distribution is bimodal. A large group are single-element corrections, a redundant `size` attribute on a form control, a heading level that breaks document outline, a missing label association. The remainder require design judgment and cannot be delegated to anything, human or otherwise, without a conversation first.

Work in the shallow mode is the problem worth solving. It is individually trivial, collectively large, and competes for the attention of engineers who should be working on determination logic and claimant experience. It also does not get done, because it never outranks anything else in a sprint. A backlog of 105 accessibility defects on a benefits system is a civic problem before it is an engineering one. Every unlabeled control is a claimant using a screen reader who cannot complete an application without help.

At five engineers clearing two tickets per week, that backlog closes in roughly ten weeks of partial attention. The question is whether that attention is the best use of five senior engineers, and whether there is a way to clear the shallow mode without spending it.

---

## What ADP is, and what it is not

ADP's value comes from the boundary it draws, so the boundary is worth stating before the benefits.

**ADP executes well-specified tickets.** It takes a ticket that declares its intent, its affected files, and the commands that verify it, and it carries that ticket from intake through planning, implementation, checks, repair, and a draft pull request. Every stage is gated. Every transition is recorded.

**ADP does not clarify ambiguous tickets.** It assumes the incoming ticket is scoped and defined for execution. It does not interview a product owner, resolve conflicting acceptance criteria, or decide what the requirement should have said. When a ticket is underspecified, ADP stops and escalates rather than inventing an interpretation. This is a scope decision, not a limitation we intend to close. Ticket refinement is judgment work that belongs with the people accountable for the product, and a system that guessed at intent would be more dangerous than one that declines.

**ADP does not supply its own testing.** It does not bring an accessibility test harness, it does not add Axe, it does not install a linter the project did not choose. It runs the test and check commands the target project already configures, and it treats their results as the definition of done. If a project has Axe wired into its suite, ADP runs Axe and must satisfy it. If a project has no accessibility testing, ADP will not manufacture any. This is also a scope decision. Test strategy encodes a team's standards and risk tolerance. A platform that imposed its own would be overriding the people who own the codebase, and would produce a pass signal that means nothing in the project's own terms.

The consequence of both decisions is that **ADP's output quality is bounded by the quality of the ticket and the strength of the project's test suite.** We consider that correct. It keeps accountability with the team, it makes ADP's behavior predictable, and it means improving tickets and improving tests improves ADP's results without changing ADP.

What ADP contributes inside that boundary is execution, enforcement, and evidence. It does the work, it refuses to exceed the declared scope, and it leaves a record that survives audit.

---

## What we measured

We ran seven supervised executions against tickets drawn from the MA PFML backlog. The pilot was not a demonstration. It was a search for defects, and it found them.

**Four platform defects surfaced and were fixed during the pilot.** A repair loop that reverted the implementation it was meant to repair. A failure summary filter that starved the repair agent of the output it needed, passing it 849 characters of a 5,177 character failure. A missing guard that let an out-of-scope repair be silently dropped instead of escalated. A container CPU allocation that starved the check pod and produced timeouts that looked like test failures. Each was found by investigation, fixed at the root cause, and covered by a regression test. We report these because a pilot that found nothing would have told us only that we had not looked.

**Run 7 completed without intervention.** A single-file accessibility correction moved from intake through plan approval, implementation, branch sync, and checks in 27 minutes, emitting 14 audit events, passing the project's test suite on the first attempt with zero repair cycles. The resulting diff was one line. The redundant attribute was removed and the heading level was preserved, which is the correct fix and not the one a naive pattern match would produce.

**Three of seven runs ended in a refusal, and all three refusals were correct.** This is the result we consider most informative, and it is covered in the next section.

---

## Auditability

Federal and state delivery contracts are increasingly explicit that AI-assisted code must be attributable. The honest state of the market is that no vendor in this category has a FedRAMP authorization covering an AI coding agent. Factory does not appear in the FedRAMP Marketplace in any capacity, verified against GSA's own dataset of 656 products. GitHub holds exactly one listing, GitHub Enterprise Cloud at the Tailored LI-SaaS Low baseline, authorized in October 2018, and no Copilot product appears inside it. We should not tell a client that any option in this space is FedRAMP authorized, because none is.

What differs is what each option can prove about its own output.

ADP signs authorship claims as DSSE envelopes against a trust root, and the merge gate verifies them fail-closed. A commit whose authorship claim does not verify does not merge. Every audit event pins the exact dated model version, for example `bedrock:us.anthropic.claude-sonnet-4-5-20250929-v1:0`, alongside the prompt version, the policy version, and content hashes for the artifact, the attestation, and the diff. Every execution pins the repository SHA, the policy bundle version, and the prompt versions in force when it ran. There are 26 distinct event types. A repair-heavy run emits up to 23 events.

The practical test is this. Eighteen months after delivery, an auditor asks which model wrote a specific line, under what instructions, against which policy, and who approved it. ADP answers from its own database. Factory has no commit signing, no cryptographic attestation, and no tamper-evident log anywhere in its documentation, and audit log export is a sales conversation rather than a product feature. Copilot's cloud agent answers well, with signed commits marked Verified, a co-author attribution to the requesting developer, and a session log link in the commit message. Copilot CLI, which is the surface most engineers would use, answers poorly. Its commits are authored by the human and signed only by local git configuration, and GitHub states plainly that the audit log does not include prompts sent locally and that a custom solution is required. Copilot CLI sessions sync to the user's personal account by default and are not administrator accessible, which puts the data exposure on the organization without giving it the audit benefit.

ADP's gate layer parses SARIF 2.1.0, so any scanner a client already runs, Semgrep, Snyk, CodeQL, Trivy, becomes a merge gate without bespoke integration work. We adopt the client's security posture rather than asking them to adopt ours.

---

## Risk reduction

The failure mode that should worry us is not an agent that writes a bad line. Review catches that. It is an agent that widens its own scope without saying so, edits shared infrastructure to make a test pass, and produces a large plausible diff that a reviewer approves under time pressure.

ADP treats the ticket's declared file impact as an enforced commit boundary rather than a suggestion. In run 5 the agent correctly diagnosed that the root cause of a failure was a shared test helper, and was blocked from editing it. It escalated in a single cycle instead of expanding its scope or spending its remaining budget. That is the behavior we want. The diagnosis was right, the fix was out of bounds, and a human was told why.

In run 6 the platform detected that the ticket already had a draft pull request from prior work, declined to duplicate it, and settled the work unit. The run terminated in 90 seconds. The fault was in our pilot selection, not the platform, and the platform caught our mistake.

Repair is budgeted. When a check fails, ADP attempts a bounded number of repair cycles and then escalates with the failure attached. It does not iterate until something passes.

The comparison here is uncomfortable for the alternatives. Copilot's cloud agent has a strong review chain, with draft-only pull requests, branch protections enforced, workflow runs requiring human approval, and the requesting developer barred from approving the resulting pull request. Its weakness is that there is no self-hosted path at all, and that the agent clones the full repository with no confirmed content exclusion enforcement. Factory offers four autonomy levels and organizational ceilings, but its strongest deployment modes, Factory Private and OpenCompute, are in Private Preview while being marketed as on-premise options, and the control plane stays Factory-hosted by default. A human engineer has no enforced scope boundary at all, which we accept because we can ask them why.

We should also weigh vendor durability, which is not usually our concern but is here. Factory's CEO publicly accused a board advisor of passing information to a competitor in September 2026, the advisor disputed the account and joined that competitor, and in October the firm that led Factory's Series C publicly disparaged the company. The competitor named in that dispute carries a valuation roughly ten times Factory's. Building client delivery process on a vendor in that position is a risk we would have to disclose.

---

## Reliability, stated honestly

Run 7 took 27 minutes end to end with no repair cycles. A run that needs repair takes roughly 35 minutes per cycle. ADP is sensitive to host capacity, and we learned that the operative constraint is CPU per test worker rather than total CPU, which took a controlled experiment to establish.

ADP will not rescue a fragile test suite in the target repository. We found a shared test helper in the MA PFML portal whose timing behavior makes several suites unreliable under load, and we filed it as a defect in that project rather than working around it in the platform. When the project's tests are flaky, ADP's results are flaky, because the project's tests are the standard it is measured against. That follows directly from the scope decision above, and the remedy is in the project, not the platform.

ADP's success rate is a function of ticket quality. Our one clean failure of the pilot traced to a ticket whose declared file impact was wrong. The platform did exactly what it should have done, which was refuse to edit a file the ticket had not declared, and the ticket was at fault.

We would be misrepresenting the pilot if we offered a success percentage from seven runs. What we can say is that every refusal was correct, every defect we found was in the platform rather than in the model's judgment, and the one run with a well-formed ticket and a stable test surface completed unattended and correctly.

---

## Cost

The arithmetic that matters is not the cost per run. It is whose time is consumed and what it is displaced from.

A single-element accessibility correction occupies a senior engineer for perhaps twenty minutes of work and a context switch that costs more than the work. ADP's run 7 consumed 27 minutes of wall clock and approximately four minutes of human attention, for ticket approval and pull request review. The engineer does something else during the other 23 minutes.

Model inference runs through Amazon Bedrock on our own account, using the same Anthropic Claude models the client had already approved for Copilot, so there is no incremental model approval and no vendor margin on inference. There is no per-seat licence. Factory publishes $20, $100, and $200 per month tiers with Teams at $60 plus $40 per seat, with three rolling rate limit windows and model multipliers ranging from 0.04 to 12, and its highest-engagement public discussion anywhere is a 295-upvote complaint about billing opacity and cancellation. Copilot carries per-seat Business or Enterprise pricing, and the policy that restricts it to FedRAMP-certified model endpoints is disabled by default, costs an additional 10 percent in AI credits, and is available only to US data-residency customers.

The durable cost argument is lock-in. ADP is ours. The models are swappable because the model identifier is configuration. The gates are the client's own scanners through a standard format. If we stop using ADP tomorrow, the client keeps their repository, their tests, their pull requests, and their audit database. We can make that statement to a client and support it, which we cannot do on either vendor's behalf.

---

## The comparison in one view

| | **ADP** | **Factory** | **Copilot CLI** | **Copilot cloud agent** | **Human engineer** |
|---|---|---|---|---|---|
| Where it runs | Engineer workstation or client enclave | SaaS control plane, agent local | Workstation | GitHub-hosted Actions | Anywhere |
| Signed agent attestation | DSSE, fail-closed merge gate | None found | None | Signed commits, session link | Commit signature only |
| Model and prompt version pinned per event | Yes | No | No | No | n/a |
| Enforced scope boundary | Ticket file impact | Permission rules | Heuristic, not guaranteed | Branch protections | None |
| FedRAMP covering the agent | No | No | No | No | n/a |
| Local prompt logging | In our database | Opt-in, unredacted, your collector | Not captured | n/a | n/a |
| Self-hosted | Yes | Preview or contract only | GHES air-gap in technical preview | No | n/a |
| Handles ambiguous tickets | No, escalates | Attempts | Attempts | Attempts | Yes |
| Supplies its own tests | No, runs the project's | No | No | No | Yes |
| Vendor dependency | Bedrock only | Factory | GitHub | GitHub | None |

The row that decides this for government work is the second one. Attribution that survives an audit is the thing we can offer that no one else in this comparison offers, and it is the thing our clients will be asked for.

---

## Why local, and why now

We propose that the next phase runs entirely on engineer workstations, against repositories we already hold. There are two reasons, and they persuade two different audiences.

### The organizational reason

Running ADP locally requires no client procurement, no new data path, no security review of a vendor, and no contract. The trust boundary is already ours. The only outbound connections are to Amazon Bedrock on our own account and to the client environments the engineer can already reach. Nothing about the deployment is novel from a compliance standpoint, which means we can gather evidence now and have that evidence in hand before anyone asks a client to approve anything.

This is also the honest sequence. We should not propose a client deployment of a platform we have run seven times. A quarter of local use across a handful of engineers produces the sample we would need to make a credible claim about reliability, and it produces it at no cost to a client relationship if the answer turns out to be no.

### The engineer's reason

Engineers will not run this to help us prove out a technology. They will run it if it makes their week better. The case to them is narrow and concrete.

**It takes the tickets you keep deferring.** The 105 accessibility defects in the PFML backlog are not interesting. They are a redundant attribute, a heading level, a missing label. Each one costs you a context switch worth more than the fix. ADP takes the ticket and gives you back a draft pull request.

**You keep the decision.** It opens a draft. It cannot approve, it cannot merge, and it cannot push anywhere except its own branch. Reviewing its output is the same review you already do, and it is a shorter one, because of the next point.

**The diff is bounded by the ticket.** ADP cannot edit a file the ticket did not declare. You are never reviewing a 40-file pull request that started as a one-line fix. When it decides that the correct fix lies outside its declared scope, it stops and tells you where, which is a useful diagnosis delivered for free.

**It runs on your machine, with your setup.** Your credentials, your git configuration, your network access to the client environment, your project's test commands. There is nothing to request and no account to create.

**It is measured by your tests, not by its own opinion.** It does not declare success. Your suite does. If your project runs Axe, ADP has to satisfy Axe. If your project's checks pass, the work meets the standard your team set.

**When it cannot do the job it says so, with the reason.** It does not keep trying until something sticks. Repair attempts are budgeted. The failure comes back to you with the output attached and the specific thing it could not resolve.

**It works while you do something else.** Run 7 took 27 minutes of wall clock and about four minutes of attention.

The honest pitch to an engineer is not that ADP writes code they could not write. It is that it clears the work they should not be spending attention on, within a boundary they control, and it stops instead of sprawling when it reaches the edge of what its ticket authorized.

---

## Why not a simpler solution

The simpler solutions deserve a direct answer, because our own practice asks for one.

**Why not assign the backlog to engineers?** That is the current plan and it has not happened. The work does not outrank anything in a sprint and it will not start doing so.

**Why not use Copilot CLI, which we already have?** For many tasks we should, and this proposal does not displace it. For client delivery under audit obligations it has a specific gap. GitHub states that the audit log does not capture local prompts and that a custom solution is required, and that not all enterprise AI controls apply to Copilot CLI. Pull requests it creates are authored by the human, so they carry none of the agent-specific controls that make the cloud agent defensible. If we need to evidence what an agent did on a government codebase, Copilot CLI cannot produce that evidence and ADP already does.

**Why not buy Factory?** Their strongest capability is deployment topology, and their own best customer reference bought them for data center control rather than coding quality. The deployment modes that would matter to a government client are in Private Preview, their benchmark claims are 28 months old and unverified, there is no cryptographic attestation anywhere in their product, and the company is in a public dispute with its own lead investor. We would be taking on vendor risk to acquire a capability we have.

**Why not wait for a FedRAMP authorized option?** None exists in this category from any vendor, and when one arrives it will not produce signed attestation of agent authorship unless someone builds it. Waiting does not reduce risk, it defers evidence gathering.

---

## Staff impact

This should be said plainly rather than left to inference. ADP does not reduce headcount and the proposal does not contemplate it. It executes tickets that a person has already specified and approved, within a scope that person declared, and every output requires human review before it merges. It cannot decide what to build, it cannot resolve an ambiguous requirement, and it cannot judge whether a fix is the right fix for a claimant. Its design deliberately refuses that work.

The intended effect is redirecting senior attention from mechanical correction toward determination logic, claimant experience, and the accessibility defects that require design judgment and therefore cannot be delegated at all. If the local pilot shows that ADP displaces work rather than redirecting it, that finding should change the proposal, and we should say so to the bargaining unit before it is discovered rather than after.

---

## What would falsify this argument

We should state in advance what results would mean we are wrong.

If, across a quarter of local use, ADP's refusals turn out to be mostly incorrect, meaning it stops on work it should have completed, then the scope enforcement is too tight to be useful and the value proposition fails. If reviewers find themselves spending as long reviewing an ADP pull request as writing the fix, the time argument fails. If the audit record turns out not to answer the questions a client auditor asks, the differentiator fails and we should stop claiming it. If the platform defect rate stays at the level we saw in the pilot, four defects in seven runs, then it is not ready for a client environment regardless of its other properties.

Each of those is measurable from the local pilot. None of them requires a client to take a risk to find out.

---

## The ask

1. **Authorize a local pilot.** Five engineers, one quarter, ADP on their own workstations against repositories we already hold. No client deployment and no procurement.
2. **Agree the measures now.** Refusal correctness, review time per pull request, platform defect rate, and whether the audit record answers a drafted auditor question set. Agreeing them before we start is what separates a pilot from a demonstration.
3. **Decide on client deployment when that pilot reports,** not before.

The AI-Enabled Delivery charter already names ADP. This is the cheapest path to finding out whether that naming was right.

---

## Appendix: sourcing and confidence

Claims about ADP's behavior come from the pilot's own audit database and from the platform source, and each is traceable to an execution identifier or a file path. Claims about Factory and GitHub come from vendor documentation, GSA's FedRAMP dataset exported 27 October 2026, and public reporting, and are cited as vendor-asserted where that is what they are.

Two limitations should travel with this document. Both vendors' trust centers are client-rendered applications whose certification claims could not be independently verified, which is a symmetric limitation and not a mark against either. And seven runs is a small sample that supports qualitative findings about platform behavior but does not support a reliability rate. The local pilot is designed to produce one.
