"""The enterprise login check runs without the sandbox when the sandbox cannot start.

Owner decision (enterprise smoke test P9): the boot check and the login gate's
watch may run ``kiro-cli --version`` and ``whoami`` unsandboxed, so an
enterprise build boots and stays open on a host with no user namespaces. Only
the "sandbox unavailable" cause stops being fatal; everything else still fails
closed, and nothing but those two fixed probes leaves the sandbox.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import enterprise_boot
from kiro_crew.enterprise_gate import EnterpriseGate, LoginWatch
from kiro_crew.kiro_prerequisite import ProcessResult
from kiro_crew.kiro_prerequisite_probe import _UNSANDBOXED_CALLER as UNSANDBOXED_CALLER
from kiro_crew.kiro_prerequisite_process import (
    _run_login_probe_unsandboxed as run_login_probe_unsandboxed,
)

_REFUSED = ProcessResult(
    ok=False,
    error="sandbox refused",
    sandbox_failure=(
        "no_backend",
        "unshare(CLONE_NEWUSER) EPERM",
        "enable user namespaces",
    ),
)


@pytest.fixture
def enterprise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kiro_crew.edition.EDITION", "enterprise")


@pytest.fixture(autouse=True)
def ungoverned_floor(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``sandbox.min_level`` floor unless a test sets one (no platform boots here)."""
    _floor_monkeypatch.setattr("kiro_crew.sandbox._governance_sandbox_floor", lambda: None)


class _Host:
    """A host whose sandbox refuses every spawn, and whose CLI answers ``whoami``."""

    def __init__(self, tmp_path: Path, whoami: ProcessResult, version: ProcessResult) -> None:
        from kiro_prerequisite_helpers import _make_executable

        from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

        self.executable = tmp_path / ".local" / "bin" / "kiro-cli"
        _make_executable(self.executable)
        self.sandboxed: list[list[str]] = []
        self.unsandboxed: list[tuple[str, tuple[str, ...]]] = []
        self.audit: list[dict[str, Any]] = []
        self.whoami = whoami
        self.version = version

        async def sandboxed(_command: str, args: list[str], **_kwargs: Any) -> ProcessResult:
            self.sandboxed.append(args)
            return _REFUSED

        async def unsandboxed(command: str, args: tuple[str, ...], **_kwargs: Any) -> ProcessResult:
            self.unsandboxed.append((command, args))
            return self.version if args == ("--version",) else self.whoami

        async def audit(**event: Any) -> None:
            self.audit.append(event)

        self.service = KiroPrerequisiteService(
            platform_name="linux",
            environ={
                "HOME": str(tmp_path),
                "PATH": f"{self.executable.parent}:/usr/bin:/bin",
            },
            home=tmp_path,
            process_runner=sandboxed,
            login_probe_runner=unsandboxed,
            audit_writer=audit,
        )


def _host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    whoami: ProcessResult | None = None,
    version: ProcessResult | None = None,
) -> _Host:
    host = _Host(
        tmp_path,
        whoami or ProcessResult(ok=True, returncode=0),
        version or ProcessResult(ok=True, returncode=0),
    )
    monkeypatch.setattr(
        "kiro_crew.kiro_prerequisite.KiroPrerequisiteService",
        lambda **_kwargs: host.service,
    )
    return host


@pytest.mark.asyncio
async def test_a_no_sandbox_host_boots_and_the_gate_stays_open(
    enterprise: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _host(tmp_path, monkeypatch)
    service = await enterprise_boot.require_kiro_login()
    assert service is host.service and service.authenticated

    gate = EnterpriseGate(closed_for=())
    watch = LoginWatch(gate, service)
    assert await watch.check() is True
    assert gate.is_open
    watch.sync()
    assert gate.is_open

    # Readiness still says the sandbox is unavailable: no session starts, and
    # the setup-complete marker is left for a sandboxed probe to write.
    assert not service.initial_setup_complete
    status = service._status
    assert status.sandbox_unavailable and status.authenticated and not status.ready
    # Boot and the watch tick each ran exactly the two fixed probes unsandboxed.
    fixed = [
        (str(host.executable), ("--version",)),
        (str(host.executable), ("whoami",)),
    ]
    assert host.unsandboxed == fixed * 2
    # The sandbox was asked first, and only for --version: nothing else (no
    # whoami, no spec or acp check) left it.
    assert host.sandboxed == [["--version"], ["--version"]]
    unsandboxed_events = [e for e in host.audit if e["caller"] == UNSANDBOXED_CALLER]
    assert len(unsandboxed_events) == 8
    assert all(
        "sandbox unavailable (no_backend)" in e["error"]
        for e in unsandboxed_events
        if e["outcome"] == "invoked"
    )


@pytest.mark.asyncio
async def test_signed_out_still_refuses_boot(
    enterprise: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _host(
        tmp_path,
        monkeypatch,
        whoami=ProcessResult(ok=False, returncode=1, output="error: Not logged in"),
    )
    with pytest.raises(SystemExit):
        await enterprise_boot.require_kiro_login()
    assert "no Kiro sign-in was found" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_a_whoami_timeout_refuses_boot(
    enterprise: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _host(tmp_path, monkeypatch, whoami=ProcessResult(ok=False, timed_out=True))
    with pytest.raises(SystemExit):
        await enterprise_boot.require_kiro_login()
    err = capsys.readouterr().err
    assert "whoami timed out" in err
    assert "no Kiro sign-in was found" not in err


@pytest.mark.asyncio
async def test_a_version_timeout_refuses_boot(
    enterprise: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    host = _host(tmp_path, monkeypatch, version=ProcessResult(ok=False, timed_out=True))
    with pytest.raises(SystemExit):
        await enterprise_boot.require_kiro_login()
    assert "did not answer in time" in capsys.readouterr().err
    assert [args for _cmd, args in host.unsandboxed] == [("--version",)]


@pytest.mark.asyncio
async def test_a_failed_version_keeps_the_sandbox_as_the_reason(
    enterprise: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _host(tmp_path, monkeypatch, version=ProcessResult(ok=False, returncode=1))
    with pytest.raises(SystemExit):
        await enterprise_boot.require_kiro_login()
    err = capsys.readouterr().err
    assert "could not be checked (unshare(CLONE_NEWUSER) EPERM)" in err
    assert "Reinstall" not in err


@pytest.mark.asyncio
async def test_an_unclear_whoami_closes_the_gate(
    enterprise: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _host(tmp_path, monkeypatch)
    assert await enterprise_boot.require_kiro_login() is host.service
    host.whoami = ProcessResult(ok=False, returncode=2, output="garbled")
    gate = EnterpriseGate(closed_for=())
    assert await LoginWatch(gate, host.service).check() is False
    assert not gate.is_open


@pytest.mark.asyncio
async def test_a_governed_sandbox_floor_keeps_the_probe_sandboxed(
    enterprise: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from kiro_crew import sandbox

    monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: "strict")
    host = _host(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        await enterprise_boot.require_kiro_login()
    assert "Fix the sandbox error above" in capsys.readouterr().err
    assert host.unsandboxed == []


@pytest.mark.asyncio
async def test_an_unreadable_floor_keeps_the_probe_sandboxed(
    enterprise: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import sandbox

    def broken() -> str:
        raise RuntimeError("policy unreadable")

    monkeypatch.setattr(sandbox, "_governance_sandbox_floor", broken)
    host = _host(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        await enterprise_boot.require_kiro_login()
    assert host.unsandboxed == []


@pytest.mark.asyncio
async def test_a_relative_cli_override_reaches_the_probe_absolute(
    enterprise: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _host(tmp_path, monkeypatch)
    monkeypatch.chdir(host.executable.parent)
    host.service._environ["KIROCREW_KIRO_BIN"] = "./kiro-cli"
    assert await enterprise_boot.require_kiro_login() is host.service
    assert {cmd for cmd, _args in host.unsandboxed} == {str(host.executable)}


@pytest.mark.asyncio
async def test_oss_never_runs_the_unsandboxed_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _host(tmp_path, monkeypatch)
    status = await host.service.snapshot()
    assert status["sandbox_unavailable"] is True
    assert status["authenticated"] is False
    assert host.unsandboxed == []


@pytest.mark.skipif(os.name != "posix", reason="Windows skips the sandbox wrap entirely")
@pytest.mark.asyncio
async def test_the_sandboxed_spawn_still_refuses_without_a_sandbox(
    enterprise: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent-side chokepoint is untouched: no sandbox, no spawn."""
    from kiro_crew import kiro_prerequisite_process as process
    from kiro_crew.sandbox import SandboxUnavailableError

    async def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise SandboxUnavailableError("no sandbox", kind="no_backend", detail="EPERM", remedy="")

    monkeypatch.setattr(process, "_prepare_sandboxed_spawn", refuse)
    spawned: list[Any] = []

    async def no_spawn(*args: Any, **_kwargs: Any) -> Any:
        spawned.append(args)
        raise AssertionError("spawned without a sandbox")

    monkeypatch.setattr(process.asyncio, "create_subprocess_exec", no_spawn)
    result = await process._run_process("/usr/bin/true", [], env={}, timeout_secs=5)
    assert result.sandbox_failure is not None and not result.ok
    assert spawned == []


# --------------------------------------------------------------------------- #
# The unsandboxed spawn itself


@pytest.mark.asyncio
async def test_only_the_fixed_argvs_run() -> None:
    with pytest.raises(ValueError):
        await run_login_probe_unsandboxed("/usr/bin/true", ("login",), env={}, timeout_secs=5)


def _script(tmp_path: Path, body: str) -> str:
    path = tmp_path / "kiro-cli"
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX only")
@pytest.mark.asyncio
async def test_a_real_probe_answers(tmp_path: Path) -> None:
    exe = _script(tmp_path, 'echo "kiro-cli 9.9.9 $1"')
    result = await run_login_probe_unsandboxed(
        exe, ("--version",), env={"PATH": "/usr/bin:/bin"}, timeout_secs=20
    )
    assert result.ok and "kiro-cli 9.9.9 --version" in result.output


@pytest.mark.skipif(os.name != "posix", reason="POSIX only")
@pytest.mark.asyncio
async def test_a_helper_left_behind_does_not_hold_the_probe_open(tmp_path: Path) -> None:
    from kiro_crew.kiro_prerequisite_process import _host_can_reap

    if not _host_can_reap():
        pytest.skip("host cannot reap survivors (no pidfd)")
    # A launcher that starts a helper and returns, like a credential helper.
    exe = _script(tmp_path, "(sleep 30 >/dev/null 2>&1 &) ; echo signed-in")
    result = await run_login_probe_unsandboxed(
        exe, ("whoami",), env={"PATH": "/usr/bin:/bin"}, timeout_secs=10
    )
    assert result.ok and not result.timed_out
    assert "signed-in" in result.output


@pytest.mark.skipif(os.name != "posix", reason="POSIX only")
@pytest.mark.asyncio
async def test_a_real_probe_that_hangs_times_out(tmp_path: Path) -> None:
    exe = _script(tmp_path, "sleep 30")
    result = await run_login_probe_unsandboxed(
        exe, ("whoami",), env={"PATH": "/usr/bin:/bin"}, timeout_secs=0.5
    )
    assert result.timed_out and not result.ok


@pytest.mark.skipif(os.name != "posix", reason="POSIX only: Windows refuses first")
@pytest.mark.asyncio
async def test_a_relative_path_is_refused() -> None:
    result = await run_login_probe_unsandboxed("kiro-cli", ("whoami",), env={}, timeout_secs=5)
    assert not result.ok and "absolute" in result.error
