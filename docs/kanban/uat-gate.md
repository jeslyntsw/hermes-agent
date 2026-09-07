# Fail-closed Kanban UAT gate

The UAT gate stops a protected **merge / deploy / LIVE** card from progressing
until the User-Acceptance-Testing cards it depends on have *demonstrably*
passed. It is **fail-closed**: anything short of an exact, machine-readable
`PASS` with `complete` coverage keeps the card blocked, with a precise reason.

## What makes a card "protected"

Only **explicit structured metadata** — never a title or body heuristic. A card
carries a JSON `gate` object (column `tasks.gate`; `Task.gate`):

- **Protected card**
  ```json
  {"kind": "merge",              // or "deploy" | "live"
   "uat": ["t_aaaa", "t_bbbb"],  // declared UAT card ids it gates on
   "repair_lane": ["t_cccc"],    // sanctioned repair/re-UAT lane (audit only)
   "exceptions": [ ... ]}        // explicit, scoped bypasses (see below)
  ```
- **UAT card**
  ```json
  {"kind": "uat",
   "verdict": {"result": "PASS", "coverage": "complete", "by": "qa", "at": 1690000000}}
  ```

A card with `gate IS NULL` (or an unrecognised `kind`) is **untyped** and is
never gated — existing boards behave exactly as before.

## The rule

Before a protected card can be **promoted, unblocked, claimed, or dispatched**,
every id in `gate.uat` must be:

1. **terminal** (`done`/`archived`), and
2. carry `verdict.result == "PASS"` **and** `verdict.coverage == "complete"`.

A missing verdict, an unparseable verdict, `partial`/`incomplete` coverage,
`FAIL`, `AMEND`, or `FAIL_AMEND` all keep the gate **closed**. The verdict lives
in durable card metadata and is read directly, so no status-only path —
notify+wake, cross-session reconciliation, `unblock`, or `recompute_ready` — can
reinterpret a `done` + `FAIL`/`AMEND` card as passing. `complete_task` records
the verdict **atomically** with the terminal flip.

`promote --force` relaxes ordinary parent gating but **cannot** open the UAT
gate. Only an explicit exception can.

## Failed UAT: only the repair lane moves

A closed gate blocks the protected card itself; it does **not** block the
untyped cards that make up the declared `repair_lane`. Those proceed normally so
the team can fix the regression and re-run UAT. The protected merge/deploy/LIVE
card stays closed and its block reason names the repair lane.

## Exceptions

The single sanctioned bypass. An exception is **explicit, scoped to one named
UAT id, attributable, auditable, and optionally time-boxed** — there is no
wildcard, comment-based, or inferred bypass.

```
hermes kanban gate-except <protected_id> --uat <uat_id> \
    --actor release-manager --expires-at 1690099999 approved in incident #42
```

Each exception is recorded as a `gate_exception` event and echoed back by
`evaluate_uat_gate(...).exceptions_used`.

## Python API

```python
from hermes_cli import kanban_db as kb

prot = kb.create_task(conn, title="Merge to LIVE", parents=[uat],
                      gate={"kind": "merge", "uat": [uat], "repair_lane": [repair]})
kb.set_task_gate(conn, uat, kind="uat", actor="qa")
kb.record_uat_verdict(conn, uat, result="PASS", coverage="complete", actor="qa")
kb.add_gate_exception(conn, prot, uat=uat, actor="rm", reason="hotfix")
decision = kb.evaluate_uat_gate(conn, prot)   # .allowed / .reason / .blockers
report = kb.audit_gates(conn)
```

`complete_task(conn, uat, uat_verdict={"result": "PASS", "coverage": "complete"})`
records the verdict atomically with completion.

## CLI

```
hermes kanban create "Merge to LIVE" --gate-kind merge --uat t_uat --repair-lane t_fix
hermes kanban gate-set   <id> --kind uat
hermes kanban uat-verdict <id> --result PASS --coverage complete
hermes kanban gate-except <id> --uat <uat_id> reason...
hermes kanban gate-audit  [--json]
```

## Limitation & adoption/migration audit

The gate is **opt-in typing**. A merge/deploy/LIVE card that is *not* typed
(`gate IS NULL`) is **not protected** — it behaves like any legacy card. This is
deliberate: the system never guesses a card's intent from its wording, so
adopting the gate on an existing board requires typing the relevant cards.

`hermes kanban gate-audit` (or `kb.audit_gates(conn)`) is the adoption/migration
check. It reports every typed protected card with its live gate status, every
UAT card and whether its verdict passes, and the count of **untyped (ungated)**
cards. It is a purely *structural* audit — it intentionally does **not** flag
which untyped cards "look like" a deploy and should be typed; that judgement
stays with the operator. Migrating a board is therefore: run the audit, then
`gate-set` / re-create the cards that should be protected and their UAT cards.

Backward compatibility: the `gate` column is added additively
(`_migrate_add_optional_columns`); existing rows read as untyped, and every
non-gate code path is unchanged.
