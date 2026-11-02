"""Point the rebar ticket store's remote at its S3 bucket.

The fetch URL names the reader profile and the push URL names the writer profile from
config/<env>/tickets.yaml, so git fetches the store under the read-only role and pushes under the
write role. The isolation contract in the same file is validated first.
"""

from __future__ import annotations

import argparse

from scripts.config import get_settings


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    settings = get_settings()

    from services.integration.tickets.rebar_config import load_rebar_backend_config
    from services.integration.tickets.store_config import load_tickets_store_config
    from services.integration.tickets.store_remote import configure_store_remote

    repo_root = load_rebar_backend_config(settings).repo_root
    remote = configure_store_remote(repo_root, load_tickets_store_config(settings))
    print(f"[rebar-store-remote] {repo_root} remote {remote.name}")
    print(f"[rebar-store-remote]   fetch {remote.fetch_url}")
    print(f"[rebar-store-remote]   push  {remote.push_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
