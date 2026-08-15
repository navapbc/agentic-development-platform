"""Load local seed data."""

from __future__ import annotations

from services.integration.github.credentials import allowed_repos
from services.integration.jira.credentials import allowed_projects

from scripts.config import get_settings


def main() -> int:
    settings = get_settings()
    print("[seed] allowlisted projects:", ", ".join(sorted(allowed_projects(settings))))
    print("[seed] allowlisted repos   :", ", ".join(sorted(allowed_repos(settings))))
    print("[seed] no ticket seeding (Jira canonical)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
