"""Pinned span attribute keys for platform telemetry.

The platform keys correlate a span to a task execution and stage. The GenAI keys
follow the OpenTelemetry generative-AI semantic conventions.
"""

from __future__ import annotations

EXECUTION_ID = "execution_id"
TASK_ID = "task_id"
STAGE = "stage"
GATE = "gate"
PROMPT_VERSION = "prompt_version"
MODEL_ID = "model_id"

PLATFORM_KEYS: tuple[str, ...] = (
    EXECUTION_ID,
    TASK_ID,
    STAGE,
    GATE,
    PROMPT_VERSION,
    MODEL_ID,
)

GEN_AI_SYSTEM = "gen_ai.system"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
