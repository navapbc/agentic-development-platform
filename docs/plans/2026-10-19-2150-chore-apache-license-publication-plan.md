---
title: Apache-2.0 Licensing and Publication Readiness - Plan
type: chore
date: 2026-10-19
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-plan-bootstrap
execution: code
---

# Apache-2.0 Licensing and Publication Readiness - Plan

## Goal Capsule

Objective. A reader of the public repository, a user of a built wheel, and an operator of a built image can each see that the platform is Nava PBC's work under Apache-2.0, which third-party terms their build accepts, how to report a vulnerability privately, and how contributions are licensed, and the repository contains no internal program or CMS account references.

Means. Add `LICENSE`, `NOTICE`, and `SECURITY.md`, wire the license files into packaging and every image this repository builds, document licensing in the README and CONTRIBUTING, scrub the jumphost module and `AGENTS.md`, and record the scan results, including the publication blockers they found (KTD1 through KTD7).

Authority. The Product Contract Key Decisions below were settled with the user and outrank any KTD. A KTD outranks a unit's Approach.

Stop conditions. Stop and report if a rerun of the secret scan finds a credential that is not a synthetic fixture, or if the U8 license inventory finds a component whose terms prohibit installation by an operator.

Execution profile. Mostly documentation, packaging, and image configuration. Proof is a wheel build, image builds, `terraform validate`, the unit suite, and repeated scans, not new unit tests.

Ownership. The implementer lands U1 through U8 as one ticket under the next `ADEVX` number. A repository admin performs the publication steps in Documentation and Operational Notes after merge.

---

## Product Contract

### Summary

Add the Apache License 2.0 text and a Nava PBC `NOTICE` that records the third-party terms an operator's build accepts. Declare the license through PEP 639 metadata and copy both files into the worker, repository-intelligence, and jumphost-base images. Add a README license section, an inbound licensing statement in CONTRIBUTING, and a `SECURITY.md` that routes reports through GitHub Private Vulnerability Reporting. Remove OSRE and CMS-specific wording from the current tree and record the secret, identifier, and license scan results.

### Problem Frame

`pyproject.toml` declares `Apache-2.0` but the repository has no `LICENSE` or `NOTICE`, so the declaration grants nothing a reader can verify. The images install proprietary and weak-copyleft components at build time, and no file tells an operator about those terms. The repository is private today and will be published with its full history. Before that happens it needs a reporting path for vulnerabilities that does not depend on an email inbox, a stated licensing basis for outside contributions, and a tree free of wording tied to internal Nava programs and CMS account conventions.

### Requirements

**Licensing**

- R1. The repository root carries the unmodified Apache License 2.0 text as `LICENSE`, including the appendix placeholder line.
- R2. `NOTICE` names Nava PBC as the copyright holder and lists each third-party component with obligations beyond Apache-2.0 that a build of this repository installs or invokes.
- R3. Package metadata declares `Apache-2.0` as a PEP 639 license expression, names Nava PBC as author, and ships `LICENSE` and `NOTICE` in every built distribution.
- R4. Every image this repository builds contains `LICENSE` and `NOTICE` and carries the `org.opencontainers.image.licenses="Apache-2.0"` label.
- R5. `README.md` ends with a License section that names Apache-2.0 and links `LICENSE` and `NOTICE`.
- R6. `CONTRIBUTING.md` states that a contribution submitted for inclusion is licensed under Apache-2.0 per section 5 of the license, with no sign-off or agreement step.

**Publication readiness**

- R7. The current tree contains no reference to OSRE, MPSM, MSPE, or CMS-specific account conventions. The `cms-ars` review profile name is exempt.
- R8. Secret, internal-identifier, and dependency-license scans cover every ref, and each finding has a recorded disposition.
- R9. `SECURITY.md` routes vulnerability reports through GitHub Private Vulnerability Reporting, defines an issue-based fallback that carries no vulnerability details, and names repository maintainers as the triage owners.

### Key Decisions

- Copyright is held by Nava PBC. Governs R2, R3. (session-settled: user-directed, chosen over "the agentic-dev-platform contributors" because the work is Nava's)
- Contributions are not gated by a DCO or a CLA. Governs R6. (session-settled: user-directed, chosen over rebar's `Signed-off-by` DCO because no sign-off step is wanted)
- The jumphost module stays and only its wording changes. Governs R7. (session-settled: user-directed, chosen over removing or rewriting the module because it is distinct enough from the OSRE module it was based on)
- Git history is published as it stands, including older references. Governs R7, R8. (session-settled: user-directed, chosen over a squashed or rewritten history because the historical references are acceptable)
- CMS-specific wording counts as a reference to remove. Governs R7. (session-settled: user-directed, chosen over limiting the scrub to OSRE, MPSM, and MSPE because CMS account conventions are equally internal)
- Images are built and run by each operator, locally or in the operator's own AWS account, and Nava does not distribute them. Governs R2, R4. (session-settled: user-directed, chosen over treating images as Nava-distributed artifacts because no image is published to a shared registry)
- `SECURITY.md` has no email contact and relies on Private Vulnerability Reporting. Governs R9. (session-settled: user-directed, chosen over `hello@navapbc.com` or `strata@navapbc.com` because no inbox is available)
- Triage is owned by the role "repository maintainers", not a named person. Governs R9. (session-settled: user-approved, chosen over naming an individual because the role survives staffing changes)

### Scope Boundaries

- Per-file license or SPDX headers are not added. Apache-2.0 does not require them and rebar does not use them.
- No DCO, CLA, `CODE_OF_CONDUCT.md`, `SUPPORT.md`, `GOVERNANCE.md`, or `MAINTAINERS.md` is added.
- `acli` provisioning in `docker/Dockerfile` and `scripts/fetch_acli.py` is unchanged.
- Git history is not rewritten.
- `docker/cbm-build.Dockerfile` is unchanged. Its build context is the upstream `codebase-memory-mcp` checkout and its output is that project's MIT binary, not an image of this repository.
- No CI job pins or verifies the `NOTICE` inventory. Considered and not built because the images are operator-built and the inventory is informational. A decision to publish images from Nava would change this call.
- No `.gitleaks.toml` or `.gitleaksignore` is added. Every finding is a synthetic fixture recorded below, and the repository runs no secret scanner in CI.
- The local absolute path `/Users/joerobinson/...` in `services/repository_intelligence/eval/scorecards/agentic-demo-service@0744a6e.2026-07-27.json` and `tests/fixtures/scip/agentic-demo-service@0744a6e.scip.json`, and the `joerobinson-nava` account in `tests/fixtures/webhooks/github_actions_workflow_run_completed.json`, are left as they are. They expose a username only, and the SCIP fixture is test input whose paths tests may resolve. A requirement to remove personal identifiers would change this call.

---

## Planning Contract

### Key Technical Decisions

- KTD1. **`NOTICE` follows rebar's layout.** A header with the project name, `Copyright 2026 Nava PBC`, and the Apache-2.0 statement, then a third-party section limited to components with obligations beyond Apache-2.0. Each entry gives the component, license, how it reaches a build, and the obligation, and states that the operator's build installs it and Nava does not distribute it. MIT, BSD, ISC, PSF, and Apache-2.0 dependencies are not listed, and Debian packages are covered by the base image's own copyright files. Chosen over an ASF-minimal `NOTICE` holding only the copyright line, because rebar is the model the project adopted and an operator gets one place to see which non-Apache terms a build accepts. The 2026 year is the year of the first commit, 2026-07-24.
- KTD2. **The `NOTICE` third-party inventory.** The entries are the ones the license scan found (see Scan Results).
  - Atlassian CLI (`acli`) is under Atlassian proprietary terms. `scripts/fetch_acli.py` downloads it into the worker image, and the Apache grant does not cover it.
  - `lmnr-claude-code-proxy` is `LicenseRef-Proprietary` under the Laminar Terms of Service. The `sandbox` extra pulls it in through `openhands-sdk` then `lmnr`, and platform code does not import it.
  - `psycopg` and `psycopg-binary` are `LGPL-3.0-only` and are core dependencies.
  - `func_timeout` is LGPL. Its PyPI metadata says LGPLv2 and the upstream repository says LGPL-3.0, so the entry records both. It arrives through `openhands-tools`.
  - `terraform-ls` 0.39.0 is `MPL-2.0` and is copied into the repository-intelligence image.
  - The Eclipse Temurin 21 JDK is GPL-2.0 with the Classpath Exception. It is copied from the `maven:3.9.11-eclipse-temurin-21` stage into the repository-intelligence image.
  - `certifi`, `tqdm`, and `orjson` contain `MPL-2.0` code.
  - Python package versions float with the resolver, so their entries carry no version. Pinned binaries carry the version the Dockerfile pins.
- KTD3. **PEP 639 metadata on hatchling 1.27 or later.** Set `license = "Apache-2.0"`, `license-files = ["LICENSE", "NOTICE"]`, and `authors = [{ name = "Nava PBC" }]`, and raise `[build-system] requires` to `hatchling>=1.27`, because older hatchling rejects a string `license-files`. Add no `License ::` classifier, because PEP 639 deprecates license classifiers once an expression is set. The author entry carries no email, because no address is available.
- KTD4. **Images copy the license files before the editable install.** Hatchling resolves `license-files` while building the editable wheel, so `LICENSE` and `NOTICE` must be in `/app` before `pip install -e` runs in `docker/Dockerfile` and `docker/repo-intelligence.Dockerfile`. `docker/jumphost-base/Dockerfile` receives the files through a BuildKit named context `repo` that `infra/bin/publish_jumphost_image.sh` passes as `--build-context repo=<repo root>`, and places them under `/usr/share/doc/agentic-dev-platform/`. This was chosen over moving the jumphost build context to the repository root, which would send the whole tree as context and change the Dockerfile's relative `COPY entrypoint.sh`. A label-only jumphost image was also rejected because it would not carry the files R4 requires.
- KTD5. **Contributions are licensed inbound-equals-outbound, and authorship is credited through git.** CONTRIBUTING gains a License section with three points. Apache-2.0 section 5 licenses any contribution intentionally submitted for inclusion under Apache-2.0 unless the contributor states otherwise. Authorship is recorded by the git author on each commit and shown in the GitHub contributors graph. A contributor who does not want section 5 to apply must say so before submitting. The alternatives were a CLA, which adds paperwork and an agreement bot, an `AUTHORS` file, which needs upkeep and duplicates git, and per-release credit in `CHANGELOG.md`, which can be added later without changing the licensing basis. Implementing this section is the recommendation for R6, and the user can replace it during review.
- KTD6. **Neutral replacement wording in the jumphost module.**
  - Drop the README sentence naming the OSRE `tf-jumphost-fargate` module.
  - Retitle "CMS and FedRAMP portability" to "Restricted-account portability".
  - In `variables.tf`, describe `permissions_boundary_arn` and `iam_path` in terms of an account's IAM delegation policy, without the `/delegatedadmin/developer/` example.
  - Keep variable names, types, defaults, and module interfaces unchanged.
  - Change the `AGENTS.md` commit example bullet to "Remove jump-host constants from Python defaults", the wording `CONTRIBUTING.md` already uses.
  - FedRAMP references elsewhere stay, because FedRAMP is a public federal program and not CMS-specific.
- KTD7. **`SECURITY.md` follows rebar's structure without an email channel.** The file has five sections: Reporting, What to include, What to expect, Supported versions, and Scope.
  - Reporting tells reporters not to use public issues, pull requests, or discussions, and points to **Security → Report a vulnerability** on the repository.
  - The fallback is a public issue titled "Security contact request" with no vulnerability details, which a maintainer converts into a draft advisory and to which the maintainer invites the reporter.
  - What to include lists the affected service or module, the release tag or commit SHA, the impact, reproduction steps or a proof of concept, and any suggested fix.
  - What to expect commits repository maintainers to acknowledge within 5 business days, report progress, coordinate the disclosure date, and credit the reporter on request.
  - Supported versions states the project is pre-1.0 and that fixes land on `main` and the latest tag only.
  - Scope excludes an operator's own AWS account configuration, IAM policies, and credentials, and routes vulnerabilities in LiteLLM, Temporal, OpenHands, `acli`, and other third-party components to those projects.

### Scan Results

The scans ran on 2026-10-19 against two sources. The first is the local clone, which holds every remote branch plus 654 local agent checkpoint refs under `refs/agents/`. The second is a `git clone --mirror` of `origin`, which holds the 32 `refs/heads` and 43 `refs/pull` refs that publication exposes. Eight of those pull refs, `refs/pull/31/head` through `refs/pull/38/merge`, point to commits absent from the local clone, so the mirror is the authoritative scan target. The local `main` matches `origin/main` at `b7360c8`.

| Scan | Coverage | Findings | Disposition |
| --- | --- | --- | --- |
| gitleaks `git --log-opts=--all` | 1,273 commits in the local clone and 991 commits in the mirror, including every pull ref | 41 in the clone and 4 in the mirror, all in `tests/unit/test_litellm_redaction.py` and `tests/unit/test_metrics_and_logs.py` | Synthetic fixtures. A sequential `ghp_0123…` token, the AWS documentation key `AKIAIOSFODNN7EXAMPLE`, and an `sk-abc…` sequence. No rotation needed |
| trufflehog `git` without verification | All branches of the local clone | The same GitHub fixtures, `tok-a` and `tok-b` URI tokens in `tests/integration/test_github_per_repo_credentials.py`, and Postgres URLs in `deploy/kubernetes/local/kustomization.yaml` and a former `tests/unit/test_config_host_rewrite.py` | Synthetic fixtures and local-stack default passwords. No rotation needed. Verification was disabled so no candidate value left the machine |
| Committed sensitive files | `git log --all` for `.env`, `*.pem`, `*.key`, `*.tfstate`, `*.tfvars` | None | Clean |
| Internal identifiers | Every line added across the mirror's history | AWS account IDs are only `000000000000`, `111122223333`, and `123456789012`, all documentation placeholders. The `10.0.x.x` addresses are CIDR examples in tfvars and tests | Clean. Personal paths are listed under Scope Boundaries |
| Client identifiers | `main` tree and every mirror ref | On `main`, the Massachusetts PFML repository name `archive-massgov-pfml` in `compose.override.yaml` and five test files, the Jira project key `PFMLPB` in `tests/integration/test_promotion_policy_rego.py`, and "MyMassGov" in two review eval fixtures. On unmerged branches `adevx-116` and later, the Jira site `lwd.atlassian.net`, the ticket `PSD-6948`, and the branch name `joeoakhart/PFMLPB-33758` | Open. See Open Questions |
| Ticket store branch | `refs/heads/tickets` on `origin` | The rebar ticket store, holding 89 tickets and 811 events. These include session logs that name PFML pilot work, and every event carries an employee's name and `@navapbc.com` email | Open. See Open Questions |
| Program references | Current tree and `origin/main` history | OSRE in `infra/terraform/bedrock-jumphost-fargate/README.md` and `AGENTS.md`. CMS in that README and `variables.tf`. OSRE and MPSM in four historical commits and one commit message | Tree references are removed by U5. History is kept per the Key Decisions |
| Python dependency licenses | 230 distributions from `.[all]` plus `pydantic-ai-slim[openai]`, resolved in a scratch venv | The KTD2 entries. `openhands-sdk`, `openhands-tools`, and `agent-client-protocol` report no license metadata but are MIT, MIT, and Apache-2.0 upstream | Recorded in `NOTICE` per KTD2. U8 repeats the Python inventory on Python 3.12 |
| Image tool licenses | Upstream repositories of every tool the Dockerfiles fetch | `terraform-ls` MPL-2.0 and Temurin GPL-2.0 with Classpath Exception. zoekt, scip, scip-java, scip-ruby, and scip-typescript are Apache-2.0. phpactor, ast-grep, scip-python, and codebase-memory-mcp are MIT | Recorded in `NOTICE` per KTD2 |

### Open Questions

- Client identifiers and the `tickets` branch block publication but do not block U1 through U8. The Key Decision that keeps history covers internal program names. It does not settle whether Massachusetts PFML identifiers, the `lwd.atlassian.net` Jira site, the ticket store, or an employee's name and email may become public. Before the visibility change, decide whether to delete the `tickets` branch and the stale `adevx-*` branches from `origin`, and whether to replace the `main` tree references with neutral fixture names. Deleting a branch does not remove commits already referenced by `refs/pull/*`, and only GitHub Support can purge those.

### Risks & Dependencies

- The license scan resolved dependencies under Python 3.14 while the images use Python 3.12. A different resolution could add or drop a non-permissive package. U8 repeats the Python inventory on Python 3.12 and reconciles the Python entries in `NOTICE`.
- Raising the hatchling floor applies to every editable install, including CI's `pip install -e ".[dev,all]"`. Build isolation fetches hatchling fresh, so no local pin conflicts.
- The jumphost build now depends on BuildKit named contexts. `infra/bin/publish_jumphost_image.sh` already uses `docker buildx build`, which supports `--build-context`.
- Private Vulnerability Reporting can only be enabled once the repository is public. Until then `SECURITY.md` describes a channel that is not yet active.

---

## Implementation Units

### U1. License and notice files

- **Goal:** Add the license text and the Nava PBC notice at the repository root.
- **Requirements:** R1, R2
- **Dependencies:** None
- **Files:** `LICENSE`, `NOTICE`
- **Approach:**
  1. Copy the Apache License 2.0 text byte-for-byte from `https://www.apache.org/licenses/LICENSE-2.0.txt`, leaving the appendix `Copyright [yyyy] [name of copyright owner]` line as published.
  2. Write `NOTICE` per KTD1 with the KTD2 entries, following the section layout of rebar's `NOTICE`.
- **Patterns to follow:** `https://github.com/navapbc/rebar/blob/main/NOTICE`
- **Test expectation:** none, because these are static legal texts.
- **Verification:** `LICENSE` matches the published text exactly. GitHub's license detection reports `Apache-2.0` after the branch is pushed. `NOTICE` passes the prose-rules gate in the Verification Contract.

### U2. Packaging metadata

- **Goal:** Declare the license through PEP 639 and ship both files in built distributions.
- **Requirements:** R3
- **Dependencies:** U1
- **Files:** `pyproject.toml`
- **Approach:** Apply KTD3 to the `[project]` table and `[build-system]`. Leave `[tool.hatch.build.targets.wheel]` unchanged, because hatchling places `license-files` under `.dist-info/licenses/` without a packages entry.
- **Test expectation:** none, because this is packaging metadata. Proof is the wheel build in the Verification Contract.
- **Verification:** A wheel built from the tree has `License-Expression: Apache-2.0`, `License-File: LICENSE`, `License-File: NOTICE`, and `Author: Nava PBC` in `METADATA`, and contains both files under `.dist-info/licenses/`. `pip install -e ".[dev,all]"` still succeeds.

### U3. License files in the Python service images

- **Goal:** Put both files and the OCI license label into the worker and repository-intelligence images.
- **Requirements:** R4
- **Dependencies:** U1, U2
- **Files:** `docker/Dockerfile`, `docker/repo-intelligence.Dockerfile`
- **Approach:** In each file, extend the `COPY pyproject.toml README.md …` line to include `LICENSE NOTICE` so they land in `/app` before `pip install -e` (KTD4). Add `LABEL org.opencontainers.image.licenses="Apache-2.0"` to the final stage. In `docker/repo-intelligence.Dockerfile` that is the `runtime` stage.
- **Execution note:** This unit is packaging only. Prove it by building the images and inspecting them.
- **Test expectation:** none, because Dockerfile changes have no unit coverage in this repository.
- **Verification:** Both compose-built images contain `/app/LICENSE` and `/app/NOTICE`, and `docker image inspect` shows the label. The editable install in each build succeeds with the new `license-files`.

### U4. License files in the jumphost-base image

- **Goal:** Put both files and the label into the operator-published jumphost image.
- **Requirements:** R4
- **Dependencies:** U1
- **Files:** `docker/jumphost-base/Dockerfile`, `infra/bin/publish_jumphost_image.sh`
- **Approach:**
  1. In the Dockerfile, add a `COPY --from=repo LICENSE NOTICE /usr/share/doc/agentic-dev-platform/` instruction and the OCI license label (KTD4).
  2. In the publish script, resolve the repository root the same way it already resolves `context_dir`, and pass it as `--build-context repo=<root>` on the existing `docker buildx build` line.
- **Patterns to follow:** The `context_dir` resolution in `infra/bin/publish_jumphost_image.sh`.
- **Test expectation:** none, because shell and Dockerfile changes have no unit coverage here.
- **Verification:** A local `docker buildx build --load` with the same named context produces an image with both files under `/usr/share/doc/agentic-dev-platform/` and the label. `bash -n infra/bin/publish_jumphost_image.sh` passes.

### U5. Remove program and CMS references

- **Goal:** Remove OSRE and CMS-specific wording from the current tree without changing the module interface.
- **Requirements:** R7
- **Dependencies:** None
- **Files:** `infra/terraform/bedrock-jumphost-fargate/README.md`, `infra/terraform/bedrock-jumphost-fargate/variables.tf`, `AGENTS.md`
- **Approach:** Apply KTD6. Touch only the description strings in `variables.tf`.
- **Test expectation:** none, because these are descriptions and prose with no behavior.
- **Verification:** A case-insensitive search of the tree for `osre`, `mpsm`, `mspe`, `delegatedadmin`, and `cms` returns only `cms-ars` occurrences. `terraform fmt -check` and `terraform validate` pass in `infra/terraform/bedrock-jumphost-fargate` and `infra/terraform/examples/private-bedrock`.

### U6. README and CONTRIBUTING licensing sections

- **Goal:** Document the license for readers and the inbound licensing basis for contributors.
- **Requirements:** R5, R6
- **Dependencies:** U1
- **Files:** `README.md`, `CONTRIBUTING.md`
- **Approach:**
  1. Append a `## License` section after Troubleshooting in `README.md` stating that the project is licensed under Apache-2.0, with links to `LICENSE` and `NOTICE`.
  2. Add a `## License` section to `CONTRIBUTING.md` before "Before you open a pull request" carrying KTD5.
- **Test expectation:** none, because this is documentation only.
- **Verification:** Both links resolve on GitHub. Both sections pass the prose-rules gate in the Verification Contract.

### U7. Security policy

- **Goal:** Give reporters a private path that needs no email address.
- **Requirements:** R9
- **Dependencies:** None
- **Files:** `SECURITY.md`
- **Approach:** Write the five sections in KTD7. Link the advisory form at `https://github.com/navapbc/agentic-development-platform/security/advisories/new`.
- **Patterns to follow:** `https://github.com/navapbc/rebar/blob/main/SECURITY.md` for section order and tone.
- **Test expectation:** none, because this is documentation only.
- **Verification:** GitHub's community profile lists the security policy once the branch is on the default branch. The file names no email address and no individual.

### U8. Rescan and reconcile

- **Goal:** Confirm the finished branch matches the Scan Results and the Python entries in `NOTICE`.
- **Requirements:** R2, R7, R8
- **Dependencies:** U1, U2, U5
- **Files:** `NOTICE` if the Python inventory differs.
- **Approach:**
  1. Make a fresh `git clone --mirror` of `origin` after the branch is pushed, so every `refs/heads` and `refs/pull` commit is present, and rerun gitleaks over it. The finding set must contain only the fixture files recorded above.
  2. In a throwaway `python:3.12-slim-trixie` container, install `.[all]`, `pydantic-ai-slim[openai]`, and `pip-licenses`, and diff the non-permissive set against the Python entries of KTD2.
  3. Rerun the reference search from U5.
  4. Add or remove Python entries in `NOTICE` when step 2 differs. The `acli`, `terraform-ls`, and Temurin entries are verified against the Dockerfiles and are never removed on the basis of `pip-licenses` output.
- **Test expectation:** none, because this is a verification pass.
- **Verification:** No secret finding outside the recorded fixture files exists. The Python non-permissive set equals the Python entries in `NOTICE`. The reference search returns only `cms-ars`.

---

## Verification Contract

| Gate | Command or check | Applies to |
| --- | --- | --- |
| Unit suite | `inv test` | U2, U5 |
| Lint | `ruff check .` and `ruff format --check .` | Whole change |
| Wheel metadata | `pip wheel . --no-deps -w <tmp>`, then read `METADATA` and the `.dist-info/licenses/` listing | U2 |
| Service images | `docker compose build` for the worker and repository-intelligence services, then check `/app/LICENSE`, `/app/NOTICE`, and the label | U3 |
| Jumphost image | `docker buildx build --build-context repo=. --load docker/jumphost-base`, then check the files and label | U4 |
| Terraform | `terraform fmt -check` and `terraform validate` in the changed module and the `private-bedrock` example | U5 |
| Secret scan | gitleaks `git --log-opts=--all` through the `zricethezav/gitleaks` image, run against a fresh `git clone --mirror` of `origin` | U8 |
| License inventory | `pip-licenses` in a throwaway `python:3.12-slim-trixie` container with `.[all]` installed | U8 |
| Prose rules | Search the new and changed markdown for em-dashes, en-dashes, space-hyphen-space, and semicolons in prose | U1, U5, U6, U7 |

---

## Definition of Done

- R1 through R9 hold on the branch.
- Every Verification Contract gate passes.
- The Python entries in `NOTICE` match the Python 3.12 inventory from U8.
- The diff touches only the files listed in U1 through U8. This plan is not committed, because it names the terms R7 removes from the tree, and it stays untracked or moves outside the repository before the change lands.
- No scratch venvs, scan reports, or temporary images remain in the repository tree.

---

## Documentation and Operational Notes

A repository admin completes these steps when the repository changes from private to public.

1. Resolve the client-identifier and `tickets` branch question under Open Questions.
2. Enable Private Vulnerability Reporting with `gh api -X PUT repos/navapbc/agentic-development-platform/private-vulnerability-reporting`.
3. Confirm that every repository maintainer has the admin role or the security manager role so advisory notifications reach them.
4. Confirm that GitHub's repository sidebar shows the Apache-2.0 license and the community profile shows the security policy.
