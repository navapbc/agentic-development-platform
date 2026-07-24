package agentic.promotion

import rego.v1

default allow := false

bundle_version := "0.1.0"

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

reasons contains "blocking_security_findings" if {
	input.blocking_findings > 0
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
