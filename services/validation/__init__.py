"""Deployment validation, the same locally and in AWS.

Each check has a scope, platform or tenant, and returns pass, fail, or skip with a cause, the
fix rendered for the deployment posture, and how to confirm the fix. Tenant validation runs in the
tenant's activity worker with the tenant's own settings and credentials, and reports each
platform dependency as one pass or fail line. Platform validation runs as the
`deployment-validate` Job in the platform namespace and starts each requested tenant's
validation on that tenant's task queue.

Stable entry points for a caller in the tenant's activity worker, such as a Run validation
button:

- `services.orchestration.temporal_activities.validation.validate_tenant`, the activity, which
  takes a `TenantValidationRequest` and returns a `TenantReport`. A caller outside a workflow
  runs `TenantValidationWorkflow` on the tenant's task queue, or awaits
  `services.validation.tenant.validate_tenant(settings, request, client=client)` in process.
- `services.validation.report.render_tenant_report(report, posture)`, a pure function that
  returns the report as plain lines with backticked commands, fit for a Slack message, a thread
  reply, or a terminal. It never names another tenant or a platform internal.
- `TenantValidationRequest(post=True)` posts the rendered report to the tenant's Slack channel,
  and that post is the Slack post check. A caller that posts the report itself, such as a thread
  reply, leaves `post` off.
"""
