package agentic.promotion

import rego.v1

default allow := false

bundle_version := "0.2.0"

allowed_projects := {"AGENTDEV"}

allowed_repos := {"agentic-demo-service", "agentic-demo-greenfield"}

reasons contains "jira_project_not_allowlisted" if {
	not allowed_projects[input.jira_project]
}

reasons contains "repository_not_allowlisted" if {
	not allowed_repos[input.repository]
}

reasons contains "ci_not_successful" if {
	input.ci_status != "success"
}

reasons contains "scan_incomplete" if {
	not input.scan_completed
}

reasons contains "blocking_security_findings" if {
	input.blocking_findings > 0
}

reasons contains "repair_attempts_exceeded" if {
	input.repair_attempts > input.max_repair_attempts
}

reasons contains "jira_snapshot_stale" if {
	not input.jira_snapshot_current
}

reasons contains "acceptance_not_verified" if {
	not input.acceptance_verified
}

reasons contains "pr_not_authorized" if {
	not input.pr_authorized
}

reasons contains "plan_not_approved" if {
	not input.plan_approved
}

allow if {
	count(reasons) == 0
}

next_action := "proceed" if allow

else := "block_and_report"

decision := {
	"allow": allow,
	"bundle_version": bundle_version,
	"reasons": reasons,
	"next_action": next_action,
}
