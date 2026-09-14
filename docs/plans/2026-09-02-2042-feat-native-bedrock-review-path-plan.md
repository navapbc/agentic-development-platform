---
title: Native Bedrock Review Path - Plan
type: feat
date: 2026-09-02
origin: docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: code
---

# Native Bedrock Review Path - Plan

## Goal Capsule

Objective. The rebar review gates reach Bedrock over the native pydantic-ai path with an in-process Claude-to-Nova failover, and the review verdicts, token counts, and cost they record are unchanged from the proxied path.

Means. Point the review kernel's `LLMConfig` at the native Bedrock provider instead of the LiteLLM OpenAI-compatible proxy, configure the Claude-to-Nova chain, and mount scoped AWS credentials into the worker that runs the review activities (KTD1, KTD2, KTD5).

Product authority. This plan enriches one feature from the roadmap at [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md). The design constraints come from `.agent-context/agentic_plan_v2.md` section 3 (rebar adoption stance) and its Phase 16 native-Bedrock-path bullet.

Stop conditions. The OpenAI-compatible path stays available for local development without AWS. The network path does not change. Failover triggers only on availability errors, never on structured-output-retry exhaustion.

## Product Contract

Product Contract preservation. Sourced from the roadmap feature "Native Bedrock review path". The requirements below restate that feature's subtasks and the section 3 settled decisions as R-IDs with no scope change.

### Summary

Move the rebar review kernel off the LiteLLM proxy onto the native Bedrock path. The kernel uses pydantic-ai's Bedrock provider with an in-process `FallbackModel(claude, nova, fallback_on=(ModelAPIError,))` chain. Both legs ride the same `bedrock-runtime` pathway and differ only by model id. AWS credentials move from the LiteLLM container alone to the review worker as well. Token and cost accounting stay identical, and the OpenAI-compatible path remains the local-dev fallback.

### Problem Frame

The review kernel builds its `LLMConfig` with `model_provider="openai"` and `base_url=<litellm>`, so every review gate call is proxied through LiteLLM. That path drops native structured output, the Claude leg's prompt caching, honest per-leg provenance, and per-call usage. rebar reports zero tokens on the OpenAI path, so review-stage cost is backfilled from Langfuse after the fact. Section 3 settled that rebar's review calls run on native Bedrock with in-process failover, and this feature delivers that path.

### Requirements

- R1. A configuration flag selects the review LLM path, defaulting to the native Bedrock path. When cleared, review gates fall back to the LiteLLM proxy path so local development without AWS is unchanged.
- R2. On the native path, each review tier resolves to a primary Claude model with an ordered Nova fallback, configured through rebar's model-class mechanism, one primary plus one fallback per tier.
- R3. Failover from Claude to Nova triggers only on availability errors (`ModelAPIError`), never on structured-output-retry exhaustion.
- R4. The worker that runs the review activities holds AWS credentials scoped to Bedrock review calls. rebar authenticates through the ambient AWS credential chain and a region, with no endpoint-URL override.
- R5. The native path keeps the standard regional Bedrock hostname for SigV4 signing, TLS SNI, HTTP host, and region, and reaches Bedrock over the same in-VPC origination point the proxied path uses today (locally, the `bedrock-tunnel` DNS-alias sidecar).
- R6. Review-gate token counts, cache tokens, `llm_calls`, and cost are recorded on the native path with the same `ReviewOutcome` fields and values a proxied run produces, sourced from pydantic-ai's in-process usage rather than the Langfuse backfill.
- R7. The review worker's AWS credentials are short-lived, least-privilege STS credentials for Bedrock review calls, with rotation, environment separation from other credential holders, and log redaction.
- R8. Review calls on the native path are traced to Langfuse through pydantic-ai instrumentation, preserving the execution-to-generation link the proxied path carried through request metadata.

### Scope Boundaries

- No change to the network path, the jump-host tunnel, or the VPCE. This feature swaps the model client, not the egress.
- LiteLLM stays the choke point for OpenHands and the non-rebar LangGraph stages. Only the rebar review kernel moves off it.
- The `FallbackModel` chain is the platform's addition, and contributing it upstream is out of scope here.
- No change to the review prompts, the multi-pass kernel logic, or the verdict mapping.

### Outstanding Questions

- Resolved in U4. Cost on the native path is priced through `litellm.cost_per_token`, with `token_cost_usd` normalizing the native `bedrock:<id>` provenance id to the `bedrock/<id>` form the cost map expects.
- Resolved in U4. The Langfuse backfill in `usage_rollup.py` is kept as the proxied-path safety net. The native path records usage in-process and does not depend on it.

### Sources

- Roadmap feature and verification table: [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md).
- Design constraints: `.agent-context/agentic_plan_v2.md` section 3 (native Bedrock, `FallbackModel`, settled decisions) and its section 19 Phase 16 native-Bedrock-path bullet (DNS-alias redirection, credential-holder widening).
- Swap seam: `services/review/llm.py` (`review_llm_config`), `services/model_gateway/llm_config.py` (`llm_config_for`), `scripts/config.py` (`rebar_llm_*`, `aws_profile`, `aws_region`, `bedrock_client_kwargs`).
- Kernel: `services/review/kernel.py` (`RebarReviewKernel`, `_run_multipass`, `get_runner`).
- rebar native support (installed `nava-rebar` 0.12.0): `rebar/llm/config.py` (`LLMConfig.model_provider`, `LLMConfig.bedrock_region_name`), `rebar/llm/bedrock_model.py` (`build_bedrock_provider`, `resolve_bedrock_region`), `rebar/llm/model_classes.py` (`FallbackTarget`, `build_fallback_model`, noting that `parse_class_slots`/`load_class_slots` accept only `frontier`/`standard`/`trivial`, so the platform resolver supplies the tier mapping instead), `rebar/llm/providers.py` (`ProviderSession._build_bedrock`), `rebar/llm/runner.py` (`get_runner`, `PydanticAIRunner`, `_extract_usage`).
- Activities and worker: `services/orchestration/temporal_activities/review.py` (`_run_gate`, `review_plan`/`review_code`/`review_security`/`verify_acceptance`, `build_review_gate`), `services/orchestration/worker.py`, `compose.yaml` (`activity-worker` at line 89, and `litellm` holds AWS creds at lines 148-149), `compose.override.yaml` (`bedrock-tunnel` sidecar).
- Model ids per tier. review-model is `us.anthropic.claude-sonnet-4-5-20250929-v1:0` primary with `us.amazon.nova-pro-v1:0` fallback. security-review-model and evaluation-model are both `us.anthropic.claude-opus-4-5-20251101-v1:0` primary with `us.amazon.nova-pro-v1:0` fallback. The primaries and the Sonnet fallback match `config/local/litellm-config.yaml`. The two Opus tiers share the `nova-pro` fallback because rebar keys its model classes by primary id (KTD2). The chain is declared in `config/local/rebar-review.toml` and read through `REBAR_LLM_CONFIG_FILE`.
- Accounting: `services/orchestration/usage_rollup.py` (`backfill_zeroed_stages`), `observability/gateway.py` (`token_cost_usd`, `reconcile_usage`).

## Planning Contract

### Key Technical Decisions

- KTD1. Turn the native path on through the existing `LLMConfig` seam, not a new client. Set `model_provider="bedrock"` and `bedrock_region_name` from settings and drop `base_url`/`api_key`, gated by a config flag. Governs R1, R4, R5. Rationale. `llm_config_for` is the single point every review tier flows through, and rebar's `ProviderSession._build_bedrock` and `get_runner` already support the Bedrock provider.
- KTD2. Resolve the Claude-to-Nova chain through rebar's native model-class path, driven by a config file the review worker controls, not through the runner's model-override hook. The platform maps each review tier to a provider-qualified Bedrock primary id and sets it as `LLMConfig.model`, and a rebar model-class config file (pointed at by `REBAR_LLM_CONFIG_FILE`) supplies the Nova fallback per primary. Governs R2, R3. Rationale. rebar builds the `FallbackModel` and applies prompt caching only on its native runner path. Its class table has three fixed classes keyed by primary id, so the two tiers that share the Opus primary share one Nova fallback, which is acceptable because the fallback only runs when Opus is unavailable and `nova-pro` is the more capable Nova.
- KTD6. Never inject the review model through the runner's `model_override` hook. Governs R6. Rationale. rebar hardcodes `cache_settings = None` on the model-override path (its offline test harness), which disables Anthropic prompt caching. Without caching the multi-pass review re-bills the full context on every pass and exceeds the token budget on even small tickets, so the review fails. Caching requires the native runner path, where a real Bedrock model object carries the `BedrockModelProfile` that enables caching and, for a chain, rebar intersects the caching capability across the legs.
- KTD3. Failover is availability-only. Configure the chain so `should_fall_back` fires on `ModelAPIError` and not on structured-output-retry exhaustion. Governs R3. Rationale. A Nova answer to a Claude-tuned prompt is a quality risk, not a failover case, per the section 3 settled decision.
- KTD4. Take review usage from pydantic-ai's in-process result, which rebar's `PydanticAIRunner._extract_usage` populates on the native path, and thread it into `RawReview.usage` so `_run_gate` records real tokens without the Langfuse backfill. Governs R6. Rationale. The zero-token gap is specific to the OpenAI proxy path. The native path reports usage natively, so the backfill becomes a safety net rather than the source of truth.
- KTD5. Route review-kind work to a dedicated `review-worker` on its own task queue and give only that worker the Bedrock credentials, rather than restructuring the gate engine. Governs R4, R7. Rationale. The review Bedrock calls flow through two dispatch points that already carry the review distinction, the `plan.review` gate evaluations and the `review_plan` activity, so routing those to a review queue isolates the credential holder while `evaluate_gate` and the gate engine stay unchanged and shared.

### High-Level Design

The switch is one config seam plus a platform tier resolver plus a credential mount.

- Config path. `review_llm_config(tier)` returns the native config when `rebar_review_native` is true (default) and the proxied config otherwise. `llm_config_for` is the proxied builder and is used by non-review callers unchanged, so the native branch lives in `review_llm_config` and never affects other model consumers. The native config sets `model` to the tier's provider-qualified Bedrock primary (`bedrock:<id>`), `model_provider="bedrock"`, `bedrock_region_name` from `aws_region`, and no `base_url`/`api_key`.
- Fallback chain. rebar builds the `FallbackModel` from a model-class config file the review worker points at through `REBAR_LLM_CONFIG_FILE`. The Sonnet tier is the `standard` class and the Opus tier is the `frontier` class, each with its Nova fallback. Because rebar keys the chain by primary id, the two tiers on the Opus primary share one Nova fallback. Prompt caching survives the wrap because rebar intersects the caching capability across the chain's legs, so the native path caches while the model-override path would not (KTD6).
- Credentials and egress. Review-kind work routes to a dedicated `review-worker` on its own task queue, and only that worker gains `AWS_PROFILE`/`AWS_REGION` and the credential mount, scoped to Bedrock review calls under least privilege. `evaluate_gate` is registered on both the review worker and the activity worker, so Temporal routes review gates to the review worker and scanner gates to the activity worker without forking the activity. `activity-worker` holds no AWS credentials. rebar uses the ambient chain and region with no endpoint-URL override. The worker resolves the standard Bedrock hostname to the existing `bedrock-tunnel` sidecar, so SigV4, SNI, host, and region stay canonical.
- Accounting. `_run_gate` already reads `result.usage` and prices with `token_cost_usd`. On the native path, `RawReview.usage` is populated from the runner result, so the recorded fields match a proxied run, and the backfill stays as a safety net.

### Assumptions

- rebar 0.12.0's Bedrock provider (`build_bedrock_provider`) and model-class fallback (`build_fallback_model`) work as installed, verified present in `rebar/llm/bedrock_model.py` and `rebar/llm/model_classes.py`.
- The `bedrock-tunnel` DNS-alias sidecar is reachable from `activity-worker` on the compose network, the same alias `litellm` uses today.
- The per-tier model ids in the LiteLLM config are the intended native ids, and the tier resolver reuses them.

### Sequencing

U1 (config flag and native `LLMConfig`) and U2 (tier resolver and fallback) are the core switch and come first, and U2 depends on U1. U3 (dedicated review worker and credentials) is independent infrastructure. U4 (usage parity) depends on U1 and U2. U5 (failover and parity verification) depends on U1 through U4.

## Implementation Units

### U1. Native LLMConfig behind a config flag

Goal. Add a `rebar_review_native` flag and make `llm_config_for` return a native Bedrock `LLMConfig` when it is set, keeping native Bedrock as the default and the proxied config as the opt-out.
Requirements. R1, R4, R5.
Files. `scripts/config.py` (add `rebar_review_native: bool = False`), `services/model_gateway/llm_config.py` (`llm_config_for` branches on the flag), `pyproject.toml` and `docker/` review-runtime image (add the pydantic-ai Bedrock extra and its `boto3`/`botocore` runtime), `.env.example` (document the flag).
Approach. When the flag is false, return today's `LLMConfig` unchanged. When true, return `LLMConfig(runner="pydantic_ai", model=model, model_provider="bedrock", bedrock_region_name=resolved.aws_region, max_tokens=..., temperature=...)` with no `base_url` or `api_key`. Do not set an endpoint-URL override (KTD1). Add `pydantic-ai-slim[bedrock]` to the review runtime so `boto3`/`botocore` and `pydantic_ai.providers.bedrock` are importable, since the installed image ships only the OpenAI extra today.
Test scenarios. The flag defaults to true and `llm_config_for` returns a native config with `model_provider="bedrock"`, `bedrock_region_name` from `aws_region`, and no `base_url`/`api_key`. With the flag cleared, the config carries `model_provider="openai"` and the LiteLLM `base_url`. `review_llm_config(tier)` reflects the flag for each of the three tiers. A preflight import of `boto3` and `pydantic_ai.providers.bedrock` succeeds, and `build_bedrock_provider` is reachable with the Bedrock client mocked.
Verification. `tests/unit/test_review_llm.py` (extend), `tests/unit/test_llm_config.py` (extend or add), `inv test`.

### U2. Platform review-tier resolver and Claude-to-Nova fallback

Goal. Resolve each review tier to a provider-qualified Bedrock primary and drive the Claude-to-Nova fallback through rebar's native model-class path, availability-triggered, with prompt caching intact.
Requirements. R2, R3.
Files. `services/model_gateway/review_tiers.py` (the tier-to-primary mapping and the provider-qualified refs), `services/review/llm.py` (native config sets the model to the tier's qualified primary), `config/local/rebar-review.toml` (the rebar model-class table with the Claude primaries and Nova fallbacks), `compose.yaml` (`REBAR_LLM_CONFIG_FILE` on the review worker), `services/review/kernel.py` (run through `get_runner`, not the model-override hook).
Approach. Set the native `LLMConfig.model` to the tier's provider-qualified Bedrock primary (`bedrock:<id>`), so rebar builds a real Bedrock model object that carries a `BedrockModelProfile`. Supply the Claude-to-Nova chain in a rebar model-class config file pointed at by `REBAR_LLM_CONFIG_FILE`, whose `standard` class is the Sonnet tier and whose `frontier` class is the Opus tier, each with its Nova fallback. rebar reads that config, builds the `FallbackModel` itself, and `should_fall_back` fires on availability errors only (KTD3). The model-override hook is not used, because it forces prompt caching off (KTD6). rebar's three classes are keyed by primary id, so the two tiers that share the Opus primary share one Nova fallback (KTD2).
Test scenarios. The resolver returns a Claude primary and the qualified refs for each of the three tiers. The shipped model-class config maps the Sonnet and Opus primaries to the `standard` and `frontier` classes and lists the Nova fallback for each. With `REBAR_LLM_CONFIG_FILE` set, `fallback_targets_for` resolves each tier's primary to its Nova fallback. `should_fall_back` returns true for an availability error and false for a structured-output-retry exhaustion error. An unknown tier name is rejected with a clear error rather than silently defaulting.
Verification. `tests/unit/test_review_tiers.py` (new), `inv test`, and a live check that a native review caches (`cache_write` or `cache_read` non-zero) with no conservative-capability warning.

### U3. Dedicated review worker with scoped Bedrock credentials

Goal. Run every review Bedrock call on a dedicated worker that holds the Bedrock credentials, so no other activity inherits them, by routing review-kind work to a dedicated task queue.
Requirements. R4, R5, R7.
Files. `services/orchestration/worker.py` (add a `review` worker role and register `evaluate_gate` plus the review activities on it), `scripts/config.py` (a `review_task_queue` setting), `services/orchestration/temporal_workflows/draft_pull_request.py` (dispatch the `plan.review` gate evaluations with `task_queue=review_task_queue`), `services/orchestration/temporal_workflows/review.py` (dispatch `review_plan` with `task_queue=review_task_queue`), `compose.yaml` and `compose.override.yaml` (a new `review-worker` service with the AWS credentials and the `bedrock-tunnel` alias reachable), `.env.example`, deployment docs under `docs/`.
Approach. The pipeline already partitions gates into `plan.review` (the LLM review gates code-review, security-review, acceptance) and `plan.scanner` (semgrep, trivy), and plan review runs as the standalone `review_plan` activity. The review Bedrock calls therefore flow through exactly two dispatch points, the `plan.review` gate evaluations through `evaluate_gate` and the `review_plan` activity. Route both to `review_task_queue`. Register `evaluate_gate` on both the review worker and the activity worker so Temporal routes by queue, review gates to the review worker and scanner gates to the activity worker, running the same activity code in both places rather than forking it. Add a `review` role to `build_worker` that registers `evaluate_gate` and the review activities on `review_task_queue`, and add a `review-worker` service holding the Bedrock credentials scoped to short-lived least-privilege Bedrock access with rotation, environment separation, and log redaction (R7). Keep the standard Bedrock hostname resolving through the `bedrock-tunnel` sidecar and set no endpoint-URL override. Locally the review worker sources Bedrock credentials from the operator's AWS profile mounted read-only at the worker home, matching how the LiteLLM service sources them, while in AWS it reads the task role from the ambient credential chain. Credential isolation is enforced by the scoped credentials on the review worker plus queue routing, so a review gate never runs where Bedrock credentials are absent, and in AWS the activity worker's task role carries no `bedrock:InvokeModel` permission as defense in depth.
Test scenarios. The `review` worker role registers `evaluate_gate` and the review activities. The draft-PR workflow dispatches `plan.review` gate evaluations to `review_task_queue`, and the plan-review workflow dispatches `review_plan` to `review_task_queue`. Scanner gates and non-review activities stay on the default queue. `review-worker` config exposes `AWS_REGION` and a credential source and no `bedrock_endpoint_url` override, while `activity-worker` exposes no AWS credentials. The credential scope is documented as Bedrock-review-only with rotation. A config that grants the review worker a non-scoped or long-lived key fails the documented check. Worker logging redacts AWS access key ids, secret access keys, session tokens, and credential-source values from startup and activity logs. The Bedrock hostname resolves to the sidecar alias on the review-worker network.
Verification. `tests/unit/test_worker_roles.py` (new, over the role-to-activities and task-queue surface), `tests/unit/test_draft_pull_request_workflow.py` and `tests/unit/test_plan_review_workflow.py` (extend for the task-queue routing), manual `inv local-up` plus `inv smoke-test` note, `inv test`.

### U4. Native usage parity

Goal. Populate review-gate usage from pydantic-ai's in-process result on the native path so recorded tokens and cost match the proxied path.
Requirements. R6.
Files. `services/review/kernel.py` (`_run_multipass`/`_raw_from_decided` thread and sum the runner's usage into `RawReview.usage`, and record the answering leg from the runner result for provenance), `services/review/types.py` (`TokenUsage` if a field is missing), `observability/gateway.py` (`token_cost_usd` normalizes a `bedrock:<id>` provenance id to the `bedrock/<id>` form LiteLLM prices), `services/orchestration/usage_rollup.py` (keep `backfill_zeroed_stages` as the proxied-path safety net).
Approach. Read the runner result usage that `PydanticAIRunner._extract_usage` populates and set `RawReview.usage` prompt, completion, cache, and `llm_calls` fields (KTD4). Because a review gate makes more than one `runner.run` call, sum `input_tokens`, `output_tokens`, the cache token fields, and `requests` across the finder call and every verifier chunk in `_run_multipass` before building `RawReview.usage`. Record provenance from the runner result's answering-model id, which is the leg that actually ran, so a Nova failover is attested rather than the primary. `_run_gate` prices with `token_cost_usd`, which normalizes the native `bedrock:<id>` provenance id to the `bedrock/<id>` form LiteLLM's cost map expects, since the model id itself contains a colon. Keep the Langfuse backfill as the proxied-path safety net, since the native path no longer depends on it.
Test scenarios. A native review run records non-zero prompt and completion tokens and `llm_calls` from the runner result, summed across the finder and every verifier chunk. Cost is priced from those tokens, and a `bedrock:<id>` provenance id prices to the same value as its `bedrock/<id>` form. The recorded `ReviewOutcome` fields match a proxied run over the same fixture within accounting tolerance. A proxied result reporting zero usage still reconciles through the backfill.
Verification. `tests/unit/test_review_activities.py` (extend), `tests/unit/test_review_kernel.py` (extend), `tests/unit/test_gateway_langfuse.py` (extend), `inv test`.

### U6. Native Langfuse tracing

Goal. Trace native review calls to Langfuse through pydantic-ai instrumentation, preserving the execution-to-generation link the proxied path carried through LiteLLM request metadata.
Requirements. R8.
Files. `observability/telemetry.py` (a Langfuse OTLP exporter for generative-AI spans, `instrument_llm_tracing`, and the session-join stamping on `ContextStampingSpanProcessor`), `observability/attributes.py` (the Langfuse attribute keys), `observability/__init__.py` (export `instrument_llm_tracing`), `services/orchestration/worker.py` (instrument at startup), `compose.override.yaml` (mount `observability` into the review-worker so the worker runs the current telemetry code).
Approach. The native path bypasses the LiteLLM Langfuse callback, so a review generation reaches Langfuse only through the pydantic-ai OTEL spans. Instrument pydantic-ai at worker startup and add a Langfuse OTLP span exporter to the platform tracer provider that forwards only `gen_ai.*` spans, guarded by the Langfuse credentials and the OTLP HTTP exporter so it degrades to collector-only tracing when either is absent. Extend the context-stamping span processor to map the execution id onto `langfuse.session.id` and the stage, task, and prompt version onto Langfuse trace metadata, so a native generation is attributable to its execution and stage the same way the proxied `session_id` join was. In production the built image carries the telemetry code, so the mount is a local-development concern only.
Test scenarios. The span processor stamps `langfuse.session.id` from the execution id and the stage, task, and prompt version as trace metadata. The generative-AI exporter forwards only generation spans and skips the inner export when none qualify. The Langfuse exporter is absent without credentials and present with them. Instrumentation is idempotent. Live, a native review emits `gen_ai` spans stamped with the session join that the Langfuse OTEL endpoint accepts.
Verification. `tests/unit/test_observability_tracing.py` (extend), `inv ci`, and a live check in the review-worker that a native review's `gen_ai` spans carry the session join and export to Langfuse successfully.

### U5. Failover and parity verification

Goal. Prove per-leg failover and end-to-end accounting parity on the native path.
Requirements. R3, R6.
Files. `services/model_gateway/review_tiers.py` (recording fallback model, answering-leg mapping), `services/review/kernel.py` (both-leg block path, answering-leg provenance), `tests/unit/test_review_tiers.py` (extend), `tests/unit/test_review_kernel.py` (extend), `tests/integration/test_review_llm_live.py` (extend).
Approach. Record the leg that answered each request with a `FallbackModel` subclass, so a run injected through the runner's model-override path can attest the model that ran rather than the configured primary. Prove failover deterministically with injected legs. The Claude leg raises an availability error and the Nova leg answers, and the recorded leg is Nova. A structured-output-retry exhaustion classifies outside the availability set, so `should_fall_back` returns false and no failover occurs. When both legs raise, the kernel catches the `FallbackExceptionGroup` and returns a `BLOCK` verdict with a zero-usage row, so the gate attests and records it rather than raising into Temporal. The live check confirms the native path records in-process usage and names a Bedrock answering leg, and that native and proxied agree on the verdict over a shared fixture.
Test scenarios. An availability error on the Claude leg fails over to Nova and provenance records the fallback leg. Structured-output-retry exhaustion does not fail over. When both legs error, the kernel returns a `BLOCK` verdict with a zero-usage row and does not raise, and a non-failover error still propagates. The live native run records non-zero in-process usage and a Bedrock answering leg, and native and proxied produce the same verdict over one fixture.
Verification. `tests/unit/test_review_tiers.py`, `tests/unit/test_review_kernel.py`, `inv ci`, and `RUN_LIVE_LLM=1 pytest tests/integration/test_review_llm_live.py` against the running gateway and tunnel.

## Verification Contract

- Lint. `ruff check` clean.
- Types. `basedpyright` clean on `services/review`, `services/model_gateway`, and `scripts/config.py`.
- Unit and end-to-end. `inv ci` runs `ruff check`, `pytest -q tests/unit tests/end_to_end`, and the evaluation gate. All green.
- Full suite. `inv test` green, including `tests/integration`.
- Regression floor. With `rebar_review_native` cleared, every pre-existing test in `tests/unit/test_review_llm.py`, `tests/unit/test_review_kernel.py`, `tests/unit/test_review_gate.py`, `tests/unit/test_review_activities.py`, and `tests/unit/test_plan_review_workflow.py` passes with no assertion change.
- Live parity. The native path matches the proxied path on verdict and usage over a shared fixture, proven in `tests/integration/test_review_llm_live.py`.

## Definition of Done

- Global. R1 through R8 are met. `inv ci` and `inv test` are green. With the flag cleared, the review path behaves exactly as before. Abandoned or experimental code from approaches that did not pan out is removed from the diff.
- U1. `rebar_review_native` defaults to true, and on the native path `review_llm_config` returns a native Bedrock config with no endpoint-URL override, while `llm_config_for` stays the proxied builder for non-review callers.
- U2. Each review tier resolves to a provider-qualified Bedrock primary, rebar builds the Claude-to-Nova `FallbackModel` natively from the model-class config with prompt caching intact, and failover is availability-only. The two Opus tiers share the `nova-pro` fallback.
- U3. Review-kind work routes to a dedicated `review-worker` on its own task queue that holds scoped, least-privilege Bedrock credentials with rotation and log redaction, `activity-worker` holds none, and the worker reaches Bedrock over the standard hostname through the existing tunnel.
- U4. Native review runs record real tokens and cost in `ReviewOutcome`, summed across the finder and verifier calls, without relying on the Langfuse backfill.
- U5. Per-leg failover is proven, structured-output-retry exhaustion does not fail over, a both-leg failure returns a `BLOCK` verdict with an attestation and a zero-usage row, and native-versus-proxied accounting parity is proven over a shared fixture.
- U6. Native review calls are instrumented and exported to Langfuse as generative-AI spans stamped with `langfuse.session.id` from the execution id and the stage as trace metadata, so a native generation is attributable to its execution and stage the way the proxied path was.
