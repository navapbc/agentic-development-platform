"""Load local seed data."""

from __future__ import annotations

from scripts.config import get_settings


def main() -> int:
    settings = get_settings()
    print("[seed] allowlisted projects:", ", ".join(sorted(settings.allowed_projects())))
    print("[seed] allowlisted repos   :", ", ".join(sorted(settings.allowed_repos())))
    print("[seed] no ticket seeding (Jira canonical)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
