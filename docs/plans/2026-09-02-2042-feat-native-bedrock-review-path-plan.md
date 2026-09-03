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

Product Contract preservation. Sourced from the roadmap feature "Native Bedrock review path"; requirements below restate that feature's subtasks and the section 3 settled decisions as R-IDs with no scope change.

### Summary

Move the rebar review kernel off the LiteLLM proxy onto the native Bedrock path. The kernel uses pydantic-ai's Bedrock provider with an in-process `FallbackModel(claude, nova, fallback_on=(ModelAPIError,))` chain. Both legs ride the same `bedrock-runtime` pathway and differ only by model id. AWS credentials move from the LiteLLM container alone to the review worker as well. Token and cost accounting stay identical, and the OpenAI-compatible path remains the local-dev fallback.

### Problem Frame

The review kernel builds its `LLMConfig` with `model_provider="openai"` and `base_url=<litellm>`, so every review gate call is proxied through LiteLLM. That path drops native structured output, the Claude leg's prompt caching, honest per-leg provenance, and per-call usage (rebar reports zero tokens on the OpenAI path, so review-stage cost is backfilled from Langfuse after the fact). Section 3 settled that rebar's review calls run on native Bedrock with in-process failover; this feature delivers that path.

### Requirements

- R1. A configuration flag selects the review LLM path, defaulting to the current proxied path so local development without AWS is unchanged. When set to native, review gates use the pydantic-ai Bedrock provider.
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
- The `FallbackModel` chain is the platform's addition; contributing it upstream is out of scope here.
- No change to the review prompts, the multi-pass kernel logic, or the verdict mapping.

### Outstanding Questions

- Deferred to planning. Whether cost on the native path is priced through `litellm.cost_per_token` (unchanged) or a Bedrock-native pricing source. Resolve in U4. Non-blocking; `token_cost_usd` already covers Bedrock model ids.
- Deferred to planning. Whether the Langfuse backfill in `usage_rollup.py` is retired or kept as a safety net once native usage is populated. Resolve in U4. Non-blocking.

### Sources

- Roadmap feature and verification table: [2026-09-02-1801-feat-remaining-work-roadmap-plan.md](docs/plans/2026-09-02-1801-feat-remaining-work-roadmap-plan.md).
- Design constraints: `.agent-context/agentic_plan_v2.md` section 3 (native Bedrock, `FallbackModel`, settled decisions) and its section 19 Phase 16 native-Bedrock-path bullet (DNS-alias redirection, credential-holder widening).
- Swap seam: `services/review/llm.py` (`review_llm_config`), `services/model_gateway/llm_config.py` (`llm_config_for`), `scripts/config.py` (`rebar_llm_*`, `aws_profile`, `aws_region`, `bedrock_client_kwargs`).
- Kernel: `services/review/kernel.py` (`RebarReviewKernel`, `_run_multipass`, `get_runner`).
- rebar native support (installed `nava-rebar` 0.12.0): `rebar/llm/config.py` (`LLMConfig.model_provider`, `LLMConfig.bedrock_region_name`), `rebar/llm/bedrock_model.py` (`build_bedrock_provider`, `resolve_bedrock_region`), `rebar/llm/model_classes.py` (`FallbackTarget`, `build_fallback_model`; note `parse_class_slots`/`load_class_slots` accept only `frontier`/`standard`/`trivial`, so the platform resolver supplies the tier mapping instead), `rebar/llm/providers.py` (`ProviderSession._build_bedrock`), `rebar/llm/runner.py` (`get_runner`, `PydanticAIRunner`, `_extract_usage`).
- Activities and worker: `services/orchestration/temporal_activities/review.py` (`_run_gate`, `review_plan`/`review_code`/`review_security`/`verify_acceptance`, `build_review_gate`), `services/orchestration/worker.py`, `compose.yaml` (`activity-worker` at line 89; `litellm` holds AWS creds at lines 148-149), `compose.override.yaml` (`bedrock-tunnel` sidecar).
- Model ids per tier (from `config/local/litellm-config.yaml`): review-model = `us.anthropic.claude-sonnet-4-5-20250929-v1:0` primary, `us.amazon.nova-pro-v1:0` fallback; security-review-model = `us.anthropic.claude-opus-4-5-20251101-v1:0` primary, `us.amazon.nova-pro-v1:0` fallback; evaluation-model = `us.anthropic.claude-opus-4-5-20251101-v1:0` primary, `us.amazon.nova-2-lite-v1:0` fallback.
- Accounting: `services/orchestration/usage_rollup.py` (`backfill_zeroed_stages`), `observability/gateway.py` (`token_cost_usd`, `reconcile_usage`).

## Planning Contract

### Key Technical Decisions

- KTD1. Turn the native path on through the existing `LLMConfig` seam, not a new client. Set `model_provider="bedrock"` and `bedrock_region_name` from settings and drop `base_url`/`api_key`, gated by a config flag. Governs R1, R4, R5. Rationale: `llm_config_for` is the single point every review tier flows through, and rebar's `ProviderSession._build_bedrock` and `get_runner` already support the Bedrock provider.
- KTD2. Resolve the Claude-to-Nova chain through a platform review-tier resolver, not rebar's model-class table. The resolver maps each review tier to a provider-qualified Bedrock primary id and its own Nova fallback target, and builds the pydantic-ai `FallbackModel` (via `build_fallback_model` or directly) before `get_runner`. Governs R2, R3. Rationale: rebar's `parse_class_slots` accepts only `frontier`/`standard`/`trivial`, the kernel passes the tier string straight into `LLMConfig.model`, and two tiers share a Claude primary but need different Nova fallbacks, which a primary-keyed model-class table cannot express.
- KTD3. Failover is availability-only. Configure the chain so `should_fall_back` fires on `ModelAPIError` and not on structured-output-retry exhaustion. Governs R3. Rationale: a Nova answer to a Claude-tuned prompt is a quality risk, not a failover case (section 3 settled decision).
- KTD4. Take review usage from pydantic-ai's in-process result, which rebar's `PydanticAIRunner._extract_usage` populates on the native path, and thread it into `RawReview.usage` so `_run_gate` records real tokens without the Langfuse backfill. Governs R6, R8. Rationale: the zero-token gap is specific to the OpenAI proxy path; the native path reports usage natively, so the backfill becomes a safety net rather than the source of truth.
- KTD5. Run the review activities in a dedicated `review-worker` on its own task queue, and give only that worker the AWS credentials. Governs R4, R7. Rationale: the review activities share `activity-worker` with every other activity today, so mounting Bedrock credentials there would grant them to all activities; a dedicated worker keeps the credential holder scoped to the review kernel.

### High-Level Design

The switch is one config seam plus a platform tier resolver plus a credential mount.

- Config path. `review_llm_config(tier)` calls `llm_config_for(tier)`. Add a `rebar_review_native` flag (default false). When false, the current proxied `LLMConfig` is returned unchanged. When true, `llm_config_for` returns an `LLMConfig` with `model_provider="bedrock"`, `bedrock_region_name` from `aws_region`, and no `base_url`/`api_key`.
- Fallback chain. A platform review-tier resolver maps `review-model`, `security-review-model`, and `evaluation-model` each to a provider-qualified Bedrock primary id and its own Nova fallback target, and builds the pydantic-ai `FallbackModel` before the runner executes. The resolver, not `LLMConfig.model`, owns the tier-to-model mapping, so the two tiers that share a Claude primary still get distinct Nova fallbacks.
- Credentials and egress. The review activities move to a dedicated `review-worker` on its own task queue, and only that worker gains `AWS_PROFILE`/`AWS_REGION` and the credential mount, scoped to Bedrock review calls (STS, least privilege). `activity-worker` holds no AWS credentials. rebar uses the ambient chain and region with no endpoint-URL override. The worker resolves the standard Bedrock hostname to the existing `bedrock-tunnel` sidecar, so SigV4, SNI, host, and region stay canonical.
- Accounting. `_run_gate` already reads `result.usage` and prices with `token_cost_usd`. On the native path, `RawReview.usage` is populated from the runner result, so the recorded fields match a proxied run; the backfill stays as a safety net.

### Assumptions

- rebar 0.12.0's Bedrock provider (`build_bedrock_provider`) and model-class fallback (`build_fallback_model`) work as installed, verified present in `rebar/llm/bedrock_model.py` and `rebar/llm/model_classes.py`.
- The `bedrock-tunnel` DNS-alias sidecar is reachable from `activity-worker` on the compose network, the same alias `litellm` uses today.
- The per-tier model ids in the LiteLLM config are the intended native ids; the tier resolver reuses them.

### Sequencing

U1 (config flag and native `LLMConfig`) and U2 (tier resolver and fallback) are the core switch and come first; U2 depends on U1. U3 (dedicated review worker and credentials) is independent infrastructure. U4 (usage parity) depends on U1 and U2. U5 (failover and parity verification) depends on U1 through U4.

## Implementation Units

### U1. Native LLMConfig behind a config flag

Goal. Add a `rebar_review_native` flag and make `llm_config_for` return a native Bedrock `LLMConfig` when it is set, keeping the proxied config as the default.
Requirements. R1, R4, R5.
Files. `scripts/config.py` (add `rebar_review_native: bool = False`), `services/model_gateway/llm_config.py` (`llm_config_for` branches on the flag), `pyproject.toml` and `docker/` review-runtime image (add the pydantic-ai Bedrock extra and its `boto3`/`botocore` runtime), `.env.example` (document the flag).
Approach. When the flag is false, return today's `LLMConfig` unchanged. When true, return `LLMConfig(runner="pydantic_ai", model=model, model_provider="bedrock", bedrock_region_name=resolved.aws_region, max_tokens=..., temperature=...)` with no `base_url` or `api_key`. Do not set an endpoint-URL override (KTD1). Add `pydantic-ai-slim[bedrock]` to the review runtime so `boto3`/`botocore` and `pydantic_ai.providers.bedrock` are importable; the installed image ships only the OpenAI extra today.
Test scenarios. The flag defaults to false and `llm_config_for` returns the proxied config with `model_provider="openai"` and the LiteLLM `base_url`. With the flag true, the config carries `model_provider="bedrock"`, `bedrock_region_name` from `aws_region`, and no `base_url`/`api_key`. `review_llm_config(tier)` reflects the flag for each of the three tiers. A preflight import of `boto3` and `pydantic_ai.providers.bedrock` succeeds, and `build_bedrock_provider` is reachable with the Bedrock client mocked.
Verification. `tests/unit/test_review_llm.py` (extend), `tests/unit/test_llm_config.py` (extend or add), `inv test`.

### U2. Platform review-tier resolver and Claude-to-Nova fallback

Goal. Resolve each review tier to a provider-qualified Bedrock primary with its own Nova fallback and build the pydantic-ai `FallbackModel`, availability-triggered.
Requirements. R2, R3.
Files. `services/review/llm.py` or a new `services/model_gateway/review_tiers.py` (the tier resolver and the per-tier primary and fallback ids), `services/review/kernel.py` (build the `FallbackModel` from the resolver before running).
Approach. Define the tier-to-model mapping from Sources: each of `review-model`, `security-review-model`, and `evaluation-model` maps to a provider-qualified Claude primary id and one Nova fallback id (KTD2). Build the pydantic-ai `FallbackModel` for the resolved tier, using rebar's `build_fallback_model` with explicit `FallbackTarget`s or the pydantic-ai `FallbackModel` directly. Configure the chain so `should_fall_back` fires on `ModelAPIError` only, not on structured-output-retry exhaustion (KTD3). Do not rely on rebar's `parse_class_slots`/`load_class_slots`, which accept only `frontier`/`standard`/`trivial`.
Test scenarios. The resolver returns a Claude primary and a Nova fallback for each of the three tiers, with the two Opus-primary tiers carrying distinct Nova fallbacks. The built `FallbackModel` lists the primary then the fallback. `should_fall_back` returns true for `ModelAPIError` and false for a structured-output-retry exhaustion error. An unknown tier name is rejected with a clear error rather than silently defaulting.
Verification. `tests/unit/test_review_tiers.py` (new), `inv test`.

### U3. Dedicated review worker with scoped Bedrock credentials

Goal. Run the review activities in their own worker on a dedicated task queue that holds the Bedrock credentials, so no other activity inherits them.
Requirements. R4, R5, R7.
Files. `services/orchestration/worker.py` (add a `review` worker role that registers only the review activities on a dedicated task queue), `scripts/config.py` (a `review_task_queue` setting), `services/orchestration/temporal_workflows/review.py` (dispatch `review_plan`/`review_code`/`review_security`/`verify_acceptance` with `task_queue=<review queue>`), `compose.yaml` (a new `review-worker` service with the AWS credential mount, and removal of review activities from `activity-worker`'s set), `compose.override.yaml` (`bedrock-tunnel` alias reachable from `review-worker`), `.env.example`, deployment docs under `docs/`.
Approach. Add a `review` role to `build_worker` that registers only the review activities on `review_task_queue`, and drop them from the `activity-worker` role so the shared worker never holds Bedrock credentials (KTD5). The review workflow routes its activities to the review task queue. Give `review-worker` `AWS_PROFILE`/`AWS_REGION` and the credential source, scoped to short-lived least-privilege Bedrock STS credentials with rotation, environment separation, and log redaction (R7). Keep the standard Bedrock hostname resolving through the `bedrock-tunnel` sidecar; set no endpoint-URL override.
Test scenarios. The `review` worker role registers the four review activities and nothing else; the `activity-worker` role no longer registers them. The review workflow dispatches review activities to `review_task_queue`. `review-worker` config exposes `AWS_REGION` and a credential source and no `bedrock_endpoint_url` override; `activity-worker` exposes no AWS credentials. The credential scope is documented as Bedrock-review-only STS with rotation. A config that grants the review worker a non-scoped or long-lived key fails the documented check. Worker logging redacts AWS access key ids, secret access keys, session tokens, and credential-source values from startup and activity logs. The Bedrock hostname resolves to the sidecar alias on the review-worker network.
Verification. `tests/unit/test_worker_roles.py` (new, over the role-to-activities and task-queue surface), `tests/unit/test_plan_review_workflow.py` (extend for the task-queue routing), manual `inv local-up` plus `inv smoke-test` note, `inv test`.

### U4. Native usage parity

Goal. Populate review-gate usage from pydantic-ai's in-process result on the native path so recorded tokens and cost match the proxied path.
Requirements. R6, R8.
Files. `services/review/kernel.py` (`_run_multipass`/`_raw_from_decided` thread and sum the runner's usage into `RawReview.usage`), `services/review/types.py` (`TokenUsage` if a field is missing), `services/review/llm.py` or the kernel (propagate `bind_execution` context into native pydantic-ai Langfuse metadata), `services/orchestration/usage_rollup.py` (keep `backfill_zeroed_stages` as a safety net).
Approach. Read the runner result usage that `PydanticAIRunner._extract_usage` populates and set `RawReview.usage` prompt, completion, cache, and `llm_calls` fields (KTD4). Because a review gate makes more than one `runner.run` call, sum `input_tokens`, `output_tokens`, the cache token fields, and `requests` across the finder call and every verifier chunk in `_run_multipass` before building `RawReview.usage`. Propagate `execution_id`, `task_id`, `stage`, and `prompt_version` from `bind_execution` into the native pydantic-ai Langfuse observations as session and generation metadata, since the native path bypasses the LiteLLM header-injection join the proxied path used. `_run_gate` already prices with `token_cost_usd` and records the `ReviewOutcome` fields, so no activity change is needed. Keep the Langfuse backfill so a zero-usage native result still reconciles. Resolve the cost-source and backfill-retirement Outstanding Questions here.
Test scenarios. A native review run records non-zero prompt and completion tokens and `llm_calls` from the runner result, summed across the finder and every verifier chunk. Cost is priced from those tokens. The recorded `ReviewOutcome` fields match a proxied run over the same fixture within accounting tolerance. A native result reporting zero usage still reconciles through the backfill. A native generation is grouped under the execution session and stage so `fetch_session_generation_rows` and `backfill_zeroed_stages` find it.
Verification. `tests/unit/test_review_activities.py` (extend), `tests/unit/test_review_kernel.py` (extend), `inv test`.

### U5. Failover and parity verification

Goal. Prove per-leg failover and end-to-end accounting parity on the native path.
Requirements. R3, R6.
Files. `tests/integration/test_review_llm_live.py` (extend), a native-path review scenario under `tests/integration/`.
Approach. Exercise the review gate on the native path with the Claude leg forced to raise `ModelAPIError` and assert the Nova leg answers and the provenance names the leg that ran. Assert a structured-output-retry exhaustion does not trigger failover. When both legs raise, catch the `FallbackModel` exception in the kernel or gate and return a `BLOCK` verdict with an attestation and a zero-usage row, so the run follows the normal block path rather than raising into Temporal. Compare recorded usage and verdict between a native run and a proxied run over the same fixture.
Test scenarios. Claude `ModelAPIError` fails over to Nova and provenance records the fallback leg. Structured-output-retry exhaustion does not fail over. When both legs error, the gate returns a `BLOCK` verdict with an attestation and a zero-usage row, and does not raise. A native run and a proxied run produce the same verdict and matching usage over one fixture.
Verification. `tests/integration/test_review_llm_live.py`, the new native-path integration scenario, `inv ci`.

## Verification Contract

- Lint. `ruff check` clean.
- Types. `basedpyright` clean on `services/review`, `services/model_gateway`, and `scripts/config.py`.
- Unit and end-to-end. `inv ci` runs `ruff check`, `pytest -q tests/unit tests/end_to_end`, and the evaluation gate. All green.
- Full suite. `inv test` green, including `tests/integration`.
- Regression floor. With `rebar_review_native` unset, every pre-existing test in `tests/unit/test_review_llm.py`, `tests/unit/test_review_kernel.py`, `tests/unit/test_review_gate.py`, `tests/unit/test_review_activities.py`, and `tests/unit/test_plan_review_workflow.py` passes with no assertion change.
- Live parity. The native path matches the proxied path on verdict and usage over a shared fixture, proven in `tests/integration/test_review_llm_live.py`.

## Definition of Done

- Global. R1 through R8 are met. `inv ci` and `inv test` are green. With the flag unset, the review path behaves exactly as before. Abandoned or experimental code from approaches that did not pan out is removed from the diff.
- U1. `rebar_review_native` defaults to false; when true, `llm_config_for` returns a native Bedrock config with no endpoint-URL override.
- U2. Each review tier resolves through the platform resolver to a Claude primary and its own Nova fallback, and failover is `ModelAPIError`-only.
- U3. Review activities run in a dedicated `review-worker` on its own task queue that holds scoped, short-lived, least-privilege Bedrock STS credentials with rotation and log redaction; `activity-worker` holds none; and the worker reaches Bedrock over the standard hostname through the existing tunnel.
- U4. Native review runs record real tokens and cost in `ReviewOutcome`, summed across the finder and verifier calls, without relying on the Langfuse backfill, and native generations carry the execution-to-generation session and stage metadata.
- U5. Per-leg failover is proven, structured-output-retry exhaustion does not fail over, a both-leg failure returns a `BLOCK` verdict with an attestation and a zero-usage row, and native-versus-proxied accounting parity is proven over a shared fixture.
