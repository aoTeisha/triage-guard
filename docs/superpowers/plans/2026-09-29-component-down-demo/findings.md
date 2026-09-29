# Findings — component-down demo

## What each outage did before this change

- **OPA down.** `opa.evaluate` returns `allow=False` with an `engine_unavailable:opa` reason.
  `verify_no_identifiers` counted that reason as a leak, so the payload check was
  structural, the case wrote `V_HALT_PII` and halted in `awaiting_recovery`. Recovery
  re-ran redaction and halted again; release is OPA-authorized too, so it was refused.
  The case had no way out until OPA came back.
- **Prolog or Datalog down during safety.** `safety.validate` turned the engine error
  into a `fail` verdict. The gate then treated it as an ordinary safety failure, which
  demands a correction that changes a field of the case, since resubmitting the same
  input would fail the same way. The nurse had nothing to correct.
  `ValidatorUnavailable` existed but was never raised.
- **LLM down.** Already degraded correctly: `classifier_fallback` uses the nurse's
  acuity, turns the gap gate off and flags the case.
- **Monitor (sweeper) down.** The graph never calls the sweeper; it only writes timer
  rows to Postgres, so cases keep flowing. The board computed `monitor_degraded` from
  the heartbeat but never displayed it.

## Checked and ruled out

- **Does Prolog/Datalog down stop the LLM?** No. They run in safety validation, after
  classification. Only OPA sits before the model.
- **Would `ValidatorUnavailable` be retried by LangGraph?** No. It subclasses
  `RuntimeError`, which the default retry policy does not retry, so it reaches
  `_on_safety_error` on the first raise.
- **Can the OPA-down payload be kept in state?** Yes. It is built by the schema-drop
  normalizer and passes the regex identifier scan; only OPA's structural proof is
  missing. It is kept because the Datalog provenance check and the board card read the
  chief complaint from it. It is never handed to the model: routing skips `classifying`.

## Stuck paths left as they are (outside the intake path)

- OPA down refuses every release and every move to treatment; the case waits.
- Prolog down makes `may_resolve_gate` refuse every resolver, so nobody can answer a
  gate until it is back.
- OPA down when a CRM writeback timer fires sets the timer to `CANCELLED`, not
  `FAILED`, so the visit is never written back.
- An error inside `find_duplicate_active_case` is caught by the CRM error handler: the
  duplicate check is skipped and the audit row blames the CRM.
- The sweeper's Datalog invariant pass logs a warning and skips on failure; no alert.
- `prolog._term` renders enum values as `'AcuitySource.HUMAN_CONFIRMED'`, so the
  safety.pl rules `human_confirmed_without_a_decision` and `auto_resolved_on_major_gap`
  never fire in graph runs.

## Demo simulation

Each outage is simulated for one submission only, by patching the engine call while
`runner.start_case` runs, the same way the safety-fail demo patches the validator. The
patch is process-wide while active, so the demo assumes one person submits at a time.
The monitor is a separate process and is demoed by stopping it.
