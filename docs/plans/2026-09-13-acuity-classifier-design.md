# Acuity Classifier — grounding the model and retiring the red flags

**Date:** 2026-09-13
**Status:** agreed, not yet implemented
**Supersedes:** the red-flag pre-check in `app/actors/acuity_classifier.py`

---

## The problem

The classifier had no clinical grounding. Its entire instruction was three sentences
of persona in `acuity_classifier.json` — no ESI criteria, no examples, nothing to
judge against. It was classifying on whatever it absorbed in pretraining, and there
was no way to tell a good answer from a bad one.

Sitting in front of it was a hand-written red-flag regex that was supposed to be the
deterministic safety net. It wasn't one. The chest-pain pattern requires "chest"
immediately followed by pain/tightness/pressure, so the demo case's own free text —
"Patient reports pressure in the chest" — does not match. The oxygen rule
string-matches a number out of a stringified Python dict, recovering a value that was
already an integer two lines earlier.

## What we decided

**ESI 1–5 is the scale.** Closes Open decisions #1, which had it as a question.

**The model proposes the level.** It reads the case and makes the clinical judgment.
It is not reduced to answering sub-questions for a decision tree — that was considered
and rejected, because it removes the thing the model is actually good at.

**One rule from ESI runs alongside it: decision point D, the vitals check.** Computed
deterministically against the age-banded table, it **annotates the case and changes no
acuity**. See "Why the vitals check annotates rather than upgrades" below — the handbook's
own worked examples rule out a mechanical threshold.

**The red flags are deleted.** With them goes `acuity_source = rule_forced` and the
whole tangle of an advisory rule arguing with the nurse's number through the gap bands.

**Intake is structured. No free text.** Roughly ten tick-box and dropdown fields plus
vitals, rather than a prose box.

**No medical-specific model.** Keep the frontier model already wired through
OpenRouter.

## The shape

```
intake      ~10 structured fields + vitals + age band + nurse's acuity
                |
classifier  ALL FOUR decision points live here.
              model reads it all           (A, B, C by judgment)
                -> system_proposed_acuity, confidence, rationale
              vitals check                 (D, deterministic)
                -> danger_zone_vitals: ["hr"]   ANNOTATION. no level change.
                |
gate        resolve against the nurse
                gap 0   -> agreed value
                gap 1   -> the NURSE's value        (changed)
                gap 2+  -> charge nurse decides
```

Two judgments decide the level: the nurse's at intake and the classifier's. The vitals
check informs them both after the fact; it overrules neither.

## Why this shape

### Why the model proposes rather than answers sub-questions

ESI decision points A, B and C all require reading and judgment. B in particular —
"is this a high-risk presentation" — is defined in the ESI handbook by clinical
judgment with worked examples, deliberately *not* as a closed list. Any attempt to
enumerate it produces a local protocol that resembles ESI rather than ESI itself.

So the model does A, B and C implicitly when it proposes a level. We encode only D.

**This is one rule from ESI, not ESI.** Stated plainly here and in the spec so nobody
later assumes A–C are implemented.

### Why all four decision points live in the classifier

Classification is one actor's job. Splitting ESI across two modules would mean neither
file contains "the ESI algorithm" and the two copies drift. So `app/esi.py` holds the
thresholds as pure data and `acuity_classifier.classify()` is its only caller — the
actor returns a complete proposal, part judged by the model and part computed.

The node does what nodes do: writes what the actor returned. It computes nothing.

### Why the vitals check runs after the model, not before

The rest of the tree needs judgment inputs that only exist once something has read the
case. Only D is pure arithmetic over structured numbers, so only D can run
independently.

Running it *before* and feeding the result to the model was considered and rejected:
it anchors the model onto the computed answer, produces the identical outcome, and
costs the independent second opinion that makes model drift detectable. Handing the
model the ESI *rubric* is grounding and helps; handing it the ESI *answer* is anchoring
and does not.

### Why the vitals check annotates rather than upgrades

The first draft of this design had the rule raise any out-of-range case to emergent.
Reading the v5 handbook killed that. Decision point D is discretionary, and its own
worked examples prove it:

- **Example Four** (ch. 6) — 34-year-old, abdominal pain, HR 102 against a limit of
  100, every other vital normal. The handbook says assign **level 3**. Do not uptriage.
- **Example Five** — 72-year-old, SpO2 91% and RR 24, both breached. Uptriaged to 2 —
  but reasoned from an infected cat bite plus steroid-induced immunosuppression, not
  from the numbers. Her baseline SpO2 at home is 90–91%.

Same kind of breach, opposite conclusions, and the difference is context in both cases.
A mechanical rule would have got Example Four wrong in exactly the case the handbook
uses to teach restraint.

So the check computes exactly and annotates. Two judgments — the nurse's and the
classifier's — decide the level between them, and if neither thought the numbers
warranted a higher level, a threshold comparison does not overrule them. This is the
stance the spec already took on the retired red flags: rules inform, judgment decides.

**The known cost.** Decision point D exists to catch the "well-appearing ill" — the
patient whose presentation reassures and whose numbers do not. That is precisely the
case where both judgments may miss it, and the annotation is then the only signal.
The handbook cites nurse accuracy at decision point B as ~43%, so "both looked and
neither bumped" is a weaker guarantee than it sounds. Accepted deliberately.

**Consequence for sequencing.** The check changes no outcome, so its only value is the
annotation, and the annotation needs the patient card — which does not exist yet
(STATUS.md, "three things don't exist at all"). Build it when the board is built.

### What the evidence says

| | Overall ESI accuracy | High-acuity recognition |
| --- | --- | --- |
| Triage nurses | 65.2% | 32.7% |
| ChatGPT | 66.5% | 87.8% |
| Copilot | 61.8% | 85.7% |

The model earns its place on high-acuity recall, not on overall accuracy. Paediatric
comparison was wider still: nurses 53.1%, GPT-4o 76.1%.

On structured versus free-text input, one clean head-to-head on the same cohort:
structured + text AUC 0.93, **structured only 0.92**, text only 0.90. Losing 0.01 does
not justify a critical-path NER redaction model and a widened injection surface.

**Known bias:** GPT-4 assigned a median ESI of 2.0 where human experts assigned 3.0
(p < 0.001). Systematic over-triage. This is the reason the gap-1 rule changed to
nurse-wins — a consistently over-triaging model should not silently win every close
call. Monitor the rate of gap-1 resolutions and the size of the emergent bucket.

## Changes to existing decisions

| Was | Now | Why |
| --- | --- | --- |
| gap 1 takes the more acute value | gap 1 takes the nurse's value | model over-triage bias should not win close calls unseen |
| red-flag regex forces emergent | deleted | brittle, and `rule_forced` conflicted with the new gap rule |
| `free_text` field in intake | deleted | avoids PII-in-prose, which schema-drop cannot catch |
| BERT urgency scorer | deleted | its scores were never read by anything |
| acuity scale "(to confirm)" | ESI 1–5 | confirmed |

## Out of scope

**The safety actor still has no rules.** Deferred deliberately. When we return to it,
the open question is whether anything re-checks the *settled* acuity — the number that
comes out of the nurse-wins rule may be one no ESI computation produced.

**Decision points A, B and C are not encoded.** By design, see above.

## Open items

- **`rr` and `age_band` are not collected.** Vitals carry `hr`, `bp`, `spo2`, `temp_c`.
  Decision point D needs respiratory rate and the patient's age band. The age band comes
  from the CRM record — pass the band, not the date of birth, which is a direct
  identifier and is stripped by `drop_identifiers`.
- **The patient card does not exist.** The annotation has no display surface until the
  board is built.
- **The structured field list is not yet specified** beyond "roughly ten". Needs a pass
  with someone clinical.
- **No eval set exists.** Labelled cases with known-correct ESI levels are the only way
  to tell whether any of this works. This is the largest remaining gap.

## The danger-zone table

Transcribed from the ESI v5 handbook, Figure 6-1
(`docs/Emergency_Severity_Index_Handbook.pdf`, ch. 6).

| Age band | HR (beats/min) | RR (breaths/min) |
| --- | --- | --- |
| < 1 month | > 190 | > 60 |
| 1–12 months | > 180 | > 55 |
| 1–3 years | > 140 | > 40 |
| 3–5 years | > 120 | > 35 |
| 5–12 years | > 120 | > 30 |
| 12–18 years | > 100 | > 20 |
| > 18 years | > 100 | > 20 |

SpO2 < 92% at every age.

ESI defines **one** threshold per band. There is no "significantly above" tier — the
handbook routes genuinely unstable vitals to level 1 at decision point A instead ("a
heart rate of 32 or 180 ... the patient is unstable and should be assigned ESI level 1
no matter how 'good' the patient appears"), but gives no number for it.

Also at decision point B, not D: **an infant under 28 days with a fever is at least
ESI level 2.**

## Resource list for the prompt (decision point C)

From Table 5-1. Count *types*, not individual tests — a CBC and a urinalysis together
are one resource.

| Counts as a resource | Does not |
| --- | --- |
| Labs (blood, urine) | History and physical exam |
| ECG, radiographs | Point-of-care testing |
| CT, MRI, ultrasound, angiography | Saline or heparin lock |
| IV fluids (hydration) | Oral medications |
| IV, IM or nebulised medications | Tetanus immunisation, prescription refills |
| Specialty consultation | Phone call to primary care physician |
| Simple procedure = 1 (laceration repair, urinary catheter) | Simple wound care (dressings, recheck) |
| Complex procedure = 2 (procedural sedation) | Crutches, splints, slings |

## Sources

- [GPT-4 vs medical experts, J Clin Nurs](https://onlinelibrary.wiley.com/doi/10.1111/jocn.17490)
- [ChatGPT/Copilot vs triage nurses, Am J Emerg Med](https://pubmed.ncbi.nlm.nih.gov/39731895/)
- [Paediatric triage comparison](https://pmc.ncbi.nlm.nih.gov/articles/PMC12953526/)
- [Tabular vs free-text triage ML](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC7809037/)
- [ESI Handbook 5th edition](https://californiaena.org/wp-content/uploads/2023/05/ESI-Handbook-5th-Edition-3-2023.pdf)
