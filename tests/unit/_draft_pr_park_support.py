"""Test-only workflow that injects a one-shot finalize defect to exercise park and resume."""

from __future__ import annotations

from services.orchestration.temporal_workflows.draft_pull_request import (
    DraftPullRequestInput,
    DraftPullRequestOutput,
    DraftPullRequestWorkflow,
)
from temporalio import workflow


@workflow.defn
class DefectOnceDraftPr(DraftPullRequestWorkflow):
    def __init__(self) -> None:
        super().__init__()
        self._defects = 0

    @workflow.run
    async def run(self, params: DraftPullRequestInput) -> DraftPullRequestOutput:
        return await super().run(params)

    async def _finalize(
        self,
        params: DraftPullRequestInput,
        impl,
        workflow_id: str,
        title: str,
    ) -> DraftPullRequestOutput:
        self._defects += 1
        if self._defects == 1:
            raise AttributeError("simulated finalize defect")
        return await super()._finalize(params, impl, workflow_id, title)
