# Security Policy

## Reporting a vulnerability

Do not report security vulnerabilities through public GitHub issues, pull requests, or discussions.

Report a vulnerability privately through GitHub Private Vulnerability Reporting. Open the repository's **Security** tab and select **Report a vulnerability**, or go to <https://github.com/navapbc/agentic-development-platform/security/advisories/new>. The report creates a draft security advisory that only you and the repository maintainers can see.

If private reporting is unavailable, open a public issue titled `Security contact request` that contains no details about the vulnerability. A repository maintainer will open a draft security advisory and invite you to it, and you can share the details there.

## What to include

Include as much of the following as you can:

- The affected service or module.
- The release tag or commit SHA.
- A description of the issue and its impact.
- Reproduction steps or a proof of concept.
- Any suggested fix.

## What to expect

- The repository maintainers acknowledge a report within 5 business days.
- The maintainers keep you informed of progress while they assess and fix the issue.
- The maintainers coordinate a disclosure date with you and publish the advisory once a fix is available. Allow a reasonable window for remediation before any public disclosure.
- The advisory credits you if you want to be credited.

## Supported versions

The project is pre-1.0. Security fixes land on `main` and in the latest release tag. Older tags do not receive fixes, so upgrade to the latest release.

## Scope

This policy covers the code in this repository. Each operator runs the platform locally or in the operator's own AWS account, so the following are out of scope:

- The operator's AWS account configuration, IAM policies, and network setup.
- Credentials and secrets that an operator supplies.
- Vulnerabilities in third-party components such as LiteLLM, Temporal, OpenHands, Langfuse, and the Atlassian CLI. Report those to the respective projects.
