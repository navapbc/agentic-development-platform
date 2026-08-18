from __future__ import annotations

import os

from scripts.operator import local_operator


def test_local_operator_matches_the_os_account():
    identity = local_operator()
    assert identity
    try:
        import pwd

        assert identity == pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError):
        import getpass

        assert identity == getpass.getuser()


def test_local_operator_ignores_a_spoofed_actor_environment(monkeypatch):
    try:
        import pwd

        expected = pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError):
        return
    monkeypatch.setenv("USER", "someone-else")
    monkeypatch.setenv("LOGNAME", "someone-else")
    assert local_operator() == expected
