"""Behavioral tests for the fail-closed Kanban UAT gate.

The gate protects explicitly-typed merge/deploy/LIVE cards: before such a card
can be promoted, unblocked, claimed, or dispatched, every UAT card it declares
must be terminal AND carry an exact machine-readable ``PASS`` verdict with
``complete`` coverage. Anything short of that — missing/unparseable verdict,
incomplete coverage, ``FAIL``, or ``FAIL_AMEND`` — keeps the gate closed with a
precise reason. Untyped cards are never affected (backward compatibility).

All card/gate typing is EXPLICIT structured metadata. Nothing here parses
titles or bodies; a card is only protected because ``gate.kind`` says so.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban as kb_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_uat_gate as gate


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c


# --- helpers ---------------------------------------------------------------

def _uat_card(conn, title="UAT: acceptance"):
    tid = kb.create_task(conn, title=title, assignee="qa")
    kb.set_task_gate(conn, tid, kind="uat", actor="qa")
    return tid


def _protected(conn, uat_ids, *, kind="merge", repair_lane=(), title="Merge to LIVE"):
    prot = kb.create_task(
        conn, title=title, assignee="dev", parents=list(uat_ids),
        gate={"kind": kind, "uat": list(uat_ids), "repair_lane": list(repair_lane)},
    )
    return prot


def _finish_uat(conn, uat_id, *, result, coverage, actor="qa"):
    ok, reason = kb.record_uat_verdict(
        conn, uat_id, result=result, coverage=coverage, actor=actor
    )
    assert ok, reason
    assert kb.complete_task(conn, uat_id, result=f"verdict={result}")


# --- 1. PASS opens the gate -----------------------------------------------

def test_pass_complete_coverage_opens_gate(conn):
    uat = _uat_card(conn)
    prot = _protected(conn, [uat])
    # Parent (uat) not terminal yet -> protected child parked in todo.
    assert kb.get_task(conn, prot).status == "todo"

    _finish_uat(conn, uat, result="PASS", coverage="complete")

    decision = kb.evaluate_uat_gate(conn, prot)
    assert decision.allowed, decision.reason

    # recompute promotes it now that UAT passed and parent is terminal.
    kb.recompute_ready(conn)
    assert kb.get_task(conn, prot).status == "ready"
    # And it can actually be claimed (dispatch path).
    assert kb.claim_task(conn, prot) is not None


# --- 2. FAIL / FAIL_AMEND keep the gate closed ----------------------------

@pytest.mark.parametrize("verdict", ["FAIL", "AMEND", "FAIL_AMEND"])
def test_failing_verdict_blocks_protected_card(conn, verdict):
    uat = _uat_card(conn)
    prot = _protected(conn, [uat])
    _finish_uat(conn, uat, result=verdict, coverage="complete")

    decision = kb.evaluate_uat_gate(conn, prot)
    assert not decision.allowed
    assert verdict in decision.reason

    # Not auto-promoted, not manually promotable (even with --force), not claimable.
    kb.recompute_ready(conn)
    assert kb.get_task(conn, prot).status == "todo"
    ok, reason = kb.promote_task(conn, prot, actor="ops", force=True)
    assert not ok
    assert "UAT" in reason
    # Force may clear ordinary parent gating but never the UAT gate.
    assert kb.get_task(conn, prot).status == "todo"
    # A stray 'ready' still cannot be claimed into 'running'.
    conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (prot,))
    conn.commit()
    assert kb.claim_task(conn, prot) is None
    assert kb.get_task(conn, prot).status == "todo"


# --- 3. Missing verdict is fail-closed ------------------------------------

def test_missing_verdict_blocks(conn):
    uat = _uat_card(conn)
    prot = _protected(conn, [uat])
    # UAT completes with NO recorded machine-readable verdict.
    assert kb.complete_task(conn, uat, result="looks fine to me")

    decision = kb.evaluate_uat_gate(conn, prot)
    assert not decision.allowed
    assert "verdict" in decision.reason.lower()
    kb.recompute_ready(conn)
    assert kb.get_task(conn, prot).status == "todo"


def test_unparseable_verdict_blocks(conn):
    uat = _uat_card(conn)
    prot = _protected(conn, [uat])
    # Corrupt/garbage verdict payload written directly.
    conn.execute(
        "UPDATE tasks SET status='done', gate=? WHERE id=?",
        ('{"kind":"uat","verdict":{"result":"MAYBE","coverage":"complete"}}', uat),
    )
    conn.commit()
    decision = kb.evaluate_uat_gate(conn, prot)
    assert not decision.allowed
    assert "verdict" in decision.reason.lower()


# --- 4. Incomplete coverage is fail-closed --------------------------------

@pytest.mark.parametrize("coverage", ["partial", "incomplete"])
def test_incomplete_coverage_blocks(conn, coverage):
    uat = _uat_card(conn)
    prot = _protected(conn, [uat])
    _finish_uat(conn, uat, result="PASS", coverage=coverage)
    decision = kb.evaluate_uat_gate(conn, prot)
    assert not decision.allowed
    assert "coverage" in decision.reason.lower()


# --- 5. Multiple UAT parents: ALL must pass -------------------------------

def test_multiple_uat_parents_all_required(conn):
    uat_a = _uat_card(conn, title="UAT A")
    uat_b = _uat_card(conn, title="UAT B")
    prot = _protected(conn, [uat_a, uat_b])

    _finish_uat(conn, uat_a, result="PASS", coverage="complete")
    _finish_uat(conn, uat_b, result="FAIL", coverage="complete")
    d1 = kb.evaluate_uat_gate(conn, prot)
    assert not d1.allowed
    assert uat_b in d1.reason
    assert uat_a not in d1.reason  # the passing one is not a blocker

    # Fix the failing lane: re-run UAT B to PASS.
    ok, _ = kb.record_uat_verdict(conn, uat_b, result="PASS", coverage="complete", actor="qa")
    assert ok
    d2 = kb.evaluate_uat_gate(conn, prot)
    assert d2.allowed, d2.reason


# --- 6. Failed UAT releases ONLY the declared repair lane -----------------

def test_failed_uat_releases_only_repair_lane(conn):
    uat = _uat_card(conn)
    repair = kb.create_task(conn, title="Fix the regression", assignee="dev")
    prot = _protected(conn, [uat], repair_lane=[repair])
    _finish_uat(conn, uat, result="FAIL", coverage="complete")

    # The declared repair-lane card is ordinary/untyped and proceeds freely.
    assert kb.evaluate_uat_gate(conn, repair).allowed
    assert kb.get_task(conn, repair).status in ("ready", "todo")
    kb.recompute_ready(conn)
    assert kb.get_task(conn, repair).status == "ready"
    assert kb.claim_task(conn, repair) is not None

    # The protected merge/deploy card stays closed and names the repair lane.
    d = kb.evaluate_uat_gate(conn, prot)
    assert not d.allowed
    assert repair in d.reason


# --- 7. Explicit, scoped, attributable exception --------------------------

def test_explicit_exception_unblocks_with_audit(conn):
    uat = _uat_card(conn)
    prot = _protected(conn, [uat])
    _finish_uat(conn, uat, result="FAIL", coverage="complete")

    assert not kb.evaluate_uat_gate(conn, prot).allowed

    ok, reason = kb.add_gate_exception(
        conn, prot, uat=uat, actor="release-manager",
        reason="hotfix approved in incident #42",
    )
    assert ok, reason

    d = kb.evaluate_uat_gate(conn, prot)
    assert d.allowed, d.reason
    assert any(e["uat"] == uat and e["actor"] == "release-manager" for e in d.exceptions_used)

    # The exception is auditable on the event log.
    events = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='gate_exception'", (prot,)
    ).fetchall()
    assert events

    # No broad bypass: an exception must name a real declared UAT id.
    bad, _ = kb.add_gate_exception(conn, prot, uat="*", actor="x", reason="nope")
    assert not bad
    bad2, _ = kb.add_gate_exception(conn, prot, uat="t_deadbeef", actor="x", reason="nope")
    assert not bad2


def test_exception_scoped_to_one_uat_of_many(conn):
    uat_a = _uat_card(conn, title="UAT A")
    uat_b = _uat_card(conn, title="UAT B")
    prot = _protected(conn, [uat_a, uat_b])
    _finish_uat(conn, uat_a, result="FAIL", coverage="complete")
    _finish_uat(conn, uat_b, result="FAIL", coverage="complete")

    kb.add_gate_exception(conn, prot, uat=uat_a, actor="rm", reason="approved A")
    d = kb.evaluate_uat_gate(conn, prot)
    # B is still failing and still blocks — the exception did not widen.
    assert not d.allowed
    assert uat_b in d.reason
    assert uat_a not in d.reason


# --- 6b. notify+wake / cross-session cannot reinterpret done+FAIL ----------

def test_done_fail_not_reinterpreted_as_pass(conn):
    uat = _uat_card(conn)
    prot = _protected(conn, [uat])
    _finish_uat(conn, uat, result="FAIL", coverage="complete")

    # Simulate every status-only path a notify/wake or cross-session handler
    # might take: unblock, recompute, re-block/unblock. None may open the gate.
    kb.recompute_ready(conn)
    conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (prot,))
    conn.commit()
    kb.unblock_task(conn, prot)
    assert kb.get_task(conn, prot).status != "ready"
    assert not kb.evaluate_uat_gate(conn, prot).allowed

    # Even the UAT card being 'done' (terminal) does not imply pass.
    assert kb.get_task(conn, uat).status == "done"
    assert not kb.evaluate_uat_gate(conn, prot).allowed


# --- unchanged ordinary cards ---------------------------------------------

def test_untyped_cards_unaffected(conn):
    parent = kb.create_task(conn, title="ordinary parent", assignee="dev")
    child = kb.create_task(conn, title="ordinary child", parents=[parent], assignee="dev")
    assert kb.evaluate_uat_gate(conn, child).allowed
    conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
    conn.commit()
    kb.recompute_ready(conn)
    assert kb.get_task(conn, child).status == "ready"
    assert kb.claim_task(conn, child) is not None


# --- unblock of a protected card never lands in ready while UAT fails ------

def test_unblock_protected_lands_in_todo_when_gate_closed(conn):
    uat = _uat_card(conn)
    prot = _protected(conn, [uat])
    _finish_uat(conn, uat, result="FAIL", coverage="complete")
    conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (prot,))
    conn.commit()
    assert kb.unblock_task(conn, prot)
    assert kb.get_task(conn, prot).status == "todo"


# --- audit / adoption check -----------------------------------------------

def test_gate_audit_reports_typed_and_untyped(conn):
    uat = _uat_card(conn)
    _protected(conn, [uat])
    kb.create_task(conn, title="ordinary", assignee="dev")
    report = kb.audit_gates(conn)
    assert report["protected"]
    assert report["untyped_count"] >= 1
    assert any(p["uat_ready"] is False for p in report["protected"])


# --- pure-module evaluation contract --------------------------------------

def test_module_verdict_problem_contract():
    assert gate.verdict_problem({"result": "PASS", "coverage": "complete"}) is None
    assert gate.verdict_problem({"result": "PASS", "coverage": "partial"})
    assert gate.verdict_problem({"result": "FAIL", "coverage": "complete"})
    assert gate.verdict_problem({"result": "BOGUS", "coverage": "complete"})
    assert gate.verdict_problem(None)
    assert gate.verdict_problem({})


# --- fail-open: inline exceptions/verdicts must NOT be smuggled in ---------

def test_inline_exception_at_creation_is_rejected(conn):
    """FAIL-OPEN REGRESSION: an inline ``exceptions`` list on the create gate
    would open a protected card without going through add_gate_exception (no
    declared-UAT/actor/reason/scope validation, no audit event). It must be
    rejected outright."""
    uat = _uat_card(conn)
    with pytest.raises(ValueError):
        kb.create_task(
            conn, title="Sneaky merge", assignee="dev",
            gate={"kind": "merge", "uat": [uat],
                  "exceptions": [{"uat": uat, "actor": "self", "reason": "pls"}]},
        )


def test_inline_exception_on_set_gate_is_rejected(conn):
    uat = _uat_card(conn)
    with pytest.raises(ValueError):
        kb._normalize_gate_metadata(
            {"kind": "deploy", "uat": [uat],
             "exceptions": [{"uat": uat, "actor": "x", "reason": "y"}]}
        )


def test_only_add_gate_exception_grants_bypass(conn):
    """The bypass is unreachable except through the audited API."""
    uat = _uat_card(conn)
    prot = _protected(conn, [uat])
    _finish_uat(conn, uat, result="FAIL", coverage="complete")
    assert not kb.evaluate_uat_gate(conn, prot).allowed
    # The audited path works and leaves an event; that remains the ONLY way.
    ok, _ = kb.add_gate_exception(conn, prot, uat=uat, actor="rm", reason="incident")
    assert ok
    assert kb.evaluate_uat_gate(conn, prot).allowed
    assert conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='gate_exception'", (prot,)
    ).fetchone()[0] == 1


def test_inline_uat_verdict_at_creation_is_rejected(conn):
    """FAIL-OPEN REGRESSION: an inline ``verdict`` on a UAT card's create gate
    would record an unaudited PASS that opens dependents' gates without going
    through record_uat_verdict/completion. It must be rejected."""
    with pytest.raises(ValueError):
        kb.create_task(
            conn, title="Sneaky UAT", assignee="qa",
            gate={"kind": "uat",
                  "verdict": {"result": "PASS", "coverage": "complete"}},
        )


def test_inline_verdict_on_normalize_is_rejected(conn):
    with pytest.raises(ValueError):
        kb._normalize_gate_metadata(
            {"kind": "uat", "verdict": {"result": "PASS", "coverage": "complete"}}
        )


def test_persisted_verdict_preserved_across_retype(conn):
    """Compatibility: a verdict legitimately recorded via the audited API
    survives a re-type of the same UAT card (it is read back from the DB, not
    from caller-supplied inline metadata)."""
    uat = _uat_card(conn)
    prot = _protected(conn, [uat])
    ok, _ = kb.record_uat_verdict(conn, uat, result="PASS", coverage="complete", actor="qa")
    assert ok
    # Re-typing the card as uat must not erase the recorded verdict.
    assert kb.set_task_gate(conn, uat, kind="uat", actor="qa")
    g = kb.get_task(conn, uat).gate
    assert g["verdict"]["result"] == "PASS"
    kb.complete_task(conn, uat, result="done")
    assert kb.evaluate_uat_gate(conn, prot).allowed


# --- role guards: verdicts never blur/clobber a protected card ------------

def test_record_verdict_refuses_protected_card(conn):
    uat = _uat_card(conn)
    prot = _protected(conn, [uat], repair_lane=["t_fix"])
    ok, reason = kb.record_uat_verdict(conn, prot, result="PASS", coverage="complete", actor="x")
    assert not ok
    assert "protected" in reason
    # Its config is intact (uat + repair_lane preserved, no verdict injected).
    g = kb.get_task(conn, prot).gate
    assert g["kind"] == "merge" and g["uat"] == [uat] and "verdict" not in g


def test_complete_with_verdict_refuses_protected_card(conn):
    uat = _uat_card(conn)
    # Parent-free protected card so completion reaches the verdict-merge guard
    # (rather than being stopped earlier by the parent-satisfaction check).
    prot = kb.create_task(conn, title="Merge", assignee="dev",
                          gate={"kind": "merge", "uat": [uat]})
    with pytest.raises(ValueError):
        kb.complete_task(conn, prot, result="x",
                         uat_verdict={"result": "PASS", "coverage": "complete"})
    # The protected card was NOT re-typed and its config is intact.
    g = kb.get_task(conn, prot).gate
    assert g["kind"] == "merge" and "verdict" not in g


def test_complete_task_records_verdict_atomically(conn):
    # A UAT card completed with a verdict lands both in one step; the gate the
    # verdict controls opens immediately.
    uat = _uat_card(conn)
    prot = _protected(conn, [uat])
    assert kb.complete_task(conn, uat, result="uat passed",
                            uat_verdict={"result": "PASS", "coverage": "complete", "by": "qa"})
    assert kb.get_task(conn, uat).status == "done"
    assert kb.get_task(conn, uat).gate["verdict"]["result"] == "PASS"
    assert kb.evaluate_uat_gate(conn, prot).allowed


# --- dispatcher path: a gate-closed protected card is never spawned --------

def _fake_spawn_factory(spawns):
    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 4242
    return fake_spawn


def test_dispatcher_never_spawns_gate_closed_protected(conn, all_assignees_spawnable):
    uat = _uat_card(conn)
    prot = _protected(conn, [uat])
    _finish_uat(conn, uat, result="FAIL", coverage="complete")

    # A full dispatcher tick must NOT spawn the protected card (recompute won't
    # promote it, and even a forced 'ready' is rejected at claim).
    spawns: list = []
    res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns))
    assert prot not in spawns
    assert prot not in [t[0] for t in res.spawned]

    # Defense in depth: force it 'ready' (simulating any racing writer) and tick
    # again — the claim inside the dispatcher rejects it back to 'todo'.
    conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (prot,))
    conn.commit()
    spawns = []
    kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns))
    assert prot not in spawns
    assert kb.get_task(conn, prot).status == "todo"

    # Now the UAT passes: the SAME dispatcher tick promotes AND spawns it.
    ok, _ = kb.record_uat_verdict(conn, uat, result="PASS", coverage="complete", actor="qa")
    assert ok
    spawns = []
    kbd.dispatch_once(conn, spawn_fn=_fake_spawn_factory(spawns))
    assert prot in spawns
    assert kb.get_task(conn, prot).status == "running"


# --- review-claim path is gated too ---------------------------------------

def test_review_claim_path_rejects_gate_closed(conn):
    uat = _uat_card(conn)
    prot = _protected(conn, [uat])
    _finish_uat(conn, uat, result="FAIL", coverage="complete")
    # A protected card sitting in review with a closed gate cannot be claimed
    # into a review run; it demotes to todo.
    conn.execute("UPDATE tasks SET status='review' WHERE id=?", (prot,))
    conn.commit()
    assert kb.claim_review_task(conn, prot) is None
    assert kb.get_task(conn, prot).status == "todo"

    # Once UAT passes, the review claim succeeds.
    ok, _ = kb.record_uat_verdict(conn, uat, result="PASS", coverage="complete", actor="qa")
    assert ok
    conn.execute("UPDATE tasks SET status='review' WHERE id=?", (prot,))
    conn.commit()
    assert kb.claim_review_task(conn, prot) is not None


# --- CLI exit status ------------------------------------------------------

def _ns(**kw):
    kw.setdefault("board", None)
    return argparse.Namespace(**kw)


def _create_ns(**kw):
    base = dict(
        board=None, title="x", body=None, assignee="dev", parent=[], workspace="scratch",
        branch=None, project=None, tenant=None, priority=0, triage=False,
        idempotency_key=None, max_runtime=None, created_by="user", skills=[],
        max_retries=None, model_override=None, provider_override=None,
        completion_contract=None, gate_kind=None, uat=[], repair_lane=[],
        goal_mode=False, goal_max_turns=None, initial_status="running", json=False,
    )
    base.update(kw)
    return argparse.Namespace(kanban_action="create", **base)


def test_cli_create_protected_without_uat_is_usage_error(kanban_home):
    # --gate-kind merge with no --uat is a usage error (exit 2), consistently.
    rc = kb_cli.kanban_command(_create_ns(title="Merge", gate_kind="merge", uat=[]))
    assert rc == 2


def test_cli_create_uat_flags_without_kind_is_usage_error(kanban_home):
    rc = kb_cli.kanban_command(_create_ns(title="x", uat=["t_abcd"]))
    assert rc == 2


def test_cli_uat_verdict_and_gate_audit_exit_status(kanban_home, capsys):
    with kbc.connect() as conn:
        uat = _uat_card(conn)
        prot = _protected(conn, [uat])

    # Record a FAIL verdict via the CLI, then complete the UAT card.
    rc = kb_cli.kanban_command(_ns(
        kanban_action="uat-verdict", task_id=uat, result="FAIL",
        coverage="complete", author="qa",
    ))
    assert rc == 0
    with kbc.connect() as conn:
        assert kb.complete_task(conn, uat, result="done")

    # Promoting the protected card must fail (non-zero exit) with a precise reason.
    rc = kb_cli.kanban_command(_ns(
        kanban_action="promote", task_id=prot, ids=None, reason=[],
        force=True, dry_run=False, json=False,
    ))
    assert rc == 1
    err = capsys.readouterr().err
    assert "UAT" in err

    # gate-audit succeeds and reports the closed gate.
    rc = kb_cli.kanban_command(_ns(kanban_action="gate-audit", json=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert prot in out
