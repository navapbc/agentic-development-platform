#!/usr/bin/env bash
# Port-forward through the ephemeral Fargate jumphost the bedrock-jumphost-fargate
# module provisions.
#
# The connectivity provider invokes this as:
#   connect_to_jumphost.sh -n <name> [-e <env>] -p <localport>:<host>:<remoteport>
# with AWS_PROFILE and AWS_REGION in the environment. The name and optional env
# resolve the SSM parameters the module publishes. The script starts a task,
# waits for ECS Exec to be ready, then opens a port-forward from the local port
# to <host>:<remoteport> reached from inside the VPC. No inbound access, no host.
#
# The env is an optional segment for accounts that run more than one deployment
# environment. Leave it unset for a single-environment account.
#
# Requires the AWS CLI, the SSM session-manager-plugin, and jq.
set -euo pipefail

name=""
env_seg=""
forward=""
duration=""
region="${AWS_REGION:-us-east-1}"
keepalive=240

while getopts "n:e:p:d:r:k:" opt; do
  case "$opt" in
    n) name="$OPTARG" ;;
    e) env_seg="$OPTARG" ;;
    p) forward="$OPTARG" ;;
    d) duration="$OPTARG" ;;
    r) region="$OPTARG" ;;
    k) keepalive="$OPTARG" ;;
    *) echo "usage: $0 -n <name> [-e <env>] -p <localport>:<host>:<remoteport> [-d secs] [-r region] [-k keepalive_secs]" >&2; exit 2 ;;
  esac
done

if [[ -z "$name" || -z "$forward" ]]; then
  echo "error: -n and -p are required (-e is optional)" >&2
  exit 2
fi

local_port="${forward%%:*}"
rest="${forward#*:}"
remote_host="${rest%:*}"
remote_port="${rest##*:}"

if [[ -z "$local_port" || -z "$remote_host" || -z "$remote_port" ]]; then
  echo "error: -p must be <localport>:<host>:<remoteport>" >&2
  exit 2
fi

for tool in aws session-manager-plugin jq; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "error: ${tool} is required and was not found on PATH" >&2
    exit 1
  fi
done

export AWS_REGION="$region"

if [[ -n "$env_seg" ]]; then
  prefix="/${env_seg}/jumphost/${name}"
else
  prefix="/jumphost/${name}"
fi

get_param() {
  aws ssm get-parameter --name "$1" --query Parameter.Value --output text
}

cluster="$(get_param "${prefix}/cluster_name")"
task_def="$(get_param "${prefix}/task_definition")"
vpc_config="$(get_param "${prefix}/aws_vpc_configuration")"

echo "[jumphost] starting task on cluster ${cluster}" >&2

overrides=""
if [[ -n "$duration" ]]; then
  overrides="--overrides {\"containerOverrides\":[{\"name\":\"${task_def}\",\"environment\":[{\"name\":\"DURATION_IN_SECS\",\"value\":\"${duration}\"}]}]}"
fi

run_json="$(aws ecs run-task \
  --cluster "$cluster" \
  --task-definition "$task_def" \
  --launch-type FARGATE \
  --platform-version '1.4.0' \
  --enable-execute-command \
  --network-configuration "awsvpcConfiguration=${vpc_config}" \
  --propagate-tags TASK_DEFINITION \
  ${overrides})"

task_id="$(echo "$run_json" | jq -r '.tasks[0].taskArn' | cut -d'/' -f3)"
if [[ -z "$task_id" || "$task_id" == "null" ]]; then
  echo "error: run-task did not return a task. Response:" >&2
  echo "$run_json" | jq '.failures' >&2
  exit 1
fi

echo "[jumphost] waiting for task ${task_id} to reach RUNNING" >&2
aws ecs wait tasks-running --cluster "$cluster" --tasks "$task_id"

echo "[jumphost] waiting for the ExecuteCommandAgent" >&2
count=0
while true; do
  task="$(aws ecs describe-tasks --cluster "$cluster" --tasks "$task_id")"
  agent_status="$(echo "$task" | jq -r '.tasks[0].containers[0].managedAgents[]? | select(.name=="ExecuteCommandAgent") | .lastStatus')"
  if [[ "$agent_status" == "RUNNING" ]]; then
    break
  fi
  if [[ "$count" -gt 18 ]]; then
    echo "error: ExecuteCommandAgent not RUNNING after 3 minutes" >&2
    exit 1
  fi
  count=$((count + 1))
  sleep 10
done

runtime_id="$(aws ecs describe-tasks --cluster "$cluster" --tasks "$task_id" --query 'tasks[0].containers[0].runtimeId' --output text)"
target="ecs:${cluster}_${task_id}_${runtime_id}"

echo "[jumphost] forwarding localhost:${local_port} -> ${remote_host}:${remote_port} via ${target}" >&2

aws ssm start-session \
  --target "$target" \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters "host=${remote_host},portNumber=${remote_port},localPortNumber=${local_port}" &
session_pid=$!

cleanup() {
  trap - EXIT INT TERM
  if [[ -n "${session_pid:-}" ]]; then
    kill "$session_pid" 2>/dev/null || true
    wait "$session_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

if [[ "$keepalive" -gt 0 ]]; then
  echo "[jumphost] keepalive every ${keepalive}s to hold the SSM session open" >&2
  while kill -0 "$session_pid" 2>/dev/null; do
    sleep "$keepalive"
    kill -0 "$session_pid" 2>/dev/null || break
    # Open and close the forwarded port so the SSM session sees traffic and does
    # not hit the Session Manager idle timeout.
    ( : < "/dev/tcp/127.0.0.1/${local_port}" ) 2>/dev/null || true
  done
fi

wait "$session_pid"
