package agentic.promotion

import rego.v1

default allow := false

bundle_version := "0.4.0"

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

reasons contains "repair_attempts_exceeded" if {
	input.repair_attempts > input.max_repair_attempts
}

reasons contains "jira_snapshot_stale" if {
	not input.jira_snapshot_current
}

reasons contains "pr_not_authorized" if {
	not input.pr_authorized
}

ran_gates contains name if {
	some gate in input.gates
	name := gate.name
}

reasons contains sprintf("gate_failed:%s", [gate.name]) if {
	some gate in input.gates
	gate.required
	gate.verdict != "pass"
	object.get(gate, "on_block", "fail_closed") != "pause_for_human"
}

human_review contains sprintf("gate_failed:%s", [gate.name]) if {
	some gate in input.gates
	gate.required
	gate.verdict != "pass"
	object.get(gate, "on_block", "fail_closed") == "pause_for_human"
}

reasons contains sprintf("required_gate_missing:%s", [name]) if {
	some name in input.required_gates
	not ran_gates[name]
}

allow if {
	count(reasons) == 0
	count(human_review) == 0
}

next_action := "proceed" if allow

else := "await_human" if {
	count(reasons) == 0
	count(human_review) > 0
}

else := "block_and_report"

decision := {
	"allow": allow,
	"bundle_version": bundle_version,
	"reasons": reasons,
	"human_review": human_review,
	"next_action": next_action,
}
