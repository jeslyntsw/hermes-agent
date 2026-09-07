"""Fail-closed UAT gate for the Kanban board.

A card is *protected* only when it carries EXPLICIT structured gate metadata —
``gate.kind`` in :data:`PROTECTED_GATE_KINDS` (merge / deploy / live). There is
no title/body heuristic anywhere in this module; an untyped card (``gate IS
NULL`` or no recognised ``kind``) is never gated, preserving legacy behaviour.

A protected card declares the ids of the UAT cards it depends on
(``gate.uat``). Before the card may progress (promote / unblock / claim /
dispatch), EVERY declared UAT card must be:

  * terminal (``done``/``archived``), and
  * carry an exact machine-readable verdict of ``PASS`` with ``complete``
    coverage (``gate.verdict = {"result": "PASS", "coverage": "complete", ...}``).

Anything short of that — a missing or unparseable verdict, incomplete
coverage, ``FAIL``, ``AMEND``, or ``FAIL_AMEND`` — keeps the gate CLOSED with a
precise, machine-readable reason. Because the verdict is read from durable card
metadata (never inferred from the card merely being ``done``), no status-only
path — notify+wake, cross-session reconciliation, unblock, recompute — can
reinterpret a ``done`` + ``FAIL``/``AMEND`` card as passing.

Exceptions are the only bypass and they are explicit, scoped to a single named
UAT id, attributable to an actor, and recorded on the event log by the caller.
There is no inferred, comment-based, or wildcard bypass.

This module performs READS only (raw SQL over ``tasks``); all writes and audit
events live in :mod:`hermes_cli.kanban_db`, which keeps the gate decoupled from
the DB write path and free of import cycles.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Optional

# Explicitly-typed protected lanes. A card is gated ONLY when its structured
# ``gate.kind`` is one of these — never because of what its title says.
PROTECTED_GATE_KINDS = ("merge", "deploy", "live")
UAT_KIND = "uat"
VALID_GATE_KINDS = (*PROTECTED_GATE_KINDS, UAT_KIND)

# Machine-readable UAT verdicts. Only PASS is a passing verdict; AMEND and
# FAIL_AMEND are explicitly non-passing (a "done, but change it" outcome must
# never read as green).
PASS = "PASS"
FAIL = "FAIL"
AMEND = "AMEND"
FAIL_AMEND = "FAIL_AMEND"
VALID_VERDICTS = (PASS, FAIL, AMEND, FAIL_AMEND)
PASSING_VERDICTS = frozenset({PASS})

# Coverage grading. Only ``complete`` clears the gate; partial/incomplete are
# fail-closed just like a FAIL.
COVERAGE_COMPLETE = "complete"
VALID_COVERAGE = (COVERAGE_COMPLETE, "partial", "incomplete")

TERMINAL_STATUSES = ("done", "archived")


@dataclass
class GateDecision:
    """Outcome of evaluating one card's UAT gate.

    ``allowed`` is True for any card that is not a protected gate card, and for
    a protected card whose every declared UAT requirement is satisfied (by a
    PASS/complete verdict or an active explicit exception). When False,
    ``reason`` is a precise human/machine-readable string and ``blockers``
    lists the individual unmet UAT requirements.
    """

    allowed: bool
    reason: Optional[str] = None
    blockers: list[dict[str, Any]] = field(default_factory=list)
    exceptions_used: list[dict[str, Any]] = field(default_factory=list)
    protected: bool = False
    kind: Optional[str] = None


def parse_gate(raw: Any) -> Optional[dict]:
    """Return the gate dict for a stored ``tasks.gate`` value, or None.

    Tolerant of NULL, already-parsed dicts, and malformed JSON (treated as no
    gate — an untyped card — so a corrupt cell can never *open* a gate)."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return obj if isinstance(obj, dict) else None
    return None


def gate_kind(gate: Optional[dict]) -> Optional[str]:
    if not isinstance(gate, dict):
        return None
    kind = gate.get("kind")
    return kind if kind in VALID_GATE_KINDS else None


def is_protected(gate: Optional[dict]) -> bool:
    return gate_kind(gate) in PROTECTED_GATE_KINDS


def is_uat(gate: Optional[dict]) -> bool:
    return gate_kind(gate) == UAT_KIND


def required_uat_ids(gate: Optional[dict]) -> list[str]:
    if not isinstance(gate, dict):
        return []
    ids = gate.get("uat")
    if not isinstance(ids, list):
        return []
    # De-dupe, preserve order, drop blanks.
    seen: dict[str, None] = {}
    for i in ids:
        if isinstance(i, str) and i.strip():
            seen.setdefault(i.strip(), None)
    return list(seen)


def repair_lane_ids(gate: Optional[dict]) -> list[str]:
    if not isinstance(gate, dict):
        return []
    ids = gate.get("repair_lane")
    if not isinstance(ids, list):
        return []
    return [i.strip() for i in ids if isinstance(i, str) and i.strip()]


def verdict_of(gate: Optional[dict]) -> Any:
    if not isinstance(gate, dict):
        return None
    return gate.get("verdict")


def verdict_problem(verdict: Any) -> Optional[str]:
    """Return None if ``verdict`` is an exact PASS with complete coverage, else
    a precise reason it does NOT clear the gate. This is the single source of
    truth for "does this UAT verdict pass?" — fail-closed on anything unexpected.
    """
    if not isinstance(verdict, dict):
        return "no machine-readable UAT verdict recorded"
    result = verdict.get("result")
    if not isinstance(result, str) or result not in VALID_VERDICTS:
        return f"unparseable UAT verdict {result!r}"
    if result not in PASSING_VERDICTS:
        return f"UAT verdict is {result}"
    coverage = verdict.get("coverage")
    if coverage != COVERAGE_COMPLETE:
        return f"UAT coverage is {coverage or 'unspecified'!s} (need {COVERAGE_COMPLETE})"
    return None


def build_verdict(result: str, coverage: str, *, actor: Optional[str], at: int) -> dict:
    """Normalise a verdict for storage. Raises ValueError on an invalid enum so
    a bad value can never be persisted (and later read as a partial pass)."""
    result = (result or "").strip().upper()
    coverage = (coverage or "").strip().lower()
    if result not in VALID_VERDICTS:
        raise ValueError(f"result must be one of {VALID_VERDICTS}, got {result!r}")
    if coverage not in VALID_COVERAGE:
        raise ValueError(f"coverage must be one of {VALID_COVERAGE}, got {coverage!r}")
    verdict = {"result": result, "coverage": coverage, "at": int(at)}
    if actor:
        verdict["by"] = str(actor)
    return verdict


def _active_exception(exceptions: Any, uat_id: str, now: int) -> Optional[dict]:
    """Return the first active, unexpired exception scoped to ``uat_id``.

    Only an exact-id scope counts — a wildcard/broad bypass is intentionally
    unrepresentable here, so a stored ``"*"`` (should it ever appear) never
    matches a real UAT id."""
    if not isinstance(exceptions, list):
        return None
    for exc in exceptions:
        if not isinstance(exc, dict):
            continue
        if exc.get("uat") != uat_id:
            continue
        expires_at = exc.get("expires_at")
        if expires_at is not None:
            try:
                if int(expires_at) <= now:
                    continue
            except (TypeError, ValueError):
                continue
        return exc
    return None


def _fetch(conn: sqlite3.Connection, task_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT status, gate FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()


def evaluate(
    conn: sqlite3.Connection, task_id: str, *, now: Optional[int] = None
) -> GateDecision:
    """Evaluate the UAT gate for ``task_id``. See the module docstring.

    Fail-closed: any protected card with an unmet UAT requirement returns
    ``allowed=False`` with a precise reason; an untyped card returns
    ``allowed=True``."""
    if now is None:
        now = int(time.time())
    row = _fetch(conn, task_id)
    if row is None:
        # Unknown card: not this gate's job to reject (existence is checked by
        # the caller); treat as not-protected so behaviour is unchanged.
        return GateDecision(allowed=True)
    gate = parse_gate(row["gate"])
    if not is_protected(gate):
        return GateDecision(allowed=True, protected=False, kind=gate_kind(gate))

    kind = gate_kind(gate)
    exceptions = gate.get("exceptions") if isinstance(gate, dict) else None
    blockers: list[dict[str, Any]] = []
    used: list[dict[str, Any]] = []
    repair_lane = repair_lane_ids(gate)

    required = required_uat_ids(gate)
    if not required:
        # A protected card that declares no UAT is a configuration error; keep
        # it closed rather than silently open.
        return GateDecision(
            allowed=False,
            reason=f"protected {kind} card declares no UAT cards to gate on",
            protected=True,
            kind=kind,
        )

    for uat_id in required:
        exc = _active_exception(exceptions, uat_id, now)
        if exc is not None:
            used.append({"uat": uat_id, "actor": exc.get("actor"), "reason": exc.get("reason")})
            continue
        urow = _fetch(conn, uat_id)
        if urow is None:
            blockers.append({"uat": uat_id, "reason": "declared UAT card does not exist"})
            continue
        if urow["status"] not in TERMINAL_STATUSES:
            blockers.append(
                {"uat": uat_id, "reason": f"UAT card not terminal (status={urow['status']})"}
            )
            continue
        problem = verdict_problem(verdict_of(parse_gate(urow["gate"])))
        if problem:
            blockers.append({"uat": uat_id, "reason": problem})

    if blockers:
        detail = "; ".join(f"{b['uat']}: {b['reason']}" for b in blockers)
        reason = f"UAT gate closed on protected {kind} card {task_id}: {detail}"
        if repair_lane:
            reason += (
                f". Failed UAT may only release the declared repair lane "
                f"({', '.join(repair_lane)}), never this {kind} card."
            )
        return GateDecision(
            allowed=False, reason=reason, blockers=blockers,
            exceptions_used=used, protected=True, kind=kind,
        )
    return GateDecision(
        allowed=True, exceptions_used=used, protected=True, kind=kind,
    )
