"""Publish the platform prompt library to Langfuse."""

from __future__ import annotations

from services.prompts import seed_prompts


def main() -> int:
    seeded = seed_prompts()
    for version in seeded:
        print(f"[seed] {version}")
    print(f"seeded {len(seeded)} prompts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
