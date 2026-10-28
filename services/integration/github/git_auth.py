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


@dataclass(frozen=True)
class GitAuth:
    """Credential material for one git invocation, carried in the environment only."""

    token: str = ""

    def config_args(self) -> tuple[str, ...]:
        """Return `-c` arguments that place the helper ahead of the git subcommand.

        Configuration given this way reaches the running command without being written into
        the repository the command creates, which is what keeps a clone's config clean.
        """
        if not self.token:
            return ()
        return ("-c", "credential.helper=", "-c", f"credential.helper={_HELPER}")

    def env(self) -> dict[str, str]:
        if not self.token:
            return {}
        return {CREDENTIAL_ENV: self.token, "GIT_TERMINAL_PROMPT": "0"}


NO_AUTH = GitAuth()
