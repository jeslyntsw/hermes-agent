"""Behavioral regression tests: ``complete_task`` is fail-closed on the UAT gate.

The sibling suite (``test_kanban_uat_gate.py``) proves the gate is enforced at
every path that *promotes* a protected card into a claimable/running lane
(promote / unblock / claim / claim-review / recompute / dispatch). This suite
closes the remaining hole: a direct ``complete_task`` call on a protected
merge/deploy card must NOT be able to flip it to ``done`` (and thereby release
its children) while its declared UAT is missing, incomplete, unparseable,
``FAIL``, ``AMEND``, or ``FAIL_AMEND``.

A protected card can legitimately sit in any completable source state
(``ready``/``blocked``/``review``, and — defensively — ``running``) because a
racing writer, an operator, or a stale lane can park it there; ``complete_task``
is the last write boundary before ``done`` and must re-check the gate INSIDE its
write transaction, before the terminal status flip or any child release.

All typing is EXPLICIT structured ``tasks.gate`` metadata — nothing here parses
titles or bodies. Only temp DBs are touched (``Path.home`` is redirected).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban as kb_cli  # noqa: F401  (parity with sibling suite)
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # HERMES_HOME alone does not isolate the board; Path.home() must move too.
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
    return kb.create_task(
        conn, title=title, assignee="dev", parents=list(uat_ids),
        gate={"kind": kind, "uat": list(uat_ids), "repair_lane": list(repair_lane)},
    )


def _finish_uat(conn, uat_id, *, result, coverage, actor="qa"):
    ok, reason = kb.record_uat_verdict(
        conn, uat_id, result=result, coverage=coverage, actor=actor
    )
    assert ok, reason
    assert kb.complete_task(conn, uat_id, result=f"verdict={result}")


def _park(conn, task_id, status):
    """Simulate any writer/operator that parked the card in a completable lane."""
    conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))
    conn.commit()


def _completed_events(conn, task_id):
    return conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='completed'",
        (task_id,),
    ).fetchone()[0]


# --- 1. Direct-completion bypass is closed for every adverse verdict --------

# ``AMEND``/``FAIL_AMEND`` are "done, but change it" — never green. Plus a
# genuine ``FAIL``. Every completable source state is exercised.
@pytest.mark.parametrize("verdict", ["FAIL", "AMEND", "FAIL_AMEND"])
@pytest.mark.parametrize("source_status", ["ready", "blocked", "review", "running"])
def test_complete_rejected_on_adverse_verdict(conn, verdict, source_status):
    uat = _uat_card(conn)
    prot = _protected(conn, [uat])
    _finish_uat(conn, uat, result=verdict, coverage="complete")  # parents now terminal
    _park(conn, prot, source_status)

    # The direct completion bypass must be refused, leaving the card non-done.
    assert kb.complete_task(conn, prot, result="shipping it") is False
    assert kb.get_task(conn, prot).status == source_status
    assert kb.get_task(conn, prot).status != "done"
    assert _completed_events(conn, prot) == 0


# --- 2. Missing / unparseable / incomplete UAT is fail-closed --------------

def test_complete_rejected_on_missing_verdict(conn):
    uat = _uat_card(conn)
    prot = _protected(conn, [uat])
    # UAT completes with NO machine-readable verdict recorded.
    assert kb.complete_task(conn, uat, result="lgtm")
    _park(conn, prot, "review")
    assert kb.complete_task(conn, prot, result="merge") is False
    assert kb.get_task(conn, prot).status == "review"


def test_complete_rejected_on_unparseable_verdict(conn):
    uat = _uat_card(conn)
    prot = _protected(conn, [uat])
    conn.execute(
        "UPDATE tasks SET status='done', gate=? WHERE id=?",
        ('{"kind":"uat","verdict":{"result":"MAYBE","coverage":"complete"}}', uat),
    )
    conn.commit()
    _park(conn, prot, "ready")
    assert kb.complete_task(conn, prot, result="merge") is False
    assert kb.get_task(conn, prot).status == "ready"


@pytest.mark.parametrize("coverage", ["partial", "incomplete"])
def test_complete_rejected_on_incomplete_coverage(conn, coverage):
    uat = _uat_card(conn)
    prot = _protected(conn, [uat])
    _finish_uat(conn, uat, result="PASS", coverage=coverage)
    _park(conn, prot, "blocked")
    assert kb.complete_task(conn, prot, result="merge") is False
    assert kb.get_task(conn, prot).status == "blocked"


# --- 3. Multiple UAT parents: one adverse verdict blocks completion --------

def test_complete_rejected_when_one_of_many_uats_fails(conn):
    uat_a = _uat_card(conn, title="UAT A")
    uat_b = _uat_card(conn, title="UAT B")
    prot = _protected(conn, [uat_a, uat_b])
    _finish_uat(conn, uat_a, result="PASS", coverage="complete")
    _finish_uat(conn, uat_b, result="FAIL", coverage="complete")
    _park(conn, prot, "review")

    assert kb.complete_task(conn, prot, result="merge") is False
    assert kb.get_task(conn, prot).status == "review"

    # Fixing the failing lane opens the gate; completion then succeeds.
    ok, _ = kb.record_uat_verdict(conn, uat_b, result="PASS", coverage="complete", actor="qa")
    assert ok
    assert kb.complete_task(conn, prot, result="merge") is True
    assert kb.get_task(conn, prot).status == "done"


# --- 4. Child release is prevented on rejection ---------------------------

def test_rejected_completion_does_not_release_children(conn):
    uat = _uat_card(conn)
    prot = _protected(conn, [uat])
    child = kb.create_task(conn, title="post-merge deploy step", assignee="dev", parents=[prot])
    assert kb.get_task(conn, child).status == "todo"  # parked behind the protected parent
    _finish_uat(conn, uat, result="FAIL", coverage="complete")
    _park(conn, prot, "ready")

    assert kb.complete_task(conn, prot, result="merge") is False
    # The protected parent is not done, so the child must remain unreleased.
    assert kb.get_task(conn, prot).status != "done"
    kb.recompute_ready(conn)
    assert kb.get_task(conn, child).status == "todo"
    assert kb.claim_task(conn, child) is None


# --- 5. PASS still completes (and releases children) ----------------------

def test_all_pass_completes_and_releases_children(conn):
    uat = _uat_card(conn)
    prot = _protected(conn, [uat])
    child = kb.create_task(conn, title="post-merge deploy step", assignee="dev", parents=[prot])
    _finish_uat(conn, uat, result="PASS", coverage="complete")

    # recompute promotes the (now gate-open) protected card; claim it and complete.
    kb.recompute_ready(conn)
    assert kb.claim_task(conn, prot) is not None
    assert kb.complete_task(conn, prot, result="merged") is True
    assert kb.get_task(conn, prot).status == "done"

    # Child is released now that its protected parent is legitimately done.
    kb.recompute_ready(conn)
    assert kb.get_task(conn, child).status == "ready"
    assert kb.claim_task(conn, child) is not None


# --- 6. Explicit scoped exception still permits completion (audited) -------

def test_explicit_exception_permits_completion(conn):
    uat = _uat_card(conn)
    prot = _protected(conn, [uat])
    _finish_uat(conn, uat, result="FAIL", coverage="complete")
    _park(conn, prot, "review")

    # Without the exception, completion is refused.
    assert kb.complete_task(conn, prot, result="merge") is False
    assert kb.get_task(conn, prot).status == "review"

    ok, reason = kb.add_gate_exception(
        conn, prot, uat=uat, actor="release-manager", reason="hotfix incident #42",
    )
    assert ok, reason

    # The audited, scoped exception opens the gate; completion now succeeds.
    assert kb.complete_task(conn, prot, result="merge") is True
    assert kb.get_task(conn, prot).status == "done"
    # The exception remains auditable on the event log.
    assert conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='gate_exception'",
        (prot,),
    ).fetchone()[0] == 1


# --- 7. Untyped / UAT cards are unaffected (backward compatibility) --------

def test_untyped_card_completes_normally(conn):
    parent = kb.create_task(conn, title="ordinary parent", assignee="dev")
    child = kb.create_task(conn, title="ordinary child", parents=[parent], assignee="dev")
    _park(conn, parent, "running")
    assert kb.complete_task(conn, parent, result="done") is True
    assert kb.get_task(conn, parent).status == "done"
    kb.recompute_ready(conn)
    assert kb.get_task(conn, child).status == "ready"


def test_uat_card_completes_with_and_without_verdict(conn):
    # Completing a UAT card (kind='uat', not protected) is never gated by this
    # predicate — including the atomic verdict-recording path.
    uat_plain = _uat_card(conn, title="UAT plain")
    assert kb.complete_task(conn, uat_plain, result="observed") is True

    uat_verdict = _uat_card(conn, title="UAT with verdict")
    prot = _protected(conn, [uat_verdict])
    assert kb.complete_task(
        conn, uat_verdict, result="passed",
        uat_verdict={"result": "PASS", "coverage": "complete", "by": "qa"},
    ) is True
    assert kb.get_task(conn, uat_verdict).gate["verdict"]["result"] == "PASS"
    # And the protected dependent may now complete once claimed.
    kb.recompute_ready(conn)
    assert kb.claim_task(conn, prot) is not None
    assert kb.complete_task(conn, prot, result="merged") is True


# --- 8. Role-confusion (protected + verdict) still raises, unchanged -------

def test_protected_with_verdict_still_raises_valueerror(conn):
    """Preserve existing API behaviour: recording a UAT verdict on a protected
    card is role confusion and raises ``ValueError`` — the new fail-closed gate
    must not silently downgrade that to a quiet ``False``."""
    uat = _uat_card(conn)
    prot = kb.create_task(conn, title="Merge", assignee="dev", gate={"kind": "merge", "uat": [uat]})
    _park(conn, prot, "ready")
    with pytest.raises(ValueError):
        kb.complete_task(
            conn, prot, result="x",
            uat_verdict={"result": "PASS", "coverage": "complete"},
        )
    g = kb.get_task(conn, prot).gate
    assert g["kind"] == "merge" and "verdict" not in g
    assert kb.get_task(conn, prot).status == "ready"


# --- 9. Rollback / concurrency: rejection is atomic and leaves no run ------

def test_rejected_completion_is_atomic(conn):
    uat = _uat_card(conn)
    prot = _protected(conn, [uat])
    _finish_uat(conn, uat, result="FAIL", coverage="complete")
    _park(conn, prot, "running")

    assert kb.complete_task(conn, prot, result="merge") is False

    row = kb.get_task(conn, prot)
    assert row.status == "running"          # not flipped
    assert row.completed_at is None         # no terminal timestamp
    assert row.result is None               # result not recorded
    assert _completed_events(conn, prot) == 0
    # No run row was closed as 'completed' by the refused completion.
    assert conn.execute(
        "SELECT COUNT(*) FROM task_runs WHERE task_id=? AND status='completed'",
        (prot,),
    ).fetchone()[0] == 0
    # An auditable rejection reason is recorded on the event log.
    reject_payloads = [
        r[0] for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='completion_rejected'",
            (prot,),
        ).fetchall()
    ]
    assert reject_payloads and any("uat" in (p or "").lower() for p in reject_payloads)
