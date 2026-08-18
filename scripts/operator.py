"""Resolve the operating-system account that runs an operator command."""

from __future__ import annotations


def local_operator() -> str:
    import os

    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:  # noqa: BLE001
        import getpass

        return getpass.getuser()
