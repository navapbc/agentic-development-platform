# Architecture

These diagrams show the components that exist in this repository and the connections the code configures between them. Each node maps to a Compose service, a Kubernetes manifest, a Terraform resource, or a client in the Python code. Components that only appear in the [roadmap](../ROADMAP.md) are not drawn.

## High-level architecture

These two diagrams show the platform by functional group. Each block names a component and its purpose, and each edge shows a primary dependency. Ports, technology stacks, and secondary connections are in the detailed diagrams below.

### Local, high level

```mermaid
flowchart LR
    subgraph LOCAL["Local machine"]
        direction LR
        CLI["invoke (system CLI)<br/>local-up, local-down, db-migrate,<br/>k8s-deploy, tunnel-start, restart,<br/>smoke-test, ..."]

        subgraph COMPOSE["Docker Compose"]
            direction LR
            subgraph TC["Temporal cluster"]
                direction TB
                TEMPORAL["Temporal<br/>(orchestrator and UI)"]
                subgraph WORKERS["Workers"]
                    direction TB
                    WW["Workflow worker"]
                    AW["Activity worker<br/>(LangGraph graphs)"]
                    RW["Review worker"]
                end
            end
            RIS["Repository intelligence<br/>(code index)"]
            COORD["Coordination service<br/>(safety layer)"]
            OPA["Open Policy Agent<br/>(promotion policy)"]
            LF["Langfuse<br/>(LLM prompts and traces)"]
            OBS["Observability<br/>(traces and metrics)"]
            PG[("PostgreSQL<br/>(database)")]
            MINIO[("MinIO<br/>(object storage)")]
            LITELLM["LiteLLM<br/>(LLM proxy)"]
        end

        subgraph K8S["Kubernetes cluster"]
            direction LR
            subgraph NSS["Namespace agent-sandbox"]
                SBX["Agent sandboxes<br/>(OpenHands, one per execution)"]
            end
            subgraph NSP["Namespace agentic-dev-platform"]
                FRONT["Inbound webhook<br/>and MCP gateway"]
            end
        end
    end

    subgraph EXT["External services"]
        direction TB
        JIRA["Jira"]
        GH["GitHub"]
        subgraph AWS["AWS"]
            BR["Amazon Bedrock"]
        end
    end

    CLI --> COMPOSE
    CLI --> K8S
    TEMPORAL --> WW
    TEMPORAL --> AW
    TEMPORAL --> RW
    TEMPORAL --> PG
    AW --> OPA
    AW --> RIS
    AW --> COORD
    AW --> PG
    AW --> LITELLM
    AW --> SBX
    AW --> JIRA
    AW --> GH
    WORKERS --> OBS
    RW --> BR
    COORD --> PG
    SBX --> LITELLM
    LITELLM --> LF
    LITELLM --> BR
    LF --> MINIO
    OBS --> MINIO
    GH --> FRONT
    FRONT --> TEMPORAL
    FRONT --> PG
    FRONT --> JIRA
    PG ~~~ GH
    PG ~~~ JIRA
    MINIO ~~~ BR

    classDef default fill:#ffffff,stroke:#232F3E,color:#232F3E
    style LOCAL fill:#F3F3F3,stroke:#666666
    style COMPOSE fill:#CFE2F3,stroke:#6FA8DC
    style TC fill:#D9D2E9,stroke:#8E7CC3
    style WORKERS fill:#D9D2E9,stroke:#8E7CC3,stroke-dasharray:4 4
    style K8S fill:#CFE2F3,stroke:#6FA8DC
    style NSP fill:#A4C2F4,stroke:#3C78D8
    style NSS fill:#A4C2F4,stroke:#3C78D8
    style EXT fill:#ffffff,stroke:#232F3E
    style AWS fill:#FCE5CD,stroke:#E69138
```

- Temporal dispatches workflow and activity tasks to the workers, which hold long-poll connections to it. The inbound webhook signals Temporal when GitHub reports a closed pull request, and the MCP gateway reads from PostgreSQL, GitHub, and Jira.
- Every worker exports traces and metrics to Observability. LiteLLM sends its generation traces to Langfuse.

### AWS, high level

```mermaid
flowchart LR
    subgraph LOCAL["Local machine"]
        CLI["invoke (system CLI)<br/>temporal-deploy, temporal-ui,<br/>tunnel-start, publish images"]
    end

    subgraph AWS["AWS account"]
        direction LR
        subgraph VPC["VPC"]
            direction LR
            JH["Bedrock jump-host<br/>(operator access)"]
            subgraph TC["Temporal cluster, ECS on Fargate"]
                direction TB
                TUI["Temporal UI<br/>(dashboard)"]
                TEMPORAL["Temporal<br/>(orchestrator)"]
            end
            subgraph DATA["Data tier"]
                direction TB
                AURORA[("Aurora PostgreSQL<br/>(Temporal database)")]
                CACHE[("Valkey cache and ClickHouse EFS<br/>(provisioned, no client yet)")]
            end
            NET["Network controls<br/>(VPC endpoints, NAT, DNS firewall)"]
        end
        subgraph REG["Regional services"]
            direction TB
            BR["Amazon Bedrock"]
            ECR["ECR<br/>(container images)"]
            SM["Secrets Manager<br/>(credentials and TLS)"]
            OPS["CloudTrail, CloudWatch,<br/>Systems Manager"]
            S3[("S3<br/>(ticket store, Langfuse, logs)")]
        end
    end

    subgraph EXT["External services"]
        direction TB
        GHA["GitHub Actions<br/>(ticket sync)"]
        JIRA["Jira"]
    end

    CLI --> OPS
    OPS --> JH
    CLI --> SM
    CLI --> ECR
    JH --> TUI
    JH --> NET
    NET --> BR
    TUI --> TEMPORAL
    TEMPORAL --> AURORA
    ECR --> TEMPORAL
    SM --> TEMPORAL
    SM -.-> CACHE
    TEMPORAL --> OPS
    GHA --> S3
    GHA --> JIRA

    classDef default fill:#ffffff,stroke:#232F3E,color:#232F3E
    style LOCAL fill:#F3F3F3,stroke:#666666
    style AWS fill:#FCE5CD,stroke:#E69138
    style VPC fill:#ffffff,stroke:#8C4FFF
    style TC fill:#D9D2E9,stroke:#8E7CC3
    style DATA fill:#E6F6F7,stroke:#00A4A6,stroke-dasharray:4 4
    style REG fill:#FFF2E5,stroke:#E69138,stroke-dasharray:4 4
    style EXT fill:#ffffff,stroke:#232F3E
```

- Only the Temporal cluster, the Bedrock jump-host, and the shared infrastructure run in AWS. The workers, LiteLLM, Langfuse, repository intelligence, and observability run locally.
- The dashed edge from Secrets Manager is the rotation of the Valkey RBAC user. No workload in AWS connects to the Valkey cache or the ClickHouse file system yet.
- Systems Manager starts the jump-host session, and the Temporal tasks send their logs to CloudWatch.

## Local environment

The local stack runs in Docker Compose ([compose.yaml](../compose.yaml), [compose.override.yaml](../compose.override.yaml)) and on the Docker Desktop Kubernetes cluster ([deploy/kubernetes/local](../deploy/kubernetes/local)). The cluster runs the workloads that run on EKS in AWS, and no pod mounts a host directory. Each block names the function it performs. Blocks built in this repository list the technologies that implement them. Off-the-shelf products are named without a technology list. Edge labels are the destination port. A cluster workload reaches a Compose service on the port Compose publishes on the host. Dashed edges are optional paths that the default `.env.example` values leave disabled.

```mermaid
flowchart LR
    subgraph EXT["External services"]
        direction TB
        GITHUB["GitHub"]
        JIRA["Jira Cloud"]
        ALERTS["Slack and email alerts"]
    end

    subgraph AWSX["AWS account"]
        direction TB
        JUMPHOST["Bedrock Jump-Host<br/><i>ECS Fargate task</i>"]
        BEDROCK["Amazon Bedrock<br/><i>Claude models</i>"]
    end

    subgraph HOST["Developer workstation"]
        direction TB
        DEV["Developer<br/><i>invoke, Docker Compose,<br/>kubectl, AWS CLI</i>"]
        SSMTUNNEL["Bedrock Tunnel<br/><i>AWS CLI,<br/>Session Manager plugin</i>"]
        WHFWD["Webhook Forwarder<br/><i>GitHub CLI</i>"]
        REPOS[("Target Repositories<br/><i>host checkouts</i>")]

        subgraph COMPOSE["Docker Compose"]
            direction LR

            subgraph TEMPORALG["Durable Workflow Engine"]
                direction TB
                TEMPORAL["Temporal Server"]
                TEMPORALUI["Temporal Web UI"]
            end

            ORCH["Workflow Orchestrator<br/><i>Python, Temporal SDK</i>"]

            subgraph PLATSVC["Platform Services"]
                direction TB
                COORD["Coordination Service<br/><i>Python, SQLAlchemy</i>"]
                RIS["Repository Intelligence Service<br/><i>Python, Zoekt, SCIP, ast-grep,<br/>terraform-ls, phpactor,<br/>codebase-memory-mcp</i>"]
                RIW["Repository Index Worker<br/><i>Python, zoekt-index, scip-python,<br/>scip-typescript, scip-java, scip-ruby</i>"]
            end

            subgraph SETUP["One-shot Setup Jobs"]
                direction TB
                MIGRATE["Database Migrations<br/><i>Alembic</i>"]
                TSCHEMA["Temporal Schema Setup<br/><i>Temporal admin-tools</i>"]
                SCHED["Schedule Bootstrap<br/><i>Python, Temporal SDK</i>"]
            end

            subgraph DATA["Shared Data Stores"]
                direction TB
                POSTGRES[("PostgreSQL")]
                MINIO[("MinIO Object Storage")]
                IDXVOL[("Repository Index Volume")]
                REGISTRY[("Container Registry")]
            end

            subgraph LANGFUSEG["LLM Tracing"]
                direction TB
                LANGFUSE["Langfuse Web"]
                LANGFUSEW["Langfuse Worker"]
                CLICKHOUSE[("ClickHouse")]
                VALKEY[("Valkey")]
            end

            subgraph TELEM["Telemetry"]
                direction TB
                OTEL["OpenTelemetry Collector"]
                TEMPO["Grafana Tempo"]
                PROM["Prometheus"]
                GRAFANA["Grafana"]
            end
        end

        subgraph K8S["Docker Desktop Kubernetes"]
            direction TB
            KAPI["Kubernetes API"]
            subgraph NSPLAT["Namespace agentic-dev-platform"]
                direction TB
                subgraph ACTG["Agent Activity Worker pod"]
                    direction TB
                    ACT["Agent Activities<br/><i>Python, Temporal SDK,<br/>OpenHands SDK, Kubernetes client,<br/>rebar, Atlassian CLI, git</i>"]
                    LANGGRAPH["CI Repair and Conflict Resolution<br/><i>LangGraph, rebar</i>"]
                    OPA["Open Policy Agent<br/><i>loopback sidecar</i>"]
                end
                subgraph REVIEWG["Review Gate Worker pod"]
                    direction TB
                    REVIEW["Review Gate Worker<br/><i>Python, Temporal SDK,<br/>rebar, Pydantic AI</i>"]
                    RRELAY["Bedrock Relay<br/><i>socat sidecar</i>"]
                end
                subgraph LITELLMG["LiteLLM pod"]
                    direction TB
                    LITELLM["LiteLLM Model Gateway"]
                    LRELAY["Bedrock Relay<br/><i>socat sidecar</i>"]
                end
                WORKSPACES[("Workspace Volume<br/><i>checkouts, repository copies,<br/>gate engines, ticket store</i>")]
                AWSCRED[("AWS Credentials Secret")]
                WEBHOOK["Inbound Webhook Receiver<br/><i>Python</i>"]
                MCP["MCP Gateway<br/><i>Python, MCP SDK</i>"]
            end
            subgraph NSSBX["Namespace agent-sandbox"]
                OPENHANDS["OpenHands Coding Sandbox<br/><i>pod per execution</i>"]
                GATEPOD["External Review Gate<br/><i>pod per gate, config-selected<br/>AI CLI or SARIF scanner</i>"]
            end
        end
    end

    %% Workflow engine
    ORCH -->|"7233"| TEMPORAL
    ACT -->|"7233"| TEMPORAL
    REVIEW -->|"7233"| TEMPORAL
    ORCH -->|"5432"| POSTGRES
    ACT -->|"5432"| POSTGRES
    REVIEW -->|"5432"| POSTGRES
    TEMPORAL -->|"5432"| POSTGRES
    TEMPORALUI -->|"7233"| TEMPORAL

    %% Agent activities
    ACT -->|"8130"| COORD
    ACT -->|"8120"| RIS
    ACT -->|"8181 loopback"| OPA
    ACT -->|"4000"| LITELLM
    LANGGRAPH -->|"4000"| LITELLM
    ACT -->|"HTTPS"| KAPI
    ACT -->|"8000 pod IP"| OPENHANDS
    ACT -->|"443"| GITHUB
    ACT -->|"443"| JIRA
    ACT -.->|"443, 587"| ALERTS
    ACT -->|"9090"| MINIO
    ACT -->|"volume"| WORKSPACES

    %% Review gates
    REVIEW -->|"443 loopback"| RRELAY
    REVIEW -.->|"4000"| LITELLM
    REVIEW -.->|"9090"| MINIO
    REVIEW -->|"volume, read-only"| WORKSPACES
    REVIEW -->|"credential_process"| AWSCRED

    %% Sandboxes
    KAPI -->|"creates pod"| OPENHANDS
    KAPI -.->|"creates pod"| GATEPOD
    OPENHANDS -->|"4000"| LITELLM
    GATEPOD -.->|"4000"| LITELLM
    GATEPOD -.->|"5001"| REGISTRY

    %% Platform services
    COORD -->|"5432"| POSTGRES
    COORD -->|"7233"| TEMPORAL
    RIS -->|"volume"| IDXVOL
    RIW -->|"volume"| IDXVOL
    RIS -->|"bind mount, read-only"| REPOS
    RIW -->|"bind mount, read-only"| REPOS

    %% Model access
    LITELLM -->|"443 loopback"| LRELAY
    LITELLM -->|"credential_process"| AWSCRED
    LRELAY -->|"8444"| SSMTUNNEL
    RRELAY -->|"8444"| SSMTUNNEL
    SSMTUNNEL -->|"443 SSM session"| JUMPHOST
    JUMPHOST -->|"443"| BEDROCK

    %% Inbound and MCP
    GITHUB -->|"443"| WHFWD
    WHFWD -->|"8140"| WEBHOOK
    WEBHOOK -->|"7233"| TEMPORAL
    WEBHOOK -->|"5432"| POSTGRES
    MCP -->|"5432"| POSTGRES
    MCP -->|"8120"| RIS
    MCP -->|"443"| GITHUB
    MCP -->|"443"| JIRA

    %% Setup jobs
    MIGRATE -->|"5432"| POSTGRES
    TSCHEMA -->|"5432"| POSTGRES
    TSCHEMA -->|"7233"| TEMPORAL
    SCHED -->|"7233"| TEMPORAL

    %% Tracing and telemetry
    ORCH -->|"4317"| OTEL
    ACT -->|"4317"| OTEL
    REVIEW -->|"4317"| OTEL
    ACT -->|"3000"| LANGFUSE
    LITELLM -->|"3000"| LANGFUSE
    LANGFUSE -->|"5432"| POSTGRES
    LANGFUSEW -->|"5432"| POSTGRES
    LANGFUSE -->|"8123, 9000"| CLICKHOUSE
    LANGFUSEW -->|"8123, 9000"| CLICKHOUSE
    LANGFUSE -->|"6379"| VALKEY
    LANGFUSEW -->|"6379"| VALKEY
    LANGFUSE -->|"9000"| MINIO
    LANGFUSEW -->|"9000"| MINIO
    OTEL -->|"4317"| TEMPO
    TEMPO -->|"9000"| MINIO
    PROM -->|"8889"| OTEL
    GRAFANA -->|"3200"| TEMPO
    GRAFANA -->|"9090"| PROM

    %% Developer access
    DEV -->|"3001"| TEMPORALUI
    DEV -->|"3000"| LANGFUSE
    DEV -->|"3300"| GRAFANA
    DEV -->|"HTTPS"| KAPI
    DEV -->|"starts"| SSMTUNNEL
    REPOS -->|"inv repos-sync, inv fetch-results"| KAPI

    classDef compute fill:#ED7100,stroke:#232F3E,color:#fff
    classDef database fill:#C925D1,stroke:#232F3E,color:#fff
    classDef storage fill:#7AA116,stroke:#232F3E,color:#fff
    classDef network fill:#8C4FFF,stroke:#232F3E,color:#fff
    classDef ai fill:#01A88D,stroke:#232F3E,color:#fff
    classDef mgmt fill:#E7157B,stroke:#232F3E,color:#fff
    classDef security fill:#DD344C,stroke:#232F3E,color:#fff
    classDef ext fill:#ffffff,stroke:#232F3E,color:#232F3E

    class TEMPORAL,TEMPORALUI,ORCH,ACT,REVIEW,COORD,RIS,RIW,MIGRATE,TSCHEMA,SCHED,WEBHOOK,MCP,KAPI,JUMPHOST compute
    class LANGGRAPH,OPENHANDS,GATEPOD,LITELLM,BEDROCK ai
    class POSTGRES,CLICKHOUSE,VALKEY database
    class MINIO,IDXVOL,WORKSPACES,REGISTRY,REPOS storage
    class LRELAY,RRELAY,SSMTUNNEL,WHFWD network
    class LANGFUSE,LANGFUSEW,OTEL,TEMPO,PROM,GRAFANA mgmt
    class OPA,AWSCRED security
    class GITHUB,JIRA,ALERTS,DEV ext
```

Component roles:

| Block | Role |
|---|---|
| Durable Workflow Engine | Temporal persists workflow state in PostgreSQL and dispatches tasks to the workers on the `agent-platform` and `agent-platform-review` task queues. |
| Workflow Orchestrator | Runs the intake, plan review, implementation, branch sync, pull request, draft pull request, and reap workflows. It executes no side effects. |
| Agent Activities | Runs every side effect the workflows request: planning model calls, repository context, work claims, code generation in the OpenHands sandbox, git push, pull request creation, Jira updates, policy checks, and alerts. |
| CI Repair and Conflict Resolution | LangGraph state graphs that run in-process in the activity worker. CI repair calls the model gateway. Conflict resolution resolves trivial merge conflicts without a model. |
| Open Policy Agent | Evaluates the pull request promotion policy. It runs as a sidecar of the activity worker, bound to the pod's loopback, so no Service exposes it. |
| Review Gate Worker | Runs the plan, code, security, and acceptance review gates with rebar against the execution's checkout on the workspace volume. Native mode calls Bedrock directly through the relay sidecar. Proxied mode calls the model gateway. |
| Coordination Service | Holds work claims, sequencing, and side-effect idempotency in PostgreSQL, and checks claim owners against Temporal. Every mutating call needs the shared bearer token. |
| Repository Intelligence Service | Builds code context packages for planning from Zoekt search, SCIP symbol indexes, ast-grep, language servers, and codebase-memory-mcp. |
| Repository Index Worker | Polls the target repositories for new commits and rebuilds the Zoekt and SCIP indexes on the shared volume. |
| LiteLLM Model Gateway | Maps logical model names to Bedrock Claude models and sends generation traces to Langfuse with credential-shaped strings redacted. Compose services and host tasks reach it on host port 4000 through a `LoadBalancer` Service. |
| Bedrock Relay and Bedrock Tunnel | Each relay answers on the `bedrock-runtime` hostname on its pod's loopback and forwards to the host tunnel, which forwards over an SSM session to the jump-host in AWS. |
| AWS Credentials Secret | Holds the credentials `inv aws-login` writes. LiteLLM and the review worker reread it through `credential_process` before the credentials they hold expire. |
| Workspace Volume | Holds the execution checkouts, the copies of the local-backend repositories and local gate engines that `inv repos-sync` sends, and the rebar ticket store seeded from MinIO. `inv fetch-results` brings the agent branches back to the host checkouts. |
| Inbound Webhook Receiver | Receives GitHub pull request events from the GitHub CLI forwarder and signals the matching Temporal workflow when a pull request closes. |
| MCP Gateway | Serves read-only Jira, GitHub, evidence, and repository context tools over the Model Context Protocol to clients that present its bearer token. |
| OpenHands Coding Sandbox | One agent-server pod per execution that performs the implementation stage. The activity worker reaches it on its pod IP with a per-sandbox session key. |
| External Review Gate | One pod per configured external gate, running an AI CLI reviewer or a SARIF scanner. |
| LLM Tracing | Langfuse records model generations from LiteLLM and from the workers. |
| Telemetry | The collector receives worker traces, metrics, and logs. Tempo stores traces, Prometheus scrapes metrics, and Grafana displays both. |

Notes on the local diagram:

- `BEDROCK_CONNECTIVITY_PROVIDER` selects the Bedrock path. The diagram shows the default `jumphost` provider. The `ssh-bastion` provider replaces the SSM session with an SSH tunnel, and the `direct` provider removes the tunnel and the relays.
- MinIO holds the Langfuse and Tempo buckets. The worker paths to MinIO are the rebar ticket store, used when `TICKET_BACKEND=rebar`, and the evidence Object Lock bucket, used when `EVIDENCE_OBJECT_STORE_ENABLED=true`. The cluster reaches MinIO on its published port 9090.
- The External Review Gate path is inactive until `config/local/gate_engine.yaml` enables an engine. The Container Registry runs only under the `registry` Compose profile.
- The `agent-sandbox` network policies limit sandbox egress to DNS and LiteLLM. The Docker Desktop cluster enforces them through its kindnet CNI, which `inv smoke-test` confirms.
- The MCP Gateway has no client in this repository.

## AWS environment

The AWS footprint is the [private-bedrock](../infra/terraform/examples/private-bedrock) composition root plus the separately applied [bootstrap](../infra/terraform/bootstrap) root. Every module in the composition root is gated by a `create_*` variable, and the diagram shows the composition with every gate enabled. Each block names the function it performs. The italic line names the AWS service for managed resources and lists the technologies for components built in this repository. Off-the-shelf products such as Temporal are named without a technology list. Edge labels are the destination port. Edges labelled with an action are AWS control-plane calls or event deliveries that have no fixed port. Dashed edges are optional paths.

```mermaid
flowchart LR
    subgraph OUT["Outside AWS"]
        direction TB
        DEV["Developer Workstation<br/><i>invoke, AWS CLI,<br/>Session Manager plugin, Docker</i>"]
        GHA["Ticket Sync Workflow<br/><i>GitHub Actions, rebar bridge,<br/>git-remote-s3, Atlassian CLI</i>"]
        JIRA["Jira Cloud"]
        NET(("Internet"))
    end

    subgraph CLOUD["AWS Cloud"]
        subgraph REGION["Region us-east-1"]

            subgraph VPC["VPC 10.0.0.0/16"]
                direction TB
                DNSFW["Outbound DNS Filter<br/><i>Route 53 Resolver DNS Firewall</i>"]
                S3GW["S3 Gateway Endpoint"]

                subgraph PUB["Public subnet"]
                    NAT["Outbound NAT<br/><i>EC2 t4g.nano, Amazon Linux 2023,<br/>iptables, or NAT Gateway</i>"]
                    IGW["Internet Gateway"]
                end

                subgraph APP["Application subnet"]
                    NLB["Temporal Frontend Load Balancer<br/><i>internal Network Load Balancer</i>"]
                    TSRV["Temporal Server"]
                    VPCE["AWS Service Endpoints<br/><i>Interface VPC endpoints</i>"]
                end

                subgraph MGMT["Management subnets, two AZs"]
                    JHOST["Bedrock Jump-Host<br/><i>Alpine, ECS Exec,<br/>Fargate task on its own cluster</i>"]
                    ALB["Temporal Web UI Load Balancer<br/><i>internal Application Load Balancer</i>"]
                    TUI["Temporal Web UI"]
                    TADM["Temporal Schema and Namespace Setup<br/><i>Temporal admin-tools</i>"]
                    ROT["Secret Rotation Function<br/><i>AWS Lambda, Python 3.13, boto3</i>"]
                end

                subgraph DATA["Data subnets, two AZs"]
                    AURORA[("Temporal Database<br/><i>Aurora PostgreSQL 16,<br/>Serverless v2</i>")]
                    CACHE[("Cache<br/><i>ElastiCache for Valkey 9.1</i>")]
                    EFS[("ClickHouse File System<br/><i>Amazon EFS</i>")]
                end
            end

            subgraph PLATFORM["Container Platform"]
                direction TB
                ECSC["Platform Cluster<br/><i>Amazon ECS, Fargate,<br/>Fargate Spot</i>"]
                ECR["Container Image Registry<br/><i>Amazon ECR</i>"]
            end

            BEDROCK["Model Inference<br/><i>Amazon Bedrock, Claude models</i>"]

            subgraph SECG["Secrets, Keys, and Identity"]
                direction TB
                SM["Platform Secrets<br/><i>AWS Secrets Manager</i>"]
                KMS["Encryption Keys<br/><i>AWS KMS</i>"]
                ACM["TLS Certificates<br/><i>AWS Certificate Manager</i>"]
                IAM["Ticket Store Roles<br/><i>IAM, GitHub OIDC</i>"]
            end

            subgraph OPSG["Operations"]
                direction TB
                SSM["Operator Access and Runbooks<br/><i>Systems Manager</i>"]
                EB["Rotation and Certificate Events<br/><i>Amazon EventBridge</i>"]
                SNS["Certificate Expiry Alerts<br/><i>Amazon SNS</i>"]
                CWL["Logs<br/><i>CloudWatch Logs</i>"]
                CT["API Audit Trail<br/><i>AWS CloudTrail</i>"]
            end

            subgraph S3G["Object Storage, Amazon S3"]
                direction TB
                S3TICK[("Rebar Ticket Store")]
                S3LF[("Langfuse Blob Store")]
                S3LB[("Load Balancer Access Logs")]
                S3EXEC[("Jump-Host Session Logs")]
                S3CT[("Audit Trail Logs")]
            end

            subgraph BOOT["Bootstrap, separate apply"]
                direction TB
                S3STATE[("Terraform State")]
                S3ACC[("Bucket Access Logs")]
            end
        end
    end

    %% Operator access
    DEV -->|"443"| SSM
    DEV -->|"443"| ECSC
    DEV -->|"443"| ECR
    DEV -->|"443"| SM
    DEV -.->|"443"| ACM
    DEV -.->|"443"| S3TICK
    SSM -->|"starts session"| JHOST
    JHOST -->|"443"| VPCE
    JHOST -->|"443"| ALB
    JHOST -->|"443"| S3GW

    %% Temporal control plane
    ALB -->|"8080"| TUI
    TUI -->|"7233"| NLB
    TADM -->|"7233"| NLB
    NLB -->|"7233"| TSRV
    TSRV -->|"6933-6939, 7233-7239"| TSRV
    TSRV -->|"5432"| AURORA
    TADM -->|"5432"| AURORA
    ECSC -.->|"runs"| TSRV
    ECSC -.->|"runs"| TUI
    ECSC -.->|"runs"| TADM
    TSRV -->|"443"| VPCE
    TUI -->|"443"| VPCE
    TADM -->|"443"| VPCE
    TSRV -->|"443"| S3GW
    ACM -->|"certificate"| NLB
    ACM -->|"certificate"| ALB
    NLB -->|"access logs"| S3LB
    ALB -->|"access logs"| S3LB

    %% AWS service endpoints
    VPCE -->|"443"| BEDROCK
    VPCE -->|"443"| SSM
    VPCE -->|"443"| ECR
    VPCE -->|"443"| SM
    VPCE -->|"443"| KMS
    VPCE -->|"443"| CWL
    VPCE -->|"443"| AURORA
    VPCE -->|"443"| CACHE
    S3GW -->|"443"| ECR
    S3GW -->|"443"| S3EXEC

    %% Secret rotation and restarts
    SM -->|"rotation schedule"| ROT
    ROT -->|"443"| VPCE
    SM -->|"rotation event"| EB
    EB -->|"starts runbook"| SSM
    SSM -->|"force deployment"| TSRV
    ACM -->|"expiry event"| EB
    EB -->|"publish"| SNS

    %% Egress
    APP -->|"443, 587"| NAT
    MGMT -->|"443, 587"| NAT
    NAT -->|"443, 587"| IGW
    IGW --> NET
    DNSFW -->|"query logs"| CWL

    %% Ticket store
    GHA -->|"443"| IAM
    GHA -->|"443"| S3TICK
    GHA -->|"443"| JIRA
    S3TICK -->|"data events"| CT
    CT -->|"log delivery"| S3CT

    %% Bucket access logging
    S3TICK -.->|"access logs"| S3ACC
    S3LF -.->|"access logs"| S3ACC
    S3LB -.->|"access logs"| S3ACC
    S3EXEC -.->|"access logs"| S3ACC
    S3CT -.->|"access logs"| S3ACC
    S3STATE -.->|"access logs"| S3ACC

    classDef compute fill:#ED7100,stroke:#232F3E,color:#fff
    classDef database fill:#C925D1,stroke:#232F3E,color:#fff
    classDef storage fill:#7AA116,stroke:#232F3E,color:#fff
    classDef network fill:#8C4FFF,stroke:#232F3E,color:#fff
    classDef ai fill:#01A88D,stroke:#232F3E,color:#fff
    classDef mgmt fill:#E7157B,stroke:#232F3E,color:#fff
    classDef security fill:#DD344C,stroke:#232F3E,color:#fff
    classDef ext fill:#ffffff,stroke:#232F3E,color:#232F3E

    class TSRV,TUI,TADM,JHOST,ROT,ECSC,ECR compute
    class AURORA,CACHE database
    class EFS,S3TICK,S3LF,S3LB,S3EXEC,S3CT,S3STATE,S3ACC storage
    class NLB,ALB,VPCE,S3GW,IGW,NAT,DNSFW network
    class BEDROCK ai
    class SSM,EB,SNS,CWL,CT mgmt
    class SM,KMS,ACM,IAM security
    class DEV,GHA,JIRA,NET ext

    style CLOUD fill:#ffffff,stroke:#232F3E,stroke-width:2px
    style REGION fill:#ffffff,stroke:#00A4A6,stroke-dasharray:5 5
    style VPC fill:#ffffff,stroke:#8C4FFF,stroke-width:2px
    style PUB fill:#F2F6E8,stroke:#7AA116
    style APP fill:#E6F6F7,stroke:#00A4A6
    style MGMT fill:#E6F6F7,stroke:#00A4A6
    style DATA fill:#E6F6F7,stroke:#00A4A6
```

Component roles:

| Block | Role |
|---|---|
| Developer Workstation | Publishes the jump-host and Temporal images, stores the Temporal TLS material, runs the Temporal deploy, and opens the jump-host session for the Bedrock tunnel and the Temporal Web UI. |
| Ticket Sync Workflow | A manually dispatched GitHub Actions workflow that assumes the ticket store writer role through GitHub OIDC and reconciles the rebar ticket store with Jira project ADP. |
| Outbound DNS Filter | Resolves only allowlisted domains for the VPC, namely AWS, GitHub, and Atlassian by default, and logs every query. |
| Outbound NAT | Forwards ports 443 and 587 from the application and management subnets to the internet. The data subnets have no default route. |
| Temporal Frontend Load Balancer | Fronts the Temporal frontend on 7233. It terminates TLS with an ACM certificate when one is set and otherwise passes TCP through to the server. |
| Temporal Server | Runs every Temporal service in each task. The image adds a Go health probe and a start script that renders the TLS and PostgreSQL configuration. |
| Temporal Web UI and its Load Balancer | Serves the Temporal Web UI over HTTPS to the operator CIDRs and the jump-host security group. |
| Temporal Schema and Namespace Setup | One-shot ECS tasks that apply the Temporal schema to Aurora and register the namespace. |
| Bedrock Jump-Host | An ephemeral task that holds an ECS Exec session open for a fixed TTL. The developer forwards local ports through it to the Bedrock endpoint and to the Temporal Web UI load balancer. |
| AWS Service Endpoints | Private endpoints for Bedrock Runtime, SSM Messages, ECR, CloudWatch Logs, KMS, Secrets Manager, the RDS Data API, and ElastiCache. |
| Temporal Database | Holds the `temporal` and `temporal_visibility` databases and the per-role platform database credentials. |
| Secret Rotation Function | Rotates the per-role database credentials through the RDS Data API and the Valkey RBAC user through the ElastiCache API. |
| Operator Access and Runbooks | Session Manager for the jump-host, Parameter Store for the jump-host settings, and the Automation runbook that force-deploys a service after its secret rotates. |
| Rotation and Certificate Events | Starts the restart runbook after a rotation and publishes ACM certificate expiry warnings to SNS. |
| Rebar Ticket Store | The S3 remote of the rebar tickets branch, read and written through `git-remote-s3` under separate reader and writer roles. |
| API Audit Trail | Records management events and the object-level events on the ticket store bucket. |

Notes on the AWS diagram:

- No platform worker, LiteLLM, Langfuse, or observability service runs in AWS. The Cache, the ClickHouse File System, and the Langfuse Blob Store have no client yet, and the per-role database credentials other than Temporal's have no reader.
- No workload in AWS sends traffic through the Outbound NAT yet. The Temporal and rotation security groups admit only the endpoints, S3, and the database, and the jump-host reaches the internet only through optional caller-supplied CIDRs.
- The Temporal server service is created with `desired_count = 0` and `ignore_changes` on the count and task definition. `inv temporal-deploy` runs the schema task, moves the service to the registered task definition at the configured count, and then runs the namespace task.
- The [evidence-store](../infra/terraform/evidence-store) module provisions an S3 Object Lock bucket but is not wired into either root module, so it is not drawn.
