# Findings — outage switch

- The per-case demo's findings (what each outage did, what was ruled out) are in
  `../2026-09-29-component-down-demo/findings.md`; the stuck paths listed there as out
  of scope are the ones this change fixes: OPA refusing every move and release, Prolog
  refusing every gate resolver, and the writeback timer being cancelled.
- `prolog._engine` is `lru_cache`d, so a hook inside it would run once per process.
  The cached engine moves to `_loaded_engine`, and `_engine` checks the outage flag
  before returning it.
- The authorization functions keep returning `(ok, why)`: about 25 test call sites
  unpack that shape. A shift-lead stand-in is marked by `why` starting with
  `SHIFT_LEAD_STANDS_IN`, which the nodes check to mark the case degraded.
- The monitor has no in-process call to hook: its outage is the sweeper skipping its
  own tick and heartbeat, which the board already reads as "Monitor down".
- Technician alerting for outages was discussed and left out by the user's choice.

## Final review

Fixed (each with a test that failed first):
- A payload left by a previous triage let a re-file with a detected leak skip the halt. A failed payload check now clears the payload, and a re-file clears the payload, the OPA-down mark, the disabled confidence gate and the validator-down list.
- A shift-lead sign-off recorded the same `degraded` key as an intake outage, so the panel listed things the case never went without. Sign-offs now record `opa_signoff` / `prolog_signoff`.
- Gate rows answered by a stand-in named Prolog as the deciding engine. They now name no engine and carry the sign-off text.
- Cards repeated a chip per round. Each degraded component is shown once.
- The health panel didn't say that reminders and reassessment timers wait while OPA or Prolog is down. It does now.

Left as they are (minor): the empty-deny-list check in the write-back retry, the stand-in text constant living in two modules, the release reasons being a hand copy of the Rego set, the refusal layer text, the lost classifier-down fact on the OPA-down path, the dropped reasons when one validator engine is down, the required role in the gate payload, the stale gate panel on poll, the ignored toggle response, the default move role, an outage-table read error counting as an outage, the cache race, the crash row naming both engines, a leftover import, and both processes needing `DEMO_OUTAGES`.
