# The Agentic Development Platform on Government Projects

**Prepared:** October 2026
**Revised:** November 2026
**Status:** Aggregate evidence from six weeks of piloting against an accessibility backlog in a government service, autumn 2026. The platform description reflects version 0.3.0.

---

## Summary

Nava's AI-Enabled Delivery charter already names the Agentic Development Platform as one of six systems expected to make delivery measurably faster, higher quality, and more consistent. This document reports what ADP does, what it does not do, what six weeks of piloting against a client backlog measured, and how it compares to the other agentic coding options available to us.

**The bottom line.** Over six weeks against the pilot codebase, ADP produced 11 pull requests across 9 tickets. None exceeded the file set its ticket declared and the largest was 7 files. Every one of the 23 defects the pilot surfaced was in the platform rather than in the agent's judgement on a well-specified ticket. ADP works inside a deliberately narrow scope, it holds that scope under enforcement rather than instruction, and it leaves an audit record no competing option produces.

The pilot ran on the workstation build of ADP. Version 0.3.0 adds a FedRAMP-aligned AWS deployment that several teams can share, and the sections below describe it alongside the pilot evidence.

Three findings carry the weight. First, ADP signs every review verdict and, on the rebar ticket backend, every plan and code change, and it records the model, prompt, and policy versions in force when it ran, which no competing option produces. Second, it reduces risk by refusing work rather than improvising, and the refusals we observed were correct. Third, it runs on infrastructure that Nava or the client controls, either an engineer workstation or a FedRAMP-aligned AWS deployment, so the trust boundary stays within existing controls.

This document records the evidence rather than proposing a course of action. It sets out what the platform does, what the pilot measured, and where ADP sits against Factory, Copilot CLI, the Copilot cloud agent, and a human engineer, so that anyone weighing those options has the same material in front of them.

---

## The problem, with a denominator

The pilot backlog carries roughly 130 open tickets, of which the large majority are accessibility defects. The distribution is bimodal. A large group are single-element corrections, a redundant `size` attribute on a form control, a heading level that breaks document outline, a missing label association. The remainder require design judgment and cannot be delegated to anything, human or otherwise, without a conversation first.

Work in the shallow mode is the problem worth solving. It is individually trivial, collectively large, and competes for the attention of engineers who should be working on core product capability and the experience of the people the service serves. It also does not get done, because it never outranks anything else in a sprint. An accessibility backlog of that size on a public service is a civic problem before it is an engineering one. Every unlabeled control is someone using a screen reader who cannot complete a task without help.

At the rate a small team clears this work alongside its primary commitments, a backlog of that size takes a quarter of partial attention to close. The question is whether that attention is the best use of senior engineers, and whether there is a way to clear the shallow mode without spending it.

---

## What ADP is, and what it is not

ADP's value comes from the boundary it draws, so the boundary is worth stating before the benefits.

**ADP executes well-specified tickets.** It takes a ticket that states its intent and, on the rebar ticket backend, the files it affects, and it carries that ticket from intake through planning, implementation, checks, repair, and a draft pull request. The commands that verify the change come from the project's checks configuration or, when the project has none, from the runtime profile the repository selects. Every stage is gated. Every transition is recorded.

**ADP does not clarify ambiguous tickets.** It assumes the incoming ticket is scoped and defined for execution. It does not interview a product owner, resolve conflicting acceptance criteria, or decide what the requirement should have said. ADP has no deterministic check of ticket completeness. An AI plan review judges every plan before implementation starts, and when it cannot approve the plan the run stops before any code is written and asks for a clearer description or acceptance criteria. This is a scope decision, not a limitation we intend to close. Ticket refinement is judgment work that belongs with the people accountable for the product, and a system that guessed at intent would be more dangerous than one that declines.

**ADP runs the project's checks and adds platform gates.** ADP does not bring an accessibility test harness and does not add Axe to a project's suite. It runs the test and check commands the project configures, and it treats their results as the definition of done. If a project has Axe wired into its suite, ADP runs Axe and must satisfy it. A repository that selects a runtime profile and has no checks of its own runs the profile's default command, such as `pytest` for a Python repository or `mvn verify` for a Java repository. Every run also passes platform gates that a project's suite does not supply, namely an AI code review, an AI security review, an AI review of the change against the ticket's acceptance criteria, and the Semgrep and Trivy scanners. Each team can change those gates in configuration. Test strategy encodes a team's standards and risk tolerance, so the platform gates add to the project's checks and never replace them.

The consequence of both decisions is that **ADP's output quality is bounded by the quality of the ticket and the strength of the project's test suite.** We consider that correct. It keeps accountability with the team, it makes ADP's behavior predictable, and it means improving tickets and improving tests improves ADP's results without changing ADP.

What ADP contributes inside that boundary is execution, enforcement, and evidence. It does the work, it refuses to exceed the declared scope, and it leaves a record that survives audit.

---

## What we measured

ADP has run against the pilot codebase over six weeks in autumn 2026, on the workstation build that preceded version 0.3.0. The aggregate is below. These are the numbers to carry forward, not any single run.

| Measure | Result |
|---|---|
| Period | Six weeks, autumn 2026 |
| Platform build | Workstation build, before version 0.3.0 |
| Pull requests produced | 11 |
| Distinct tickets carried to a pull request | 9 |
| Median pull request size | 2 files |
| Largest pull request | 7 files |
| Pull requests exceeding the ticket's declared scope | 0 |
| Pull requests reviewed by a human | 4, all approved |
| Pull requests merged | 0, merging was out of scope for the pilot |
| Platform defects found and fixed | 23 |

Four findings matter more than the totals.

**The execution denominator, and why it is not a success rate.** The audit store covers the last week of the period, because a schema rebuild during hardening dropped the earlier events. Within that week it holds 15 executions across 6 work units. Two completed, one failed, one is awaiting human input, and eleven were cancelled.

Those 15 are not 15 independent attempts. Seven of them sit on a single ticket and two further tickets carry retry pairs. That clustering is the visible shape of a defect we have since fixed, in which the work claim was released only on the human-intervention path and was addressed to the implementation child workflow rather than the parent that holds the claim. No outcome released anything, the work unit stayed locked until an operator edited the table, and re-running the ticket was the workaround. The executions that ended in cancellation are mostly that defect rather than the agent failing at its task.

This is the better reason for offering no success percentage, and the small sample is the lesser one. The platform changed twelve times inside these six weeks. A completion rate computed across that window describes the hardening rather than the approach. A reliability rate needs a frozen platform version and a run of tickets against it, and neither exists yet.

**Scope held in every case.** Every pilot ticket declared its file set. Across 11 pull requests, none exceeded it, and the largest was 7 files. The boundary is enforced rather than advisory, so a one-line ticket cannot become a sprawling diff. This is the property that makes review cheap, and it is the one we would most want a client to test.

**The platform was the thing that failed, not the model's judgement.** All 23 defects were in ADP itself, in orchestration, state handling, container configuration, and diagnostic reporting. We did not find a case where the agent misunderstood a well-specified accessibility ticket and produced a wrong fix. That distinction matters, because platform defects are ours to fix once, while judgement failures would be a recurring risk we could not engineer away.

**The defect discovery rate is falling.** 12 of the 23 fixes landed in a two-day window at the start of piloting. The remainder arrived in a thinning tail over the following five weeks. The most recent execution completed with zero human intervention events, zero repair cycles, a passing check run, and a correct one-line diff, which became the final pull request of the period. We are not claiming the curve has reached zero. We are reporting that it is declining and that the remaining failures are narrower each time.

Four of the 11 pull requests drew human review and all four were approved. Those approvals fell in the first week of the period, and nothing produced after the end of that week has been reviewed, so the reviewed sample predates most of the hardening described above. None of the 11 merged. The pilot ran against a non-delivery environment and merging was never the objective. Approval is the strongest signal available in this setting and it is a weaker one than a merge.

The bottom line is that ADP produces correctly scoped, reviewable changes against well-specified tickets, and that six weeks of piloting has been spent hardening the platform rather than correcting its output. No measured batch of tickets has run on version 0.3.0, so every figure in this section describes the workstation build.

---

## Auditability

Federal and state delivery contracts are increasingly explicit that AI-assisted code must be attributable. The honest state of the market is that no vendor in this category has a FedRAMP authorization covering an AI coding agent. Factory does not appear in the FedRAMP Marketplace in any capacity, verified against GSA's own dataset of 656 products. GitHub holds exactly one listing, GitHub Enterprise Cloud at the Tailored LI-SaaS Low baseline, authorized in October 2018, and no Copilot product appears inside it. We should not tell a client that any option in this space is FedRAMP authorized, because none is.

What differs is what each option can prove about its own output.

ADP signs two kinds of record. Every review verdict, from the plan review through the acceptance review, is signed with SSHSIG inside a DSSE envelope and verified against a public trust root, so a verifier holds only public keys and cannot forge a verdict. A review stage refuses to run when signing is enabled and its key or trust root is missing, so a verdict never falls back to a shared-secret signature. On the rebar ticket backend with an authoring identity configured, ADP also signs the plan and the code change as authorship claims. Before it marks a pull request ready for review, it verifies both claims and stops the run if either is missing or does not verify. ADP never merges. A person does. Every execution pins the repository SHA, the policy bundle version, the prompt versions, and the models in force when it ran. The pilot store recorded 26 distinct event types, and a repair-heavy run emitted 23 of them. The platform now defines at least 43.

Per-event pinning is partial, and the measured coverage belongs in this document rather than a rounded-up version of it. Across the 181 events in the pilot store, 165 carry content hashes for the artifact, the attestation, and the diff. 49 carry a prompt version, 42 carry a model identifier, and 15 carry a policy version. Where a model is named it is the exact dated version, for example `bedrock:us.anthropic.claude-sonnet-4-5-20250929-v1:0`, and where it is not the event falls back to a stage label such as `planning-model`. Carrying all four identifiers on every event is the open work on this differentiator rather than a property we already hold.

The store has a retention boundary as well. In the pilot it ran beside the platform on the engineer's workstation and held events from 20 October onward, because a schema rebuild during hardening dropped what came before it. A record that lives on one developer's machine cannot answer a question asked eighteen months after delivery. In the version 0.3.0 AWS deployment the audit database runs in Amazon Aurora, encrypted and backed up. The platform also includes a write-once evidence store on S3 Object Lock in compliance mode, with a default retention of seven years and retention set per audit classification. The services write to it when a deployment supplies its bucket, and the example AWS composition does not provision that bucket by default.

The practical test is this. Eighteen months after delivery, an auditor asks which model wrote a specific line, under what instructions, against which policy, and who approved it. ADP answers from its own database, within the coverage and retention limits above, and it is the only option here whose answer improves when we decide it should. Factory has no commit signing, no cryptographic attestation, and no tamper-evident log anywhere in its documentation, and audit log export is a sales conversation rather than a product feature. Copilot's cloud agent answers well, with signed commits marked Verified, a co-author attribution to the requesting developer, and a session log link in the commit message. Copilot CLI, which is the surface most engineers would use, answers poorly. Its commits are authored by the human and signed only by local git configuration, and GitHub states plainly that the audit log does not include prompts sent locally and that a custom solution is required. Copilot CLI sessions sync to the user's personal account by default and are not administrator accessible, which puts the data exposure on the organization without giving it the audit benefit.

ADP's gate layer parses SARIF 2.1.0, so any scanner a client already runs, Semgrep, Snyk, CodeQL, Trivy, becomes a blocking gate without bespoke integration work. We adopt the client's security posture rather than asking them to adopt ours.

Version 0.3.0 also secures what the platform itself runs. Every platform image is built on a CIS-hardened Amazon Linux 2023 base and scanned with Amazon Inspector before it is pushed. An image with an unaccepted finding at high severity or above, or one that fails the CIS benchmark, is not pushed. A daily build of the newest release rebuilds and rescans every image, and the platform's images are signed with AWS Signer. Inside the cluster, an admission policy in each team's sandbox namespace admits only images from approved registries pinned by digest, and a deployment can also refuse any image without a clean scan or a valid signature. The Temporal server, admin tools, and UI are built with the FIPS 140-3 validated Go Cryptographic Module, CMVP certificate 5247.

---

## Risk reduction

The failure mode that should worry us is not an agent that writes a bad line. Review catches that. It is an agent that widens its own scope without saying so, edits shared infrastructure to make a test pass, and produces a large plausible diff that a reviewer approves under time pressure.

When a ticket declares its file impact, ADP treats it as an enforced commit boundary rather than a suggestion. A ticket without declared paths, including any Jira ticket, has no file boundary, and its run relies on the review gates and human review. Every pilot ticket declared its file set, and across 11 pull requests no diff exceeded it.

The clearest evidence comes from a run that did not succeed. The execution diagnosed the root cause of a failing check as a shared test helper outside its declared scope. The commit layer dropped the out-of-scope changes and named both the helper and the test beside it in the audit record. Two repair cycles later the budget was exhausted, and the run escalated to a human with the diagnosis attached. It produced no pull request and it cost the ticket six further attempts before we fixed the platform defects behind them. The diagnosis was right, the fix was out of bounds, and a person was told why.

That is a boundary holding in a run that was failing, which is the case that matters. An agent making progress has no reason to widen its scope. An agent that cannot pass a check is the one that does.

The platform also refuses work it should not take. It detects when a ticket already carries a pull request from prior work and settles the work unit rather than duplicating it, terminating in well under two minutes.

Repair is budgeted, at five attempts by default. When a check fails, ADP attempts a bounded number of repair cycles and then escalates with the failure attached. It does not iterate until something passes.

Version 0.3.0 adds further limits. Any gate can be configured to pause a run for a person rather than fail it, and a plan checkpoint can require human approval before implementation starts. Each run's implementation is capped at $5, 2 million tokens, and 200 model calls by default, and each run's model key carries its own spending limit. Each team's sandboxes run in their own namespace and cannot pull another team's images. Sandbox and gate pods that an earlier attempt left behind are deleted rather than left running.

The comparison here is uncomfortable for the alternatives. Copilot's cloud agent has a strong review chain, with draft-only pull requests, branch protections enforced, workflow runs requiring human approval, and the requesting developer barred from approving the resulting pull request. Its weakness is that there is no self-hosted path at all, and that the agent clones the full repository with no confirmed content exclusion enforcement. Factory offers four autonomy levels and organizational ceilings, but its strongest deployment modes, Factory Private and OpenCompute, are in Private Preview while being marketed as on-premise options, and the control plane stays Factory-hosted by default. A human engineer has no enforced scope boundary at all, which we accept because we can ask them why.

We should also weigh vendor durability, which is not usually our concern but is here. Factory has been the subject of public reporting in late 2026 concerning disputes with an advisor and with an investor. We have not independently verified those accounts, and we note them only because building client delivery process on a venture-stage vendor carries a continuity risk we would have to disclose.

---

## Reliability, stated honestly

In the pilot, a clean execution took roughly 40 to 45 minutes end to end on an engineer workstation, of which the project's own security scan can be 15 minutes or more on a large repository. A run that needs repair adds roughly 35 minutes per cycle. Human attention across a clean run is about four minutes, for ticket approval and pull request review. ADP is sensitive to host capacity, and the operative constraint is CPU per test worker rather than total CPU, which took a controlled experiment to establish.

ADP will not rescue a fragile test suite in the target repository. During piloting we found a shared test helper whose timing behavior made several suites unreliable under load. We reported it to the owning team rather than working around it in the platform. When the project's tests are flaky, ADP's results are flaky, because the project's tests are the standard it is measured against. That follows from the principle above that the project's checks define done, and the remedy is in the project, not the platform.

ADP's success rate is a function of ticket quality. The clearest failure of the pilot traced to a ticket whose declared file impact was wrong. The platform did what it should have done, which was refuse to edit a file the ticket had not declared, and the ticket was at fault.

We are not offering a success percentage, and the sample size is the lesser of the two reasons. The platform changed twelve times during these six weeks, so there is no fixed version for a rate to describe. 11 pull requests is enough to show that scope enforcement holds and that the output is correctly shaped. A reliability rate needs a frozen platform version and a run of tickets against it.

Version 0.3.0 adds recovery from a lost worker, a heartbeat on every activity, and cleanup of the sandbox and gate pods an earlier attempt left. The AWS deployment is provisioned in a sandbox account, and no full ticket run on it has been timed.

---

## Cost

The arithmetic that matters is not the cost per run. It is whose time is consumed and what it is displaced from.

A single-element accessibility correction occupies a senior engineer for perhaps twenty minutes of work and a context switch that costs more than the work. In the pilot, an ADP execution consumed roughly 40 minutes of wall clock and about four minutes of human attention. The engineer does something else during the remainder.

The machine time has a price and it is small. The two completed executions in the audit store cost $1.59 and $1.18 in Bedrock inference, against 1.0 to 1.8 million tokens each. The ticket that took seven executions and produced nothing cost about $4.74 across all of them, which is a fair upper bound on what a run that goes wrong costs us.

Scaled against the backlog, a backlog of roughly a hundred accessibility tickets at four minutes of attention each is about seven hours of senior engineer time, against the roughly 35 hours the same tickets would take to write by hand, before counting context switching. The saving is not the headline. The headline is that work which has not been getting done at all becomes work that gets done.

Model inference runs through Amazon Bedrock on our own account, using Anthropic Claude models under Nava's existing Bedrock agreement, so there is no vendor margin on inference. There is no per-seat licence. Each activity uses its own model, set in configuration. Planning, security review, and evaluation use Claude Opus, coding and code review use Claude Sonnet, and classification uses Claude Haiku, so routine steps run on lower-cost models. Spend is capped per run, and a team can be given a monthly model budget. Factory publishes $20, $100, and $200 per month tiers with Teams at $60 plus $40 per seat, with three rolling rate limit windows and model multipliers ranging from 0.04 to 12. Copilot carries per-seat Business or Enterprise pricing, and the policy that restricts it to FedRAMP-certified model endpoints is disabled by default, costs an additional 10 percent in AI credits, and is available only to US data-residency customers.

The AWS deployment carries a standing infrastructure cost that the workstation mode does not. Off-hours scale-down reduces it by stopping the platform services and Temporal and letting the Aurora database pause.

The durable cost argument is lock-in. ADP is ours. The models for planning, coding, and classification are swappable because the model identifier is configuration. The review gates read a fixed table of Bedrock models, so changing their model is a small code change. Scanner gates can be the client's own scanners through a standard format. If we stop using ADP tomorrow, the client keeps their repository, their tests, their pull requests, and their audit database. We can make that statement to a client and support it, which we cannot do on either vendor's behalf.

---

## The comparison in one view

| | **ADP** | **Factory** | **Copilot CLI** | **Copilot cloud agent** | **Human engineer** |
|---|---|---|---|---|---|
| Where it runs | Engineer workstation, or a FedRAMP-aligned AWS deployment on EKS and ECS | SaaS control plane, agent local | Workstation | GitHub-hosted Actions | Anywhere |
| Signed agent attestation | Signed review verdicts on every run. Signed plan and code authorship on the rebar backend, verified before the pull request is marked ready | None found | None | Signed commits, session link | Commit signature only |
| Model and prompt version pinned per event | Partial, see Auditability | No | No | No | n/a |
| Enforced scope boundary | File impact a rebar ticket declares | Permission rules | Heuristic, not guaranteed | Branch protections | None |
| FedRAMP covering the agent | No. Version 0.3.0 is built to FedRAMP Medium technical controls | No | No | No | n/a |
| Local prompt logging | In our database | Opt-in, unredacted, your collector | Not captured | n/a | n/a |
| Self-hosted | Yes, on a workstation or in Nava's or the client's AWS account | Preview or contract only | GHES air-gap in technical preview | No | n/a |
| Handles ambiguous tickets | No, plan review rejects and escalates | Attempts | Attempts | Attempts | Yes |
| Supplies its own tests | Runs the project's checks, with runtime profile defaults and platform security gates | No | No | No | Yes |
| Spend control | Per-run caps on cost, tokens, and model calls | Plan tiers and rate limits | Per-seat pricing | Per-seat pricing | n/a |
| Image supply chain | Hardened, scanned, and signed images, FIPS-validated Temporal | Not assessed | Not assessed | Not assessed | n/a |
| Vendor dependency | Amazon Bedrock, with a model chosen per activity | Factory | GitHub | GitHub | None |

The row that decides this for government work is the second one. Attribution that survives an audit is the thing we can offer that no one else in this comparison offers, and it is the thing our clients will be asked for.

---

## Where ADP runs

ADP runs in two modes, and the pilot used the first. Both run against repositories that Nava or the client already holds. The consequences differ for the organization and are the same for the engineer at the keyboard.

### On an engineer workstation

In the workstation mode ADP runs in Docker on an engineer's machine, and it introduces no third-party vendor into the delivery path. It runs inside Nava's existing, already-approved development environment, under the same access and data-handling controls that already govern Nava engineering work, including the engineer's existing authorised access and Nava's own Bedrock agreement. Any use against a client codebase remains subject to that engagement's agreed security and data-handling terms. Nothing about the deployment is novel from a compliance standpoint, so evidence accumulates without a client carrying any exposure.

It also puts the evidence in the right order. 11 pull requests support qualitative findings about scope enforcement and output shape. A reliability rate would need a larger sample across more engineers, and local use is where that sample accumulates at no cost to a client relationship if the answer turns out to be no.

### Hosted on AWS

Version 0.3.0 adds a hosted mode on AWS. It is built to the FedRAMP Medium technical controls the platform owns and inherits the controls AWS provides. ADP holds no FedRAMP authorization of its own. The coding agents and their sandboxes run on Amazon EKS, the remaining platform services run on Amazon ECS, and the Temporal workflow engine runs over TLS. Model calls reach Bedrock through private endpoints, name resolution is limited to an allowlist, data is encrypted with customer-managed keys, and database and cache credentials rotate in Secrets Manager. Several teams can share one deployment, each with its own worker, sandbox namespace, secrets, ticket store, configuration, and limits, and each repository selects a runtime profile for Python and Node, Java, or Go. LLM traces go to a self-hosted Langfuse, and platform metrics and traces go to Amazon Managed Service for Prometheus, Amazon Managed Grafana, and AWS X-Ray.

### What either mode means for the engineer

From an engineer's seat the behavior is narrow and concrete.

**It takes the tickets you keep deferring.** The accessibility defects in a backlog like that one are not interesting. They are a redundant attribute, a heading level, a missing label. Each one costs you a context switch worth more than the fix. ADP takes the ticket and gives you back a draft pull request.

**You keep the decision.** It opens a draft. It cannot approve, it cannot merge, and it cannot push anywhere except its own branch. It marks the pull request ready for review only after a person approves it. Reviewing its output is the same review you already do, and it is a shorter one, because of the next point.

**The diff is bounded by the ticket.** When the ticket declares its files, ADP cannot edit a file the ticket did not declare. You are never reviewing a 40-file pull request that started as a one-line fix. When it decides that the correct fix lies outside its declared scope, it stops and tells you where, which is a useful diagnosis delivered for free.

**It runs where your work already runs.** In the workstation mode it runs in Docker on your machine against repositories you can already reach, and setup needs Bedrock access and Jira or GitHub credentials. In the hosted mode you file the ticket and the platform does the rest.

**It is measured by your tests, not by its own opinion.** It does not declare success. Your suite does. If your project runs Axe, ADP has to satisfy Axe. If your project's checks pass, the work meets the standard your team set, and the platform gates add their own review on top.

**When it cannot do the job it says so, with the reason.** It does not keep trying until something sticks. Repair attempts are budgeted. The failure comes back to you with the output attached and the specific thing it could not resolve.

**It works while you do something else.** In the pilot, a clean execution took roughly 40 minutes of wall clock and about four minutes of your attention.

ADP does not write code an engineer could not write. It clears work that does not warrant their attention, within a boundary they control, and it stops instead of sprawling when it reaches the edge of what its ticket authorized.

---

## Why not a simpler solution

Each of the simpler options deserves a direct answer.

**Why not assign the backlog to engineers?** That is the current plan and it has not happened. The work does not outrank anything in a sprint and it will not start doing so.

**Why not use Copilot CLI, which we already have?** For many tasks we should, and ADP does not displace it. For client delivery under audit obligations it has a specific gap. GitHub states that the audit log does not capture local prompts and that a custom solution is required, and that not all enterprise AI controls apply to Copilot CLI. Pull requests it creates are authored by the human, so they carry none of the agent-specific controls that make the cloud agent defensible. If we need to evidence what an agent did on a government codebase, Copilot CLI cannot produce that evidence and ADP already does.

**Why not buy Factory?** Their strongest capability is deployment topology, and their published customer references emphasize that rather than coding quality. The deployment modes that would matter to a government client are in Private Preview, their benchmark claims are 28 months old and unverified, there is no cryptographic attestation anywhere in their product, and the company has been the subject of public reporting concerning investor disputes. We would be taking on vendor risk to acquire a capability we have.

**Why not wait for a FedRAMP authorized option?** None exists in this category from any vendor, and when one arrives it will not produce signed attestation of agent authorship unless someone builds it. ADP version 0.3.0 runs inside an AWS environment built to the FedRAMP Medium technical controls, without claiming an authorization. Waiting does not reduce risk, it defers evidence gathering.

---

## Staff impact

This should be stated plainly rather than left to inference. ADP does not reduce headcount. It executes tickets that a person has already specified and approved, within a scope that person declared, and every output requires human review before it merges. It cannot decide what to build, it cannot resolve an ambiguous requirement, and it cannot judge whether a fix is the right fix for the person it affects. Its design deliberately refuses that work.

The intended effect is redirecting senior attention from mechanical correction toward core product capability, the experience of the people the service serves, and the accessibility defects that require design judgment and therefore cannot be delegated at all. If extended use shows that ADP displaces work rather than redirecting it, that finding belongs in front of the bargaining unit before it is discovered rather than after.

---

## What would falsify these claims

The claims above are falsifiable, and these are the results that would falsify them.

If ADP's refusals turn out to be mostly incorrect, meaning it stops on work it should have completed, then the scope enforcement is too tight to be useful and the central claim fails. If reviewers find themselves spending as long reviewing an ADP pull request as writing the fix, the time claim fails. If the audit record turns out not to answer the questions a client auditor asks, the differentiator fails and we should stop stating it. If the platform defect rate stops declining, rather than continuing the fall from 12 fixes in the first two days to a thinning tail thereafter, then it is not ready for a client environment regardless of its other properties.

Each of those is measurable from continued use on Nava's own infrastructure, on workstations or in Nava's AWS deployment. None of them requires a client to carry a risk to find out.

---

## Where this leaves ADP

ADP is narrow by construction. It executes tickets that are already specified, inside a file boundary the ticket declares, measured by the checks the project already owns, and it records every step in a form an auditor can verify. The pilot evidence supports those statements. It does not yet support a reliability rate. Version 0.3.0 extends the platform from the workstation to a hosted AWS deployment that several teams can share.

Set against the alternatives, the distinction is attribution. Factory carries no cryptographic attestation anywhere in its product. Copilot CLI cannot evidence what an agent did, because GitHub states the audit log does not capture local prompts. The Copilot cloud agent attributes well and has no self-hosted path at all. ADP is the only option in the comparison that signs its review verdicts and, on the rebar ticket backend, its authorship on infrastructure we control, and the only one we can change when a client needs something different.

The AI-Enabled Delivery charter already names ADP. This document is the current account of what sits behind that name.

---

## Appendix: sourcing and confidence

Pilot figures come from the pilot's own audit database on the workstation build, and each is traceable to a record in that database. Claims about the platform's current behavior come from the platform source at version 0.3.0, commit `e4881b1b`. Claims about Factory and GitHub come from vendor documentation, GSA's FedRAMP dataset exported in October 2026, and public reporting, and are cited as vendor-asserted where that is what they are.

Two limitations should travel with this document. Both vendors' trust centers are client-rendered applications whose certification claims could not be independently verified, which is a symmetric limitation and not a mark against either. And 11 pull requests produced by a platform that changed twelve times across the period supports qualitative findings about scope enforcement and output shape, and does not support a reliability rate.
