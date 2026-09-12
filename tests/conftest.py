"""Test-session hooks that keep hermetic settings resolution deterministic."""

from __future__ import annotations

import os

_ENV_SNAPSHOT: dict[str, str] = {}


def pytest_sessionstart(session):
    _ENV_SNAPSHOT.clear()
    _ENV_SNAPSHOT.update(os.environ)


def pytest_collection_finish(session):
    # Importing litellm during collection calls load_dotenv, which copies the
    # deployment's .env into os.environ. A repository configured for a rebar and
    # local-git deployment would then leak those backends into every hermetic
    # settings resolution. Restore the environment captured before collection.
    for key in [name for name in os.environ if name not in _ENV_SNAPSHOT]:
        del os.environ[key]
    for key, value in _ENV_SNAPSHOT.items():
        os.environ[key] = value
