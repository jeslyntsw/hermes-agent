"""Tests for the dry-run/read-only System Alert watchdog.

Loads ``scripts/system_alert_watchdog.py`` from the repo by file path (mirroring
``tests/scripts/test_release_acp_registry.py``) and exercises the alert
classification, Telegram formatting, dedupe/cooldown, off-hours routing, and
secret-redaction behaviour. Everything runs in dry-run: no alert is ever sent.

Acceptance covered:
  * Simulated CRITICAL alert is rendered + "delivered" (simulated, never sent).
  * WARNING is local-only in working hours (never produces a Telegram message).
  * INFO alert is local-only (recorded, never produces a Telegram message).
  * Dedupe within a run + cooldown across runs suppress repeats.
  * Off-hours: CRITICAL still delivers; WARNING stays local-only.
  * No secret leakage into the Telegram message or the evidence log.
  * Critical-only active transport: with mode=active AND the enable gate set, a
    CRITICAL alert is sent through an injected (fake) transport — and the
    rendered message it receives is already redacted.
  * Warning/info are NEVER sent over the transport, even active+enabled+in-hours.
  * Dry-run and active-but-disabled never invoke the transport (no real send).
  * Transport failure (returned error or raised exception) is recorded as a
    failed, un-simulated send with a redacted, body-free error and is not
    cooldown-suppressed (so a failed critical retries next run).
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

_MODULE_NAME = "_system_alert_watchdog_under_test"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        _MODULE_NAME,
        Path(__file__).resolve().parents[2] / "scripts" / "system_alert_watchdog.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclasses with ``from __future__ import annotations`` resolve postponed
    # annotations via sys.modules[cls.__module__]; register before exec so the
    # frozen-annotation lookup finds the module (py3.9 dataclasses crash
    # otherwise — same gotcha documented in test_kanban_checkpoint_handoff.py).
    sys.modules[_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(_MODULE_NAME, None)
        raise
    return module


@pytest.fixture(scope="module")
def saw():
    return _load_module()


# A fixed in-hours / off-hours pair (NZ local). 2026-06-01 is a Monday.
def _in_hours(saw) -> int:
    dt = datetime(2026, 6, 1, 10, 0, tzinfo=saw.NZ_TZ)  # Mon 10:00 NZ
    return int(dt.timestamp())


def _off_hours(saw) -> int:
    dt = datetime(2026, 6, 1, 23, 0, tzinfo=saw.NZ_TZ)  # Mon 23:00 NZ
    return int(dt.timestamp())


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_work_hours_boundaries(saw):
    assert saw.is_jeslyn_working_hours(_in_hours(saw)) is True
    assert saw.is_jeslyn_working_hours(_off_hours(saw)) is False
    # Saturday is always off-hours.
    sat = int(datetime(2026, 6, 6, 10, 0, tzinfo=saw.NZ_TZ).timestamp())
    assert saw.is_jeslyn_working_hours(sat) is False


def test_classify_thresholds(saw):
    assert saw.classify(96.0, warn=85.0, crit=95.0) == saw.CRITICAL
    assert saw.classify(90.0, warn=85.0, crit=95.0) == saw.WARNING
    assert saw.classify(50.0, warn=85.0, crit=95.0) == saw.INFO


# ---------------------------------------------------------------------------
# Simulated critical alert
# ---------------------------------------------------------------------------


def test_simulated_critical_delivers_but_is_not_sent(saw, tmp_path):
    now = _in_hours(saw)
    alert = saw.make_alert(saw.CRITICAL, "disk:/", "disk usage high on /",
                           "97.0% used", source="disk", now=now)
    result = saw.run([alert], now=now, mode=saw.MODE_DRY_RUN, state=saw.CooldownState())

    delivered = result.delivered
    assert [d.alert.key for d in delivered] == ["disk:/"]
    delivery = result.deliveries["disk:/"]
    # Simulated: rendered but never actually sent.
    assert delivery["simulated"] is True
    assert delivery["sent"] is False
    msg = result.telegram_message
    assert "CRITICAL" in msg and "disk usage high" in msg
    assert saw.SEVERITY_EMOJI[saw.CRITICAL] in msg


class FakeTransport:
    """Records every call so tests can assert exactly when (and whether) the
    watchdog attempted a real send. Never contacts Telegram."""

    def __init__(self, result=None, raises: Exception | None = None):
        self.calls: list[str] = []
        self._result = result if result is not None else {"sent": True, "transport": "telegram"}
        self._raises = raises

    def __call__(self, message: str) -> dict:
        self.calls.append(message)
        if self._raises is not None:
            raise self._raises
        return self._result


# ---------------------------------------------------------------------------
# Critical-only active transport (injected fake — no real Telegram send)
# ---------------------------------------------------------------------------


def test_active_enabled_critical_sends_via_injected_transport(saw, monkeypatch):
    now = _in_hours(saw)
    monkeypatch.setenv(saw.ACTIVE_ENABLED_ENV, "1")
    transport = FakeTransport({"sent": True, "transport": "telegram"})
    alert = saw.make_alert(saw.CRITICAL, "disk:/", "disk crit", "97.0% used",
                           source="disk", now=now)
    result = saw.run([alert], now=now, mode=saw.MODE_ACTIVE,
                     state=saw.CooldownState(), transport=transport)

    delivery = result.deliveries["disk:/"]
    assert delivery["sent"] is True
    assert delivery["simulated"] is False
    assert delivery["transport"] == "telegram"
    # Exactly one real send, and the message it received is the rendered alert.
    assert len(transport.calls) == 1
    assert "CRITICAL" in transport.calls[0]
    # Evidence reflects a real (non-simulated) send.
    [rec] = result.evidence
    assert rec["sent"] is True
    assert rec["simulated"] is False


def test_active_enabled_transport_receives_redacted_message(saw, monkeypatch):
    now = _in_hours(saw)
    monkeypatch.setenv(saw.ACTIVE_ENABLED_ENV, "1")
    transport = FakeTransport()
    alert = saw.make_alert(saw.CRITICAL, "probe:secret", "captured",
                           "token=supersecretvalue123", source="probe", now=now)
    saw.run([alert], now=now, mode=saw.MODE_ACTIVE,
            state=saw.CooldownState(), transport=transport)
    # The transport never sees a raw secret — redaction happens before send.
    assert len(transport.calls) == 1
    assert "supersecretvalue123" not in transport.calls[0]
    assert "[REDACTED]" in transport.calls[0]


def test_active_enabled_warning_never_sends(saw, monkeypatch):
    # Even active + enabled + in working hours, a warning is local-only: the
    # transport is never invoked and there is no delivery / telegram_message.
    now = _in_hours(saw)
    monkeypatch.setenv(saw.ACTIVE_ENABLED_ENV, "1")
    transport = FakeTransport()
    alert = saw.make_alert(saw.WARNING, "loadavg:1m", "load high", "5.00",
                           source="load", now=now)
    result = saw.run([alert], now=now, mode=saw.MODE_ACTIVE,
                     state=saw.CooldownState(), transport=transport)
    assert transport.calls == []
    assert result.delivered == []
    assert result.deliveries == {}
    assert result.telegram_message == ""
    [rec] = result.evidence
    assert rec["action"] == saw.LOCAL_ONLY


def test_active_enabled_info_never_sends(saw, monkeypatch):
    now = _in_hours(saw)
    monkeypatch.setenv(saw.ACTIVE_ENABLED_ENV, "1")
    transport = FakeTransport()
    alert = saw.make_alert(saw.INFO, "heartbeat:hb", "heartbeat ok", "120s old", now=now)
    result = saw.run([alert], now=now, mode=saw.MODE_ACTIVE,
                     state=saw.CooldownState(), transport=transport)
    assert transport.calls == []
    assert result.deliveries == {}


def test_dry_run_never_invokes_transport(saw):
    # Dry-run is the default: a critical alert is simulated, transport untouched.
    now = _in_hours(saw)
    transport = FakeTransport()
    alert = saw.make_alert(saw.CRITICAL, "disk:/", "disk crit", "97% used", now=now)
    result = saw.run([alert], now=now, mode=saw.MODE_DRY_RUN,
                     state=saw.CooldownState(), transport=transport)
    assert transport.calls == []
    delivery = result.deliveries["disk:/"]
    assert delivery["sent"] is False
    assert delivery["simulated"] is True


def test_active_but_disabled_never_invokes_transport(saw, monkeypatch):
    # mode=active but the enable gate is unset → no real send.
    now = _in_hours(saw)
    monkeypatch.delenv(saw.ACTIVE_ENABLED_ENV, raising=False)
    transport = FakeTransport()
    alert = saw.make_alert(saw.CRITICAL, "disk:/", "disk crit", "97% used", now=now)
    result = saw.run([alert], now=now, mode=saw.MODE_ACTIVE,
                     state=saw.CooldownState(), transport=transport)
    assert transport.calls == []
    assert result.deliveries["disk:/"]["simulated"] is True


# ---------------------------------------------------------------------------
# Default (real) transport wiring — every test above injects a fake, so nothing
# here covers the imports _default_telegram_transport itself resolves.
# ---------------------------------------------------------------------------


def test_default_transport_resolves_home_target_from_delivery_module(saw, monkeypatch):
    """The default transport resolves the home target from the module that defines it today
    and hands that target to the real send seam. No message leaves the process: the gateway
    config is faked and _send_to_platform is replaced with a recorder."""
    from cron import scheduler_delivery as delivery
    from gateway import config as gateway_config
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from tools import send_message_tool

    pconfig = PlatformConfig(enabled=True, token="fake-token")
    monkeypatch.setattr(
        gateway_config, "load_gateway_config",
        lambda: GatewayConfig(platforms={Platform.TELEGRAM: pconfig}),
    )
    monkeypatch.setattr(delivery, "_get_home_target_chat_id", lambda name: "home-chat-1")
    monkeypatch.setattr(delivery, "_get_home_target_thread_id", lambda name: "thread-7")

    sent: list[tuple] = []

    async def _fake_send(platform, pcfg, chat_id, message, thread_id=None, **kwargs):
        sent.append((platform, pcfg, chat_id, message, thread_id))
        return {}

    monkeypatch.setattr(send_message_tool, "_send_to_platform", _fake_send)

    result = saw._default_telegram_transport("watchdog probe")

    assert result == {"sent": True, "transport": "telegram"}
    assert sent == [(Platform.TELEGRAM, pconfig, "home-chat-1", "watchdog probe", "thread-7")]


# ---------------------------------------------------------------------------
# Transport failure behaviour (recorded, sanitized, retried)
# ---------------------------------------------------------------------------


def test_transport_returned_error_recorded_and_not_cooled_down(saw, monkeypatch):
    now = _in_hours(saw)
    monkeypatch.setenv(saw.ACTIVE_ENABLED_ENV, "1")
    transport = FakeTransport({"sent": False, "transport": "telegram",
                               "error": "telegram not configured/enabled"})
    state = saw.CooldownState()
    alert = saw.make_alert(saw.CRITICAL, "disk:/", "disk crit", "97% used", now=now)
    result = saw.run([alert], now=now, mode=saw.MODE_ACTIVE,
                     state=state, transport=transport)

    delivery = result.deliveries["disk:/"]
    assert delivery["sent"] is False
    assert delivery["simulated"] is False
    assert delivery["error"]
    # Evidence carries the redacted error; cooldown is NOT applied so a failed
    # critical retries next run rather than being silenced.
    [rec] = result.evidence
    assert rec["sent"] is False
    assert "error" in rec
    assert state.in_cooldown(alert, now + 60) is False


def test_transport_raised_exception_recorded_without_leak(saw, monkeypatch):
    now = _in_hours(saw)
    monkeypatch.setenv(saw.ACTIVE_ENABLED_ENV, "1")
    # The exception message embeds a secret-shaped token; it must not leak.
    boom = RuntimeError("boom token=supersecretvalue123")
    transport = FakeTransport(raises=boom)
    state = saw.CooldownState()
    alert = saw.make_alert(saw.CRITICAL, "disk:/", "disk crit", "97% used", now=now)
    result = saw.run([alert], now=now, mode=saw.MODE_ACTIVE,
                     state=state, transport=transport)

    delivery = result.deliveries["disk:/"]
    assert delivery["sent"] is False
    assert delivery["simulated"] is False
    rec = result.evidence[0]
    # Only the exception *type* is recorded — no message body / secret.
    assert "supersecretvalue123" not in json.dumps(rec)
    assert "supersecretvalue123" not in json.dumps(delivery)
    assert rec.get("error")
    # A raised transport is also a failure → not cooled down.
    assert state.in_cooldown(alert, now + 60) is False


# ---------------------------------------------------------------------------
# Routine warning is local-only (no Telegram in the critical-only scope)
# ---------------------------------------------------------------------------


def test_warning_is_local_only_in_hours(saw):
    # Even in working hours, a warning is NOT routed to Telegram in this scope:
    # not delivered, no deliveries entry, no telegram_message — just evidence.
    now = _in_hours(saw)
    alert = saw.make_alert(saw.WARNING, "loadavg:1m", "load average high",
                           "1m load 5.00", source="load", now=now)
    result = saw.run([alert], now=now, mode=saw.MODE_DRY_RUN, state=saw.CooldownState())
    assert result.delivered == []
    assert result.deliveries == {}
    assert result.telegram_message == ""
    [rec] = result.evidence
    assert rec["action"] == saw.LOCAL_ONLY
    assert rec["severity"] == saw.WARNING


# ---------------------------------------------------------------------------
# Info local-only
# ---------------------------------------------------------------------------


def test_info_is_local_only_never_messaged(saw):
    now = _in_hours(saw)
    alert = saw.make_alert(saw.INFO, "heartbeat:hb", "heartbeat ok", "120s old", now=now)
    result = saw.run([alert], now=now, mode=saw.MODE_DRY_RUN, state=saw.CooldownState())
    assert result.delivered == []
    assert result.deliveries == {}
    assert result.telegram_message == ""
    # Still recorded in evidence as local-only.
    [rec] = result.evidence
    assert rec["action"] == saw.LOCAL_ONLY
    assert rec["severity"] == saw.INFO


# ---------------------------------------------------------------------------
# Dedupe / cooldown
# ---------------------------------------------------------------------------


def test_dedupe_collapses_duplicate_keys_in_one_run(saw):
    now = _in_hours(saw)
    a1 = saw.make_alert(saw.WARNING, "disk:/", "disk warn", "86% used", now=now)
    a2 = saw.make_alert(saw.CRITICAL, "disk:/", "disk crit", "96% used", now=now)
    deduped = saw.dedupe([a1, a2])
    # One entry for the shared key, and the more-severe critical wins.
    assert len(deduped) == 1
    assert deduped[0].severity == saw.CRITICAL


def test_cooldown_suppresses_repeat_within_window(saw):
    now = _in_hours(saw)
    state = saw.CooldownState()
    alert = saw.make_alert(saw.CRITICAL, "disk:/", "disk crit", "96% used", now=now)

    first = saw.run([alert], now=now, mode=saw.MODE_DRY_RUN, state=state)
    assert [d.alert.key for d in first.delivered] == ["disk:/"]

    # Same key shortly after — inside the critical cooldown window — suppressed.
    soon = now + 60
    repeat = saw.make_alert(saw.CRITICAL, "disk:/", "disk crit", "96% used", now=soon)
    second = saw.run([repeat], now=soon, mode=saw.MODE_DRY_RUN, state=state)
    assert second.delivered == []
    [rec] = second.evidence
    assert rec["action"] == saw.SUPPRESSED

    # After the cooldown elapses it delivers again.
    later = now + saw.COOLDOWN_SECONDS[saw.CRITICAL] + 1
    again = saw.make_alert(saw.CRITICAL, "disk:/", "disk crit", "96% used", now=later)
    third = saw.run([again], now=later, mode=saw.MODE_DRY_RUN, state=state)
    assert [d.alert.key for d in third.delivered] == ["disk:/"]


# ---------------------------------------------------------------------------
# Off-hours critical delivery behaviour
# ---------------------------------------------------------------------------


def test_offhours_critical_still_delivers(saw):
    now = _off_hours(saw)
    alert = saw.make_alert(saw.CRITICAL, "disk:/", "disk crit", "96% used", now=now)
    result = saw.run([alert], now=now, mode=saw.MODE_DRY_RUN, state=saw.CooldownState())
    assert [d.alert.key for d in result.delivered] == ["disk:/"]
    [decision] = result.decisions
    assert decision.action == saw.DELIVER
    assert "off-hours" in decision.reason


def test_offhours_warning_is_local_only(saw):
    # Off-hours a warning is also local-only (no Telegram) — consistent with the
    # in-hours behaviour, since warnings are never part of this transport scope.
    now = _off_hours(saw)
    alert = saw.make_alert(saw.WARNING, "loadavg:1m", "load warn", "5.00", now=now)
    result = saw.run([alert], now=now, mode=saw.MODE_DRY_RUN, state=saw.CooldownState())
    assert result.delivered == []
    assert result.deliveries == {}
    assert result.telegram_message == ""
    [decision] = result.decisions
    assert decision.action == saw.LOCAL_ONLY


# ---------------------------------------------------------------------------
# No secret leakage
# ---------------------------------------------------------------------------


SECRET_DETAILS = [
    "openai key sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345 leaked",
    "password=hunter2 in config",
    "api_key: deadbeefcafef00dba5e",
    "github ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    "aws AKIAIOSFODNN7EXAMPLE here",
]


@pytest.mark.parametrize("detail", SECRET_DETAILS)
def test_no_secret_leakage_in_message_or_evidence(saw, detail):
    now = _in_hours(saw)
    alert = saw.make_alert(saw.CRITICAL, "probe:secret", "probe captured payload",
                           detail, source="probe", now=now)
    result = saw.run([alert], now=now, mode=saw.MODE_DRY_RUN, state=saw.CooldownState())

    msg = result.telegram_message
    rec = result.evidence[0]
    secret_fragments = ["sk-ABCDEFGH", "hunter2", "deadbeefcafe", "ghp_ABCDEFGH", "AKIAIOSFODNN7EXAMPLE"]
    for frag in secret_fragments:
        assert frag not in msg, (frag, msg)
        assert frag not in json.dumps(rec), (frag, rec)
    assert "[REDACTED]" in msg


def test_redact_collapses_newlines_and_bounds_length(saw):
    out = saw.redact("line one\nline two", max_len=200)
    assert "\n" not in out
    long = saw.redact("x" * 500, max_len=50)
    assert len(long) <= 50


# ---------------------------------------------------------------------------
# Evidence log + cooldown persistence (sanitized, local-only)
# ---------------------------------------------------------------------------


def test_evidence_log_round_trip_is_sanitized(saw, tmp_path):
    now = _in_hours(saw)
    log = tmp_path / "evidence.log"
    alert = saw.make_alert(saw.CRITICAL, "probe:secret", "captured",
                           "token=supersecretvalue123", now=now)
    result = saw.run([alert], now=now, mode=saw.MODE_DRY_RUN, state=saw.CooldownState())
    saw.append_evidence(result.evidence, log)

    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["severity"] == saw.CRITICAL
    assert rec["action"] == saw.DELIVER
    assert "supersecretvalue123" not in lines[0]


def test_cooldown_state_persists_and_reloads(saw, tmp_path):
    now = _in_hours(saw)
    state_path = tmp_path / "cooldown.json"
    state = saw.CooldownState()
    alert = saw.make_alert(saw.CRITICAL, "disk:/", "crit", "96%", now=now)
    saw.run([alert], now=now, mode=saw.MODE_DRY_RUN, state=state)
    state.save(state_path)

    reloaded = saw.CooldownState.load(state_path)
    assert reloaded.in_cooldown(alert, now + 60) is True
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["last_delivered"]["disk:/"] == now


# ---------------------------------------------------------------------------
# Active cron wrapper generation (portable mktemp + no-alert silence)
# ---------------------------------------------------------------------------


def _last_path_component(template: str) -> str:
    """The final ``/``-separated component of a mktemp template string."""
    return template.rsplit("/", 1)[-1]


def test_wrapper_uses_portable_mktemp_dir_template(saw):
    """The generated wrapper must use a temp *directory* whose mktemp template
    ends in a trailing run of X's — the only shape macOS/BSD mktemp accepts.

    This is the regression guard for the original bug: a ``.XXXXXX.out`` /
    ``.XXXXXX.err`` file template (X's NOT trailing) is rejected by BSD mktemp.
    """
    wrapper = saw.render_active_wrapper("/some/scripts/system_alert_watchdog.py")
    # Must create a temp directory, not a bare temp file.
    assert "mktemp -d " in wrapper
    # The OLD broken templates must be gone entirely.
    assert ".XXXXXX.out" not in wrapper
    assert ".XXXXXX.err" not in wrapper
    # Locate the mktemp template literal and assert its last component is a bare
    # run of X's (>= 3), i.e. the X's are trailing — valid on GNU and BSD.
    import re as _re
    m = _re.search(r'mktemp -d "([^"]+)"', wrapper)
    assert m, wrapper
    template = m.group(1)
    last = _last_path_component(template)
    assert last.endswith("XXXXXX"), template
    # After stripping the literal shell-expansion prefix, the trailing token is
    # nothing but X's (no ".out"/".err"/other suffix after the X run).
    trailing_xs = last[len(last.rstrip("X")):]
    assert len(trailing_xs) >= 3
    assert last.rstrip("X").endswith("system-alert-active.")


def test_wrapper_failure_line_is_not_alert_worded(saw):
    """A wrapper-level (process) failure must not borrow real-alert wording."""
    wrapper = saw.render_active_wrapper("/some/scripts/system_alert_watchdog.py")
    assert saw.WRAPPER_ERROR_MARKER in wrapper
    # The terse failure line must not look like a "System Alert [CRITICAL]" page.
    assert "System Alert [" not in wrapper
    assert "CRITICAL" not in wrapper
    # set -e would abort before we can inspect rc; it must NOT be used, and the
    # temp dir must be cleaned up on exit.
    assert "set -e" not in wrapper
    assert "trap 'rm -rf" in wrapper


def test_wrapper_quotes_paths_safely(saw):
    """Odd characters in the script path are shell-quoted, not interpolated raw."""
    wrapper = saw.render_active_wrapper("/path with space/sys watchdog.py")
    assert "'/path with space/sys watchdog.py'" in wrapper


def test_wrapper_rejects_unknown_mode(saw):
    with pytest.raises(ValueError):
        saw.render_active_wrapper("/x.py", mode="bogus")


def test_active_wrapper_opens_delivery_gate_for_watchdog(saw):
    """The generated active wrapper must open the critical-only delivery gate.

    Without ``HERMES_SYSTEM_ALERT_ACTIVE_ENABLED=1`` set for the watchdog
    process, ``deliver()`` only ever simulates even under ``--mode active`` —
    so the gate assignment must be present and scoped to the watchdog command.
    """
    wrapper = saw.render_active_wrapper("/some/scripts/system_alert_watchdog.py")
    gate = f"{saw.ACTIVE_ENABLED_ENV}=1"
    assert gate in wrapper
    # The assignment must prefix the actual watchdog invocation (same line), so
    # it is scoped to that single process rather than exported wrapper-wide.
    invocation_lines = [
        ln for ln in wrapper.splitlines()
        if '"$PYTHON" "$SCRIPT"' in ln and not ln.lstrip().startswith("#")
    ]
    assert invocation_lines, wrapper
    assert all(ln.startswith(gate + " ") for ln in invocation_lines), invocation_lines
    # Active mode is still what the watchdog is told to run in (double-gate).
    assert "--mode active" in wrapper


def test_dry_run_wrapper_does_not_open_delivery_gate(saw):
    """A dry-run wrapper must NOT open the gate, so it only ever simulates."""
    wrapper = saw.render_active_wrapper(
        "/some/scripts/system_alert_watchdog.py", mode=saw.MODE_DRY_RUN
    )
    assert f"{saw.ACTIVE_ENABLED_ENV}=1" not in wrapper
    assert "--mode dry-run" in wrapper


# --- Functional execution of the generated wrapper (requires bash) ---------

_BASH = shutil.which("bash") or ("/bin/bash" if os.path.isfile("/bin/bash") else None)
_needs_bash = pytest.mark.skipif(_BASH is None, reason="bash not available")


def _run_wrapper(saw, tmp_path, stub_body: str) -> subprocess.CompletedProcess:
    """Render the wrapper pointed at a stub 'python' and run it under bash.

    ``stub_body`` is the body of a fake interpreter (it receives the watchdog's
    argv but ignores it); we point ``python`` at it so the wrapper exercises its
    real temp-file + redirection + exit-handling logic without running probes.
    """
    stub = tmp_path / "stub_python.sh"
    stub.write_text("#!/usr/bin/env bash\n" + stub_body, encoding="utf-8")
    stub.chmod(0o755)
    wrapper_path = tmp_path / "wrapper.sh"
    wrapper_path.write_text(
        saw.render_active_wrapper(tmp_path / "unused_script.py", python=str(stub)),
        encoding="utf-8",
    )
    return subprocess.run(
        [_BASH, str(wrapper_path)],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(tmp_path),
    )


@_needs_bash
def test_wrapper_runs_and_is_silent_on_healthy_run(saw, tmp_path):
    """A healthy (rc=0) watchdog run produces NO wrapper output.

    This run also proves the mktemp template is portable on THIS platform: the
    old ``.XXXXXX.out`` template would have made ``mktemp`` fail here.
    """
    proc = _run_wrapper(saw, tmp_path, "exit 0\n")
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert proc.stderr == ""


@_needs_bash
def test_wrapper_emits_terse_nonalert_line_on_process_failure(saw, tmp_path):
    """A nonzero watchdog exit yields one terse, non-alert health line — and the
    captured stderr body (which embeds a secret-shaped token here) never leaks.
    """
    proc = _run_wrapper(
        saw, tmp_path,
        'echo "traceback token=supersecretvalue123" >&2\nexit 1\n',
    )
    assert proc.returncode == 0  # wrapper swallows rc so it is not double-flagged
    out = proc.stdout
    assert saw.WRAPPER_ERROR_MARKER in out
    assert "rc=1" in out
    # Not dressed as a real System Alert, and no captured stderr/secret echoed.
    assert "System Alert [" not in out
    assert "CRITICAL" not in out
    assert "supersecretvalue123" not in out


@_needs_bash
def test_wrapper_passes_active_gate_to_watchdog_process(saw, tmp_path):
    """The active gate env var is actually visible to the watchdog process.

    The stub 'python' records the value of HERMES_SYSTEM_ALERT_ACTIVE_ENABLED it
    was invoked with to a sentinel file (cwd is tmp_path). No real send happens —
    the stub never runs the watchdog or contacts Telegram. This proves the env
    gate the Python deliver() enforces is open under the active wrapper.
    """
    proc = _run_wrapper(
        saw, tmp_path,
        'printf "%s" "${HERMES_SYSTEM_ALERT_ACTIVE_ENABLED:-unset}" > gate_seen.txt\n'
        "exit 0\n",
    )
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert proc.stderr == ""
    assert (tmp_path / "gate_seen.txt").read_text(encoding="utf-8") == "1"


@_needs_bash
def test_wrapper_cleans_up_its_temp_dir(saw, tmp_path, monkeypatch):
    """The wrapper removes its private temp dir on exit (no leak in TMPDIR)."""
    tmpdir = tmp_path / "wtmp"
    tmpdir.mkdir()
    monkeypatch.setenv("TMPDIR", str(tmpdir))
    stub = tmp_path / "stub_python.sh"
    stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)
    wrapper_path = tmp_path / "wrapper.sh"
    wrapper_path.write_text(
        saw.render_active_wrapper(tmp_path / "unused_script.py", python=str(stub)),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [_BASH, str(wrapper_path)],
        capture_output=True, text=True, timeout=30, cwd=str(tmp_path),
        env={**os.environ, "TMPDIR": str(tmpdir)},
    )
    assert proc.returncode == 0
    # No leftover system-alert-active.* temp dirs.
    assert list(tmpdir.glob("system-alert-active.*")) == []


@_needs_bash
def test_old_xxxxxx_dot_out_template_would_fail_mktemp(tmp_path):
    """Document the original bug: a ``.XXXXXX.out`` template (X's not trailing)
    is rejected by macOS/BSD mktemp. This anchors WHY the wrapper uses a temp
    directory with trailing X's instead. (GNU mktemp also rejects this shape.)
    """
    bad = subprocess.run(
        [_BASH, "-c", 'mktemp "${TMPDIR:-/tmp}/system-alert-active.XXXXXX.out"'],
        capture_output=True, text=True, timeout=30,
    )
    # Clean up if some lenient mktemp actually created a file.
    created = (bad.stdout or "").strip()
    if bad.returncode == 0 and created and os.path.exists(created):
        os.unlink(created)
        pytest.skip("local mktemp accepted the non-trailing template; not BSD-strict")
    assert bad.returncode != 0


# --- emit-active-wrapper CLI ----------------------------------------------


def test_emit_active_wrapper_cli_prints_runnable_wrapper(saw, capsys):
    rc = saw.main(["--emit-active-wrapper"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("#!/usr/bin/env bash")
    assert "mktemp -d " in out
    assert ".XXXXXX.out" not in out
    # References the watchdog script itself in active mode, with the delivery
    # gate opened for the watchdog process (critical-only send is still enforced
    # by the Python deliver()).
    assert "--mode active" in out
    assert f"{saw.ACTIVE_ENABLED_ENV}=1" in out
