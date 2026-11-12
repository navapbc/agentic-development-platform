"""Test-session hooks that keep hermetic settings resolution deterministic."""

from __future__ import annotations

import os

import dotenv.main

# load_dotenv() and dotenv_values() called without a path search upward from the calling
# module, and the in-repository virtualenv puts the developer's .env on that path. litellm
# and the OpenHands SDK both do this on import, and an OTEL endpoint read that way starts an
# exporter that fails against the unreachable collector at interpreter exit. Tests read
# configuration only from explicit files, so implicit discovery finds nothing.
dotenv.main.find_dotenv = lambda *args, **kwargs: ""

_ENV_SNAPSHOT: dict[str, str] = {}


def pytest_sessionstart(session):
    _ENV_SNAPSHOT.clear()
    _ENV_SNAPSHOT.update(os.environ)


def pytest_collection_finish(session):
    # A module imported during collection can still write to os.environ, which would leak
    # into every hermetic settings resolution. Restore the environment captured before
    # collection.
    for key in [name for name in os.environ if name not in _ENV_SNAPSHOT]:
        del os.environ[key]
    for key, value in _ENV_SNAPSHOT.items():
        os.environ[key] = value
