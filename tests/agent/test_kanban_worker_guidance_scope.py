"""KANBAN_GUIDANCE is the dispatcher's WORKER protocol, not a kanban-tools blurb.

``kanban_show`` is visible to any session whose profile enables the ``kanban``
toolset — an ordinary chat/orchestrator session with no assigned card. Gating the
worker protocol on tool visibility alone told those sessions they "have been
assigned ONE task" and handed them ``$HERMES_KANBAN_TASK`` orientation they cannot
satisfy. Ownership is the existing contract: an assigned task id AND
``agent.delegation_context.is_dispatcher_owned_worker_context()`` (cron ticks and
delegate children inherit the env but are not the worker).

These tests drive the real init path (``agent.agent_init._load_tools``) and the
real prompt path (``agent.system_prompt.build_system_prompt``); the tools
themselves must be untouched either way.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from agent.agent_init import _load_tools
from agent.delegation_context import (
    DELEGATED_CHILD_ENV_MARKER, delegated_child_context, non_dispatcher_owned_context)
from agent.prompt_builder import KANBAN_GUIDANCE
from agent.system_prompt import build_system_prompt

WORKER_PROTOCOL_HEADING = "# Kanban task execution protocol"

# One phrase per lifecycle stage the worker protocol must still deliver in full.
WORKER_PROTOCOL_MARKERS = (
    "Call `kanban_show()` first",             # orientation
    "cd $HERMES_KANBAN_WORKSPACE",            # workspace
    "kanban_heartbeat",                       # heartbeat
    "The reviewer approves with",             # approval
    "kanban_complete",                        # completion
    "lists child IDs",                        # child graph
    "parents=[your-task-id]",                 # child graph
)


@pytest.fixture(autouse=True)
def _clean_kanban_env(monkeypatch):
    for key in ("HERMES_KANBAN_TASK", DELEGATED_CHILD_ENV_MARKER):
        monkeypatch.delenv(key, raising=False)
    yield


def _prompt_agent(tool_names, **overrides):
    """Minimal agent shaped like the one ``build_system_prompt`` consumes."""
    base = dict(
        load_soul_identity=False, skip_context_files=True, valid_tool_names=set(tool_names),
        _task_completion_guidance=False, _tool_use_enforcement=False, _environment_probe=False,
        _memory_store=None, _memory_manager=None, _memory_enabled=False, _user_profile_enabled=False,
        model="", provider="", platform="cli", pass_session_id=False, session_id="",
        _emit_status=lambda *_a, **_kw: None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _init_guidance(toolsets=("kanban",)):
    """Run the real ``_load_tools`` and return (guidance, resolved tool names)."""
    agent = SimpleNamespace(quiet_mode=True)
    _load_tools(agent, list(toolsets), None)
    return agent._kanban_worker_guidance, agent.valid_tool_names


# --- Unassigned, kanban-capable sessions -----------------------------------


def test_unassigned_kanban_session_has_no_worker_protocol_but_keeps_tools(tmp_path, monkeypatch):
    """A Main-like session with the kanban toolset and no assigned card."""
    monkeypatch.chdir(tmp_path)
    guidance, tool_names = _init_guidance()
    assert "kanban_show" in tool_names, "kanban tools must stay in the schema"
    assert guidance == ""

    prompt = build_system_prompt(_prompt_agent(tool_names, _kanban_worker_guidance=guidance))
    assert WORKER_PROTOCOL_HEADING not in prompt
    assert "You have been assigned ONE task" not in prompt


@pytest.mark.parametrize("raw", ["", "   "])
def test_blank_task_env_fails_safe(raw, tmp_path, monkeypatch):
    """A present-but-empty assignment is not an assignment."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_TASK", raw)
    guidance, tool_names = _init_guidance()
    assert "kanban_show" in tool_names
    assert guidance == ""


# --- The genuine dispatcher-owned worker ------------------------------------


def test_genuine_dispatcher_worker_gets_the_full_protocol(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_real_card")
    guidance, tool_names = _init_guidance()
    assert guidance == KANBAN_GUIDANCE

    prompt = build_system_prompt(_prompt_agent(tool_names, _kanban_worker_guidance=guidance))
    assert WORKER_PROTOCOL_HEADING in prompt
    for marker in WORKER_PROTOCOL_MARKERS:
        assert marker in prompt, f"worker protocol lost its {marker!r} instruction"


# --- Inherited env that is NOT ownership ------------------------------------


def test_cron_tick_inside_a_worker_is_not_a_worker(tmp_path, monkeypatch):
    """``cron/scheduler.py`` enters the non-dispatcher context around each tick."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_real_card")
    with non_dispatcher_owned_context():
        guidance, tool_names = _init_guidance()
        prompt = build_system_prompt(_prompt_agent(tool_names))
    assert guidance == ""
    assert WORKER_PROTOCOL_HEADING not in prompt


def test_in_process_delegate_child_is_not_a_worker(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_real_card")
    with delegated_child_context("child-1"):
        guidance, _tool_names = _init_guidance()
        prompt = build_system_prompt(_prompt_agent({"kanban_show"}))
    assert guidance == ""
    assert WORKER_PROTOCOL_HEADING not in prompt


def test_spawned_descendant_of_a_child_is_not_a_worker(tmp_path, monkeypatch):
    """The env marker survives a real process spawn; TASK alone must not promote it."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_real_card")
    monkeypatch.setenv(DELEGATED_CHILD_ENV_MARKER, str(tmp_path))
    guidance, _tool_names = _init_guidance()
    assert guidance == ""
    assert WORKER_PROTOCOL_HEADING not in build_system_prompt(_prompt_agent({"kanban_show"}))


# --- init and the system_prompt fallback must agree -------------------------


@pytest.mark.parametrize("scenario", ["unassigned", "worker", "cron", "child"])
def test_init_and_prompt_fallback_agree(scenario, tmp_path, monkeypatch):
    """``system_prompt`` re-resolves for agents that bypass ``agent_init``; the two
    entry points must never disagree about who is a worker."""
    monkeypatch.chdir(tmp_path)
    if scenario != "unassigned":
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_real_card")

    def _both():
        init_value, tool_names = _init_guidance()
        # No _kanban_worker_guidance attribute at all -> the fallback path.
        fallback_prompt = build_system_prompt(_prompt_agent(tool_names))
        return init_value, fallback_prompt

    if scenario == "cron":
        with non_dispatcher_owned_context():
            init_value, fallback_prompt = _both()
    elif scenario == "child":
        with delegated_child_context("child-1"):
            init_value, fallback_prompt = _both()
    else:
        init_value, fallback_prompt = _both()

    assert (WORKER_PROTOCOL_HEADING in fallback_prompt) == bool(init_value)


# --- Session-static resolution ----------------------------------------------


def test_initialized_empty_guidance_stays_empty(tmp_path, monkeypatch):
    """An agent that resolved "" at init keeps it even under a worker env."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_real_card")
    prompt = build_system_prompt(_prompt_agent({"kanban_show"}, _kanban_worker_guidance=""))
    assert WORKER_PROTOCOL_HEADING not in prompt


@pytest.mark.parametrize("start_as_worker", [True, False])
def test_repeat_builds_are_stable_when_ambient_env_changes(start_as_worker, tmp_path, monkeypatch):
    """The system prompt is byte-stable for the life of a conversation; a mid-session
    env flip (a worker var exported by a tool call, a cleared one) must not move it."""
    monkeypatch.chdir(tmp_path)
    if start_as_worker:
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_real_card")
    agent = _prompt_agent({"kanban_show"})
    first = build_system_prompt(agent)

    if start_as_worker:
        monkeypatch.delenv("HERMES_KANBAN_TASK")
    else:
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_appeared_later")
    assert build_system_prompt(agent) == first
    assert (WORKER_PROTOCOL_HEADING in first) is start_as_worker


def test_interleaved_sessions_do_not_leak(tmp_path, monkeypatch):
    """A multiplexed host builds prompts for a worker and a plain session in turn."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_real_card")
    worker = _prompt_agent({"kanban_show"})
    worker_prompt = build_system_prompt(worker)

    with non_dispatcher_owned_context():
        bystander = _prompt_agent({"kanban_show"})
        bystander_prompt = build_system_prompt(bystander)

    assert WORKER_PROTOCOL_HEADING in worker_prompt
    assert WORKER_PROTOCOL_HEADING not in bystander_prompt
    # And re-entering either session later still renders its own verdict.
    assert build_system_prompt(worker) == worker_prompt
    assert build_system_prompt(bystander) == bystander_prompt


def test_predicate_failure_fails_safe(monkeypatch, tmp_path):
    """An unresolvable ownership contract means "not a worker", never a worker."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_real_card")
    import agent.delegation_context as dc

    def _boom():
        raise RuntimeError("contextvar unavailable")

    monkeypatch.setattr(dc, "is_dispatcher_owned_worker_context", _boom)
    guidance, _tool_names = _init_guidance()
    assert guidance == ""


def test_worker_env_without_kanban_tools_adds_nothing(tmp_path, monkeypatch):
    """No kanban tools in the schema -> the protocol would name unreachable tools."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_real_card")
    assert WORKER_PROTOCOL_HEADING not in build_system_prompt(_prompt_agent({"terminal"}))


def test_kanban_tool_visibility_boundary_is_unchanged(tmp_path, monkeypatch):
    """The guidance fix must not move the check_fn gate: no toolset, no tools."""
    monkeypatch.chdir(tmp_path)
    _guidance, tool_names = _init_guidance(toolsets=("file",))
    assert "kanban_show" not in tool_names
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_real_card")
    _guidance, worker_tools = _init_guidance(toolsets=("file",))
    assert "kanban_show" in worker_tools, "dispatcher workers still get lifecycle tools"


def test_os_environ_is_not_mutated(tmp_path, monkeypatch):
    """Ownership is read, never written — the worker's heartbeat shares this env."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_real_card")
    before = dict(os.environ)
    _init_guidance()
    build_system_prompt(_prompt_agent({"kanban_show"}))
    assert dict(os.environ) == before
