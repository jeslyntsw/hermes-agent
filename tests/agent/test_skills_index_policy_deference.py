"""The generated ## Skills block must not contradict an explicit skill-loading policy.

A profile whose SOUL.md (or a project context file) states a selective skill-loading
policy was handed a generated block ordering the opposite in the same prompt
("you MUST load ... Err on the side of loading ... load them even for tasks you
already know how to do"). The generated wording is the *default*, so it defers to an
explicit policy when one exists and keeps its blanket behaviour when none does.

Three things survive either way and are asserted as such: mandatory safety/specialist
procedures stay loadable, content already in context is reused instead of re-fetched,
and genuinely `[SKILL_PRUNED]` content is reloaded.
"""

from __future__ import annotations

import pytest

from agent.prompt_builder import (
    SKILLS_GUIDANCE, build_skills_system_prompt, clear_skills_system_prompt_cache)


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_skills_system_prompt_cache(clear_snapshot=True)
    yield
    clear_skills_system_prompt_cache(clear_snapshot=True)


def _index(tmp_path, monkeypatch, **kwargs):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    skill = tmp_path / "skills" / "coding" / "demo-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Demo skill for the index.\n---\n\nBody.\n",
        encoding="utf-8")
    return build_skills_system_prompt(**kwargs)


def test_blanket_load_directive_is_scoped_by_the_deferral_clause(tmp_path, monkeypatch):
    """The relationship under test: the "load everything relevant" default must be
    *preceded* by the clause that hands precedence to an explicit policy, so a model
    reading top-down never meets the unconditional order first."""
    prompt = _index(tmp_path, monkeypatch)
    assert "explicit skill-loading policy" in prompt
    assert "MUST load" in prompt, "the default must survive for profiles without a policy"
    assert prompt.index("explicit skill-loading policy") < prompt.index("MUST load")


def test_default_behaviour_is_retained_when_no_policy_exists(tmp_path, monkeypatch):
    """Absent an explicit policy the original err-on-the-side default still applies."""
    prompt = _index(tmp_path, monkeypatch)
    assert "erring on the side of loading" in prompt or "Err on the side of loading" in prompt
    assert "even partially relevant" in prompt
    # The closing "proceed without loading" line is likewise conditioned, not absolute.
    closing = prompt[prompt.index("proceed without loading"):]
    qualifier = prompt[:prompt.index("proceed without loading")]
    assert "Absent such a policy" in qualifier[-80:], (
        f"the closing directive must be qualified by the same deferral; got {closing[:120]!r}")


def test_safety_and_specialist_procedures_survive_any_policy(tmp_path, monkeypatch):
    prompt = _index(tmp_path, monkeypatch)
    assert "mandatory safety or specialist procedure" in prompt
    # And the phrasing must make it unconditional rather than part of the default.
    assert "Under any policy" in prompt


def test_already_loaded_content_is_reused_not_refetched(tmp_path, monkeypatch):
    prompt = _index(tmp_path, monkeypatch)
    assert "already loaded in this conversation" in prompt
    assert "reuse" in prompt.lower()


def test_pruned_content_must_still_be_reloaded(tmp_path, monkeypatch):
    """Reuse must never be read as "ignore the Skill Safety Rule"."""
    prompt = _index(tmp_path, monkeypatch)
    assert "[SKILL_PRUNED]" in prompt
    assert "reload" in prompt.lower()
    # The authoritative rule itself is untouched.
    assert "## Skill Safety Rule" in SKILLS_GUIDANCE
    assert "reload it with skill_view(name='...')" in SKILLS_GUIDANCE


def test_index_boundaries_and_gates_are_unchanged(tmp_path, monkeypatch):
    """Everything the block already promised keeps working."""
    prompt = _index(tmp_path, monkeypatch)
    assert "<available_skills>" in prompt and "</available_skills>" in prompt
    assert "- demo-skill: Demo skill for the index." in prompt
    assert "skill_manage(action='patch')" in prompt
    # No skills, no block.
    clear_skills_system_prompt_cache(clear_snapshot=True)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "empty"))
    assert build_skills_system_prompt() == ""


def test_dangling_tool_reference_rule_is_unchanged(tmp_path, monkeypatch):
    """The basic-tools example still drops web_search when the session has no web tools."""
    prompt = _index(tmp_path, monkeypatch, available_tools={"terminal"})
    assert "basic tools like terminal" in prompt
    assert "web_search" not in prompt
