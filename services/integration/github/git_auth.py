"""Git HTTP credentials supplied per invocation, so no token enters a remote URL.

The empty `credential.helper` entry resets any helper inherited from system or global git
config, which would otherwise persist the token to a keychain or to `~/.git-credentials`.
"""

from __future__ import annotations

from dataclasses import dataclass

CREDENTIAL_ENV = "ADP_GIT_HTTP_TOKEN"
HTTP_USERNAME = "x-access-token"

_HELPER = (
    '!f() { test "$1" = get || exit 0; '
    f'printf "username={HTTP_USERNAME}\\npassword=%s\\n" "${CREDENTIAL_ENV}"; }}; f'
)


@dataclass(frozen=True, repr=False)
class GitAuth:
    """Credential material for one git invocation, carried in the environment only.

    The generated `repr` is replaced because it would render the token into any log line,
    exception message, or Temporal failure payload that formats the value.
    """

    token: str = ""

    def __repr__(self) -> str:
        return f"{type(self).__name__}(token={'set' if self.token else 'unset'})"

    def config_args(self) -> tuple[str, ...]:
        """Return `-c` arguments that place the helper ahead of the git subcommand.

        Configuration given this way reaches the running command without being written into
        the repository the command creates, which is what keeps a clone's config clean.
        """
        if not self.token:
            return ()
        return ("-c", "credential.helper=", "-c", f"credential.helper={_HELPER}")

    def env(self) -> dict[str, str]:
        """Return the environment one git invocation runs with.

        Prompt suppression applies with or without a token, because a git call that finds no
        credential would otherwise block on the terminal until the command timeout expires.
        """
        environment = {"GIT_TERMINAL_PROMPT": "0"}
        if not self.token:
            return environment
        return {**environment, CREDENTIAL_ENV: self.token}


NO_AUTH = GitAuth()


def no_auth_for(repo: str) -> GitAuth:
    """Resolve every repository to no credential, for backends that authenticate nothing."""
    return NO_AUTH
