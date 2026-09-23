"""The generated ## Skills block must not contradict an explicit skill-loading policy.

A profile whose SOUL.md (or a project context file) states a selective skill-loading
policy was handed a generated block ordering the opposite in the same prompt
("you MUST load ... Err on the side of loading ... load them even for tasks you
already know how to do"). The generated wording no longer carries a blanket default
at all: the block states one policy sentence — the soul's policy decides, and pruned
content is reloaded — and otherwise carries only the index itself.

The authoritative Skill Safety Rule lives in SKILLS_GUIDANCE and is untouched.
"""

from __future__ import annotations

import pytest

from agent.prompt_builder import (
    SKILLS_GUIDANCE, build_skills_system_prompt, clear_skills_system_prompt_cache)

POLICY_SENTENCE = "Soul policy wins; reload only if pruned."

# Every scrap of the old instructional essay, which must not come back in any form.
BANNED_FRAGMENTS = (
    "MUST load",
    "even partially relevant",
    "erring on the side of loading",
    "Err on the side of loading",
    "Under any policy",
    "mandatory safety or specialist procedure",
    "already loaded in this conversation",
    "basic tools like",
    "Absent such a policy",
    "proceed without loading",
    "skill_manage(action='patch')",
    "offer to save as a skill",
)


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


def test_block_states_exactly_the_one_policy_sentence(tmp_path, monkeypatch):
    prompt = _index(tmp_path, monkeypatch)
    assert "## Skills" in prompt
    assert POLICY_SENTENCE in prompt


def test_instructional_essay_is_gone(tmp_path, monkeypatch):
    """No fragment of the old default-loading essay survives anywhere in the block."""
    prompt = _index(tmp_path, monkeypatch)
    for fragment in BANNED_FRAGMENTS:
        assert fragment not in prompt, f"removed guidance resurfaced: {fragment!r}"


def test_index_boundaries_and_gates_are_unchanged(tmp_path, monkeypatch):
    """Everything the block still promises keeps working."""
    prompt = _index(tmp_path, monkeypatch)
    assert "<available_skills>" in prompt and "</available_skills>" in prompt
    assert "- demo-skill: Demo skill for the index." in prompt
    # The tags wrap the index rather than trailing it.
    assert prompt.index("<available_skills>") < prompt.index("- demo-skill:")
    assert prompt.index("- demo-skill:") < prompt.index("</available_skills>")


def test_no_skills_means_no_block(tmp_path, monkeypatch):
    """An empty skills dir still yields no block at all."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "empty"))
    assert build_skills_system_prompt() == ""


def test_skill_safety_rule_still_lives_in_the_guidance():
    """Reload-when-pruned stays authoritative in SKILLS_GUIDANCE, not in the index block."""
    assert "## Skill Safety Rule" in SKILLS_GUIDANCE
    assert "reload it with skill_view(name='...')" in SKILLS_GUIDANCE


def test_tool_availability_does_not_alter_the_policy_sentence(tmp_path, monkeypatch):
    """With no web tools the block neither names web_search nor revives the basic-tools example."""
    prompt = _index(tmp_path, monkeypatch, available_tools={"terminal"})
    assert POLICY_SENTENCE in prompt
    assert "web_search" not in prompt
    assert "basic tools like" not in prompt
