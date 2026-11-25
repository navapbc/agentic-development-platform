from __future__ import annotations

import plistlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts import local_renew
from scripts.local_renew import Session, Step, Tunnel
from tests.support import hermetic_settings

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
WARN = 900
STATIC = "the static access key of the AWS profile agentic-platform"
SSO = "the AWS profile agentic-platform signs in through SSO, which needs a browser"


@pytest.mark.parametrize(
    ("session", "step", "reason"),
    [
        (Session(True, NOW + timedelta(hours=6), STATIC), Step.NONE, "no renewal is needed"),
        (Session(True, NOW + timedelta(minutes=5), STATIC), Step.RENEW, "in 5 minutes"),
        (Session(True, NOW - timedelta(minutes=1), STATIC), Step.RENEW, "expired at"),
        (Session(False, None, STATIC), Step.RENEW, "Secret is missing"),
        (Session(True, None, STATIC), Step.NONE, "carry no expiry"),
        (Session(True, NOW + timedelta(minutes=5), "", SSO), Step.SKIP, "signs in through SSO"),
        (Session(False, None, "", SSO), Step.SKIP, "Run inv aws-login at a terminal"),
        (Session(True, NOW + timedelta(hours=6), "", SSO), Step.NONE, "no renewal is needed"),
    ],
    ids=[
        "fresh",
        "near-expiry",
        "expired",
        "missing",
        "no-expiry",
        "interactive-near-expiry",
        "interactive-missing",
        "interactive-fresh",
    ],
)
def test_the_session_is_renewed_only_when_near_expiry_and_renewable(session, step, reason):
    action = local_renew.session_action(session, now=NOW, warn_seconds=WARN)

    assert action.step is step
    assert reason in action.reason


@pytest.mark.parametrize(
    ("tunnel", "step"),
    [
        (Tunnel("jumphost", required=True, running=False), Step.START_TUNNEL),
        (Tunnel("jumphost", required=True, running=True), Step.NONE),
        (Tunnel("direct", required=False, running=False), Step.NONE),
    ],
    ids=["stopped", "running", "not-required"],
)
def test_the_tunnel_starts_only_when_required_and_stopped(tunnel, step):
    assert local_renew.tunnel_action(tunnel).step is step


def test_decide_orders_the_session_before_the_tunnel():
    actions = local_renew.decide(
        Session(True, NOW - timedelta(minutes=1), STATIC),
        Tunnel("ssh-bastion", required=True, running=False),
        now=NOW,
        warn_seconds=WARN,
    )

    assert [action.step for action in actions] == [Step.RENEW, Step.START_TUNNEL]


class _Credentials:
    def __init__(self, method: str, token: str = "") -> None:
        self.method = method
        self._token = token

    def get_frozen_credentials(self):
        return SimpleNamespace(token=self._token)


@pytest.mark.parametrize(
    ("settings", "credentials", "renewable", "reason"),
    [
        ({"aws_login_command": "kion favorite x --credential-process"}, None, True, ""),
        ({}, _Credentials("shared-credentials-file"), True, ""),
        ({}, _Credentials("sso"), False, "through SSO"),
        ({}, _Credentials("shared-credentials-file", token="t"), False, "session token"),
        ({}, _Credentials("custom-process"), False, "through custom-process"),
    ],
    ids=["login-command", "static-key", "sso", "fixed-token", "credential-process"],
)
def test_only_a_login_command_or_a_static_key_renews_without_a_person(
    settings, credentials, renewable, reason
):
    session = SimpleNamespace(get_credentials=lambda: credentials)

    source, blocker = local_renew.renewal_source(
        hermetic_settings(aws_profile="agentic-platform", **settings),
        {},
        session_factory=lambda profile: session,
    )

    assert bool(source) is renewable
    assert reason in blocker


def _run_with(monkeypatch, stored, source, tunnel_running, outcomes, *, reports=None):
    from scripts import aws_login, connectivity_providers

    reports = [] if reports is None else reports
    monkeypatch.setattr(local_renew, "report_host_action", lambda text: reports.append(text) or 1)
    monkeypatch.setattr(local_renew, "get_settings", hermetic_settings)
    monkeypatch.setattr(aws_login, "stored_record", stored)
    monkeypatch.setattr(aws_login, "login_environ", dict)
    monkeypatch.setattr(local_renew, "renewal_source", lambda settings, environ: source)
    monkeypatch.setattr(connectivity_providers, "tunnel_running", lambda: tunnel_running)
    performed = []

    def perform(step):
        def act():
            performed.append(step)
            return outcomes[step]

        return act

    monkeypatch.setattr(
        local_renew,
        "PERFORM",
        {step: (perform(step), failure) for step, (_, failure) in local_renew.PERFORM.items()},
    )
    return local_renew.run(), performed


def test_a_fresh_session_and_a_running_tunnel_perform_nothing(monkeypatch, capsys):
    fresh = {"Expiration": (datetime.now(UTC) + timedelta(hours=6)).isoformat()}

    reports = []
    code, performed = _run_with(
        monkeypatch, lambda settings: fresh, (STATIC, ""), True, {}, reports=reports
    )

    assert (code, performed, reports) == (0, [], [])
    assert capsys.readouterr().out.count("[local-renew]") == 2


FRESH = {"Expiration": (datetime.now(UTC) + timedelta(hours=6)).isoformat()}


@pytest.mark.parametrize(
    ("stored", "source", "outcomes", "reported", "manual"),
    [
        (lambda s: None, (STATIC, ""), {Step.RENEW: True}, "renewed the AWS session at", False),
        (lambda s: FRESH, (STATIC, ""), {Step.START_TUNNEL: True}, "restarted the", False),
        (lambda s: None, ("", SSO), {}, f"cannot renew the AWS session, because {SSO}.", True),
        (lambda s: None, (STATIC, ""), {Step.RENEW: False}, "could not renew", True),
        (lambda s: FRESH, (STATIC, ""), {Step.START_TUNNEL: False}, "could not restart", True),
    ],
    ids=["renewal", "tunnel-restart", "sso-profile", "failed-renewal", "failed-tunnel"],
)
def test_the_agent_reports_what_it_did_and_what_it_cannot_fix(
    monkeypatch, stored, source, outcomes, reported, manual
):
    reports = []
    running = Step.START_TUNNEL not in outcomes

    _run_with(monkeypatch, stored, source, running, outcomes, reports=reports)

    [report] = reports
    assert reported in report
    assert report.endswith(local_renew.MANUAL_FIX) is manual


def test_watch_checks_on_each_interval_and_survives_a_failed_check(capsys):
    checks, sleeps = [], []

    def check():
        checks.append(len(checks))
        if len(checks) == 1:
            raise RuntimeError("the cluster is unreachable")
        return 0

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        local_renew.watch(60, once=check, sleep=sleep)

    assert (len(checks), sleeps) == (3, [60, 60, 60])
    assert "the check raised RuntimeError" in capsys.readouterr().err


def test_changed_reports_skips_a_repeat_until_a_failed_write_or_the_refresh():
    outcomes, written = iter([2, 0, 3, 1]), []

    def record(text):
        written.append(text)
        return next(outcomes)

    report = local_renew.changed_reports(record, refresh_after=2)

    assert [report(text) for text in ("a", "a", "b", "b", "b", "b", "b")] == [2, 0, 0, 3, 0, 0, 1]
    assert written == ["a", "b", "b", "b"]


def test_a_failed_action_exits_non_zero_after_the_others_run(monkeypatch):
    code, performed = _run_with(
        monkeypatch,
        lambda settings: None,
        (STATIC, ""),
        False,
        {Step.RENEW: False, Step.START_TUNNEL: True},
    )

    assert (code, performed) == (1, [Step.RENEW, Step.START_TUNNEL])


def test_a_skipped_interactive_renewal_exits_zero(monkeypatch, capsys):
    code, performed = _run_with(monkeypatch, lambda settings: None, ("", SSO), True, {})

    assert (code, performed) == (0, [])
    assert "skipping the renewal" in capsys.readouterr().out


def test_an_unreadable_secret_fails_the_run_and_still_checks_the_tunnel(monkeypatch):
    from scripts import aws_login

    def unreachable(settings):
        raise aws_login.LoginError("the cluster is unreachable")

    code, performed = _run_with(
        monkeypatch, unreachable, (STATIC, ""), False, {Step.START_TUNNEL: True}
    )

    assert (code, performed) == (1, [Step.START_TUNNEL])


class _Launchctl:
    def __init__(self, bootstrap_code: int = 0) -> None:
        self.calls: list[list[str]] = []
        self._bootstrap_code = bootstrap_code

    def __call__(self, argv, **_):
        self.calls.append(argv)
        code = self._bootstrap_code if argv[1] == "bootstrap" else 0
        return SimpleNamespace(returncode=code, stderr="")


TOOLS = {
    "uv": "/opt/uv/bin/uv",
    "kubectl": "/usr/local/bin/kubectl",
    "aws": "/usr/local/bin/aws",
    "docker": "/Applications/Docker.app/Contents/Resources/bin/docker",
}


def test_install_writes_a_kept_alive_agent_that_checks_on_the_interval(tmp_path):
    launchctl = _Launchctl()

    code = local_renew.install(
        300, home=tmp_path, platform="darwin", which=TOOLS.get, run_command=launchctl, uid=501
    )

    agent = plistlib.loads(local_renew.plist_path(tmp_path).read_bytes())
    assert code == 0
    assert agent["Label"] == local_renew.LABEL
    assert agent["ProgramArguments"] == [
        "/opt/uv/bin/uv",
        "run",
        "inv",
        "local-renew",
        "--watch",
        "--interval",
        "300",
    ]
    assert agent["KeepAlive"] is True
    assert "StartInterval" not in agent
    assert Path(agent["WorkingDirectory"]) == local_renew.CHECKOUT_ROOT
    assert agent["EnvironmentVariables"]["PATH"].split(":") == [
        "/opt/uv/bin",
        "/usr/local/bin",
        "/Applications/Docker.app/Contents/Resources/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    assert agent["StandardOutPath"] == agent["StandardErrorPath"]
    assert Path(agent["StandardOutPath"]).parent == tmp_path / "Library" / "Logs"
    assert launchctl.calls == [
        ["launchctl", "bootout", f"gui/501/{local_renew.LABEL}"],
        ["launchctl", "bootstrap", "gui/501", str(local_renew.plist_path(tmp_path))],
    ]


def test_install_reports_a_failed_bootstrap(tmp_path):
    code = local_renew.install(
        600,
        home=tmp_path,
        platform="darwin",
        which=TOOLS.get,
        run_command=_Launchctl(bootstrap_code=5),
        uid=501,
    )

    assert code == 1


def test_install_refuses_without_uv_on_path(tmp_path):
    tools = {key: value for key, value in TOOLS.items() if key != "uv"}
    launchctl = _Launchctl()

    code = local_renew.install(
        600, home=tmp_path, platform="darwin", which=tools.get, run_command=launchctl, uid=501
    )

    assert (code, launchctl.calls) == (1, [])
    assert not local_renew.plist_path(tmp_path).exists()


def test_install_refuses_off_macos_and_points_at_the_cron_line(tmp_path, capsys):
    launchctl = _Launchctl()

    code = local_renew.install(
        600, home=tmp_path, platform="linux", which=TOOLS.get, run_command=launchctl, uid=501
    )

    assert (code, launchctl.calls) == (2, [])
    assert not local_renew.plist_path(tmp_path).exists()
    assert "cron line" in capsys.readouterr().err


def test_uninstall_unloads_the_job_and_removes_its_plist(tmp_path):
    target = local_renew.plist_path(tmp_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"")
    launchctl = _Launchctl()

    code = local_renew.uninstall(home=tmp_path, platform="darwin", run_command=launchctl, uid=501)

    assert code == 0
    assert launchctl.calls == [["launchctl", "bootout", f"gui/501/{local_renew.LABEL}"]]
    assert not target.exists()


class _AlertSlack:
    def __init__(self, monkeypatch) -> None:
        from services.orchestration import notifications, platform_escalation
        from services.orchestration.notifications import SlackPostRef

        self.events: list[tuple] = []

        def deliver(message, settings=None):
            self.events.append(("tenant", message.thread_ts, message.subject))
            if not message.thread_ts:
                message.posts.append(SlackPostRef("C0TENANT", "100.1"))
            return ["slack"]

        def post_admin(message, settings):
            self.events.append(("admin", message.thread_ts, message.subject))
            if not message.thread_ts:
                message.posts.append(SlackPostRef("", "200.1", admin=True))
            return True

        def reply(post, *, text, settings):
            self.events.append(("reply", post.ts, text))
            return True

        def edit(post, **kwargs):
            self.events.append(("edit", post.ts, kwargs["blocks"][-1].get("block_id")))
            return True

        monkeypatch.setattr(notifications, "deliver_alert", deliver)
        monkeypatch.setattr(notifications, "_post_admin_slack", post_admin)
        monkeypatch.setattr(notifications, "post_thread_reply", reply)
        monkeypatch.setattr(notifications, "update_slack_post", edit)
        monkeypatch.setattr(
            platform_escalation,
            "process_admin_destination",
            lambda: platform_escalation.AdminDestination(slack_channel="C0ADMIN"),
        )

    def of(self, kind: str) -> list[tuple]:
        return [event[1:] for event in self.events if event[0] == kind]


def _outage(monkeypatch, hop):
    from services.coordination.idempotency import InMemorySideEffectLedger
    from tests.support import InMemoryModelPathEpisodes, ModelPathStub, model_path_gate

    slack = _AlertSlack(monkeypatch)
    episodes = InMemoryModelPathEpisodes()
    path = ModelPathStub(hop)
    gate = model_path_gate(
        path,
        ledger=InMemorySideEffectLedger(),
        settings=hermetic_settings(tenant="acme", escalation_action_key="key"),
        episodes=episodes,
    )

    def report(text):
        return episodes.record_host_action(text, datetime.now(UTC))

    return slack, episodes, path, gate, report


async def test_ae19_the_agent_restarts_the_tunnel_reports_it_and_the_alert_resolves(monkeypatch):
    slack, episodes, path, gate, report = _outage(monkeypatch, "bedrock_path")
    await gate.admit(issue_key="ACME-1")
    episode = episodes.open_episodes["acme"]
    assert (episode.slack_ts, episode.admin_slack_ts) == ("100.1", "200.1")

    path.hop = ""
    reports = []
    _run_with(
        monkeypatch,
        lambda settings: FRESH,
        (STATIC, ""),
        False,
        {Step.START_TUNNEL: True},
        reports=reports,
    )
    [text] = reports
    report(text)
    await gate.sweep()

    replies = slack.of("reply")
    assert [ts for ts, _ in replies] == ["100.1", "200.1"]
    assert all("restarted the" in reply for _, reply in replies)
    assert slack.events.index(("reply", "200.1", text)) < slack.events.index(
        ("edit", "100.1", "adp-resolved")
    )
    assert sorted(slack.of("edit")) == [("100.1", "adp-resolved"), ("200.1", "adp-resolved")]
    assert [ts for ts, subject in slack.of("admin") if "recovered" in subject] == ["200.1"]
    assert (episodes.open_episodes, episodes.resolvers) == ({}, [""])


async def test_a_failure_the_agent_cannot_fix_posts_once_and_leaves_the_alert_open(monkeypatch):
    slack, episodes, path, gate, report = _outage(monkeypatch, "credentials")
    await gate.admit(issue_key="ACME-1")

    for _ in range(2):
        reports = []
        _run_with(monkeypatch, lambda settings: None, ("", SSO), True, {}, reports=reports)
        report(reports[0])
        path.next_pass()
        await gate.sweep()

    [(ts, text), (admin_ts, _)] = slack.of("reply")
    assert (ts, admin_ts) == ("100.1", "200.1")
    assert SSO in text and text.endswith(local_renew.MANUAL_FIX)
    assert slack.of("edit") == []
    assert "acme" in episodes.open_episodes


def test_the_host_report_reaches_the_published_postgres_port(monkeypatch, capsys):
    import database.engine
    from scripts.config import get_settings
    from services.orchestration import model_path

    secret = "".join(("pg", "-", "fixture", "-", "secret"))
    monkeypatch.delenv("PLATFORM_IN_CONTAINER", raising=False)
    monkeypatch.setenv(
        "DATABASE_URL", f"postgresql+asyncpg://platform:{secret}@postgres:5432/agent_platform"
    )
    monkeypatch.setenv("POSTGRES_HOST_PORT", "15432")
    get_settings.cache_clear()
    monkeypatch.setattr(local_renew, "get_settings", get_settings)
    urls = []

    class _Engine:
        async def dispose(self):
            return None

    def engine_for(url):
        urls.append(url)
        return _Engine()

    async def refuse(text, *, engine):
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(database.engine, "create_platform_engine", engine_for)
    monkeypatch.setattr(model_path, "record_host_action", refuse)
    try:
        assert local_renew.report_host_action("The host agent renewed the AWS session.") == 0
    finally:
        get_settings.cache_clear()

    assert urls == [f"postgresql+asyncpg://platform:{secret}@localhost:15432/agent_platform"]
    assert secret not in capsys.readouterr().err
