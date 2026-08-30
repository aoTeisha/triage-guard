# Triage Guard: Architecture and State-Machine Specification

**Project:** Triage Guard, an AI-Governed ER Triage System

**Authors:** Idan Beck, Barak Amir

**What this is.** This document is the complete specification for **Triage Guard**, an AI-governed emergency-room (ER) triage system. It is designed to be self-contained, with each section starting with a brief introduction explaining its content and purpose. Please read the **Conventions & scope** section first, since all later sections rely on its definitions.

**Companion diagram.** This document matches the Triage Guard architecture diagram at [`triage-guard-system.png`](../assets/triage-guard-system.png). The **arrow numbers** in the Transitions table and scenario paths are the same as those shown on the diagram, so you can easily find any row in both places. Agent names, world statuses, and the "notify user" list also correspond directly to the diagram. Mermaid versions of the architecture and the control-plane state machine are included in the companion file **[`diagrams.md`](../diagrams.md)**.

**Open items.** The safety-fail branch of the human gate - previously the one unresolved core decision - is now **resolved**; the resolution is recorded in the resolved section below and modeled in detail in the "Safety-Fail Branch" section of [`SYSTEM_MODELING.md`](./SYSTEM_MODELING.md). Document intake uses **mock questionnaire data (no live OCR)**, and the method along with its demo cases is explained in **Document intake & demo data**. In other sections, items that still need to be finalized are marked as _(to confirm)_ or _(to define)_.

**Companion system model.** A focused risk-model of the highest-risk parts of the system - the treatment-move execution (including its `UNKNOWN` state and reconciliation), the interface contract, the safety-fail branch, and the capacity/reversibility design - is in [`SYSTEM_MODELING.md`](./SYSTEM_MODELING.md). Sections below reference it where relevant.

**Contents**

- [Problem definition & system value](#problem-definition--system-value)
- [How the pieces fit together](#how-the-pieces-fit-together)
- [Tech stack](#tech-stack)
- [Conventions & scope](#conventions--scope)
- [Document intake & demo data](#document-intake--demo-data)
- [Identity resolution & patient data](#identity-resolution--patient-data)
- [Local CRM stub (SQLite)](#local-crm-stub-sqlite)
- [Actors / Agents](#actors--agents)
- [States](#states)
- [Events](#events)
- [Guards / Conditions](#guards--conditions)
- [Actions](#actions)
- [Transitions (complete table)](#transitions-complete-table)
- [Context / State variables (Data plane)](#context--state-variables-data-plane)
- [Queue ordering rule](#queue-ordering-rule)
- [Neuro-Symbolic Architecture](#neuro-symbolic-architecture)
- [Safety invariants](#safety-invariants)
- [Temporal logic rules](#temporal-logic-rules)
- [Symbolic governance layer: OPA, Z3, Prolog, Datalog](#symbolic-governance-layer-opa-z3-prolog-datalog)
- [Open decisions](#open-decisions)
- [Safety-fail branch of the human gate (resolved)](#safety-fail-branch-of-the-human-gate-resolved)

---

## Problem definition & system value

**The problem.** Emergency-department triage assigns each arriving patient an acuity level (how urgently they must be seen). Done by a single overworked nurse under load, it is error-prone; done by an LLM alone, it is unaccountable and unsafe. Triage Guard puts an LLM in the loop for the part it is good at (reading messy clinical input and proposing an acuity) while a symbolic governance layer enforces the parts that must never be left to a stochastic model (safety validation, authorization, ordering fairness, privacy, and human sign-off on contested cases).

**Users.** Triage nurses (submit the intake form, propose acuity), charge nurses / shift leads (resolve contested cases, sign releases), and an ops technician (handles agent outages). Patients are the subjects, not operators.

**What counts as success.** Every patient gets a defensible acuity and a queue position; no patient is ordered ahead of a more acute one; no case reaches treatment without passing safety and any required approval; no patient identifier reaches the model; every state-changing action is logged and explainable; and the system keeps running (degraded, not stopped) when a non-critical component fails.

**Risk if it misfires.** Under-triage (a critically ill patient queued as routine) can be fatal; a privacy leak exposes patient identity; an unlogged or unexplainable decision is indefensible clinically and legally. These are the failures the symbolic layer exists to make impossible, not merely unlikely.

**Which decisions need formal control.** Acuity resolution when the nurse and system disagree, any move into treatment, any release/close, any handling of patient identifiers, and the ordering of the waiting queue. Each of these is gated by an explicit guard and a symbolic layer, never by the model alone.

---

## How the pieces fit together

_This is a one-screen map of the system, giving the rest of the document a clear starting point. The system uses a single control loop, called the Orchestrator, which is supported by specialist agents and services._

- **Input Channel** is where a case starts: the **website** intake form (a structured webform).
- **Input processing** uses an **API Gateway** to pass along the raw input.
- **Intake Parser (ingestion)** reads the submitted form and turns it into a single, unified message. In this version, the form uses **mock / structured data** (no live OCR). A real deployment could add an OCR or extraction adapter behind the same interface.
- **Understanding, safety & routing** includes a **PII / sensitive-data filter** (keeps identifiers off the model payload), a **policy gate**, and a **sentiment/urgency** scorer (distress or pain).
- **Agent Core** includes the **Orchestrator Agent** (the only one that writes to State) and six specialist agents: Intake Parser, Acuity Classifier, Safety Validation, Human Escalation, Waiting Room Monitor, and Audit.
- **Knowledge & Memory** covers the Knowledge Base, policy vector store, CRM (patient profile and history), and session memory.
- **Monitoring & Evaluation** includes user and agent feedback, an evaluation pipeline (test cases and regression checks), logs and traces, and a metrics dashboard.

---

## Tech stack

_This section shows the tools and technologies we are using in the project._

| Component           | Tech                                                      | Role                                                                                                                                  |
| ------------------- | --------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| Agent orchestration | **CrewAI**                                                | Orchestrator + specialist agents (Intake Parser, Acuity Classifier, Safety Validation, Human Escalation, Waiting Room Monitor, Audit) |
| Text analysis       | **BERT (or another BERT-based model trained for Hebrew)** | sentiment/urgency scorer and PII detection in the Input Normalizer stage                                                              |
| Observability       | **Langfuse**                                              | traces, logs, and eval pipeline feeding **Monitoring & Evaluation**                                                                   |
| Package management  | **uv**                                                    | Python dependency management                                                                                                          |

---

## Conventions & scope

_This section introduces the key terms and concepts used throughout the document. It covers naming conventions, the three planes where a case can exist, who is allowed to write State, and how failures are handled. Please start here, since later sections depend on these definitions._

- **Events** are `UPPERCASE_SNAKE`; **states** are `lowercase_snake`; **guards** are boolean predicates; **actions** are verbs.
- The machine has **three state planes**:
  - **Control**: where the Orchestrator is in processing a case (its position in the pipeline).
  - **Data**: the parsed or derived payload, including fields, acuity, confidence, and verdict.
  - **World**: the patient's clinical status on the board (the kanban column).
  - _Control and World evolve semi-independently: a case can hold control-state_ `monitoring` _while its World status cycles_ `waiting -> treatment_started -> released`_. They are kept as separate planes so the machine is not the cross-product of the two._
- **Writer rule:** agents propose, they never write. The **Orchestrator is the sole writer to State.** Every agent arrow terminates at the Orchestrator.
- **Design stance: fail-operational.** Unless a critical component fails, the system keeps working. Non-critical agents switch to a human or manual fallback. Critical agents either switch to human fallback or stop completely (see the **Per-agent failure model**).
- **Ingestion stance: no live OCR.** The triage intake is a **structured webform** (mock data in this build); there is no OCR pipeline, but a real deployment could add one behind the same interface. Acuity is always supplied by the nurse and never inferred from the raw input. See **Document intake & demo data**.
- **Identity vs. model input.** The system holds full patient identifiers (they are needed to look up the record); it simply never puts them into the LLM-facing payload. Identity is resolved first (stable-ID lookup), then the model reasons over a `case_id`-keyed clinical payload with no name/ID. See **Identity resolution & patient data** and the **No identifiers in model input** invariant.
- The **arrow numbers** in the Transitions table match the arrows on the diagram.
- **Notation.** Acuity follows the ESI style, where a lower number means more acute (1 is most urgent, 5 is least). Logic operators: `∧` means and, `∨` means or, `¬` means not, and `->` means implies. Temporal operators (used in **Temporal logic rules** and **Safety invariants**): `G` for always/globally, `F` for eventually, `X` for next step, `U` for until, and `F≤t` for eventually within a set time t.

---

## Document intake & demo data

This section explains how a case enters the system and describes the four mock inputs used in the demo. There is no live OCR. Instead, the form's content is provided as mock / structured webform data, and each mock case tests one of the four intake outcomes. These four cases also serve as the intake regression and demo set: success, degraded, failed, and rejected.

The Intake Parser validates the submitted webform and produces one of four possible outcomes. Each outcome matches an existing transition out of `parsing`, so no new components are needed. The mock data simply determines which branch is used.

| Demo case                          | Mock input                                                                            | Intake outcome (event)                                          | Arrow | Lands in                   | System response                                                                                                                 |
| ---------------------------------- | ------------------------------------------------------------------------------------- | --------------------------------------------------------------- | ----- | -------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| 1 : Clean submission               | complete webform, all required fields present                                         | `DATA_PARSED` (`required_fields_complete` and `input_is_valid`) | 4     | `data_parsed` -> continues | proceed to identity resolution, then redaction, then classification                                                             |
| 2 : Partial or missing fields      | some required fields absent                                                           | `MISSING_FIELDS_DETECTED`                                       | 16    | `missing_fields_requested` | generate a form listing **exactly the missing fields**; nurse completes it, then `FIELDS_SUBMITTED` (arrow 1b.x), then re-parse |
| 3 : Failed / incomplete submission | submission failed or nothing usable was received                                      | `SUBMISSION_FAILED`                                             | 17    | `submission_failed`        | offer to **resubmit the form** or switch to **full manual entry** (nurse fills the whole form)                                  |
| 4 : Invalid or injection input     | submission does not match the expected schema, or free-text contains prompt-injection | `INVALID_INPUT_DETECTED`                                        | 18    | `input_rejected`           | reject; notify "invalid input"; log a security event; **never proceeds to classify**                                            |

> **Acuity is never inferred by the system at intake.** The nurse must always supply `nurse_proposed_acuity` on the form. If this field is missing, the case is handled as demo case 2 (missing fields), not by an automatic guess.
>
> **Case 4 is also the injection defence.** A submission whose free-text field contains prompt-injection text is flagged with `reason = injection` and rejected at intake; it never reaches the classifier, which satisfies the **Injection rejected** invariant.

---

## Identity resolution & patient data

**Two different jobs, in order.** "Handle PII" is really two separate steps that must happen in sequence:

1. **Identity resolution (needs the real ID).** Right after a clean parse, the Orchestrator looks the patient up in the CRM using a **stable patient ID only** (national ID). This is the `resolving_identity` state; its entry action is `fetch_patient_data`.
2. **Payload construction (drops the ID).** The system then merges stored history with this visit's new data and builds the **model-facing payload**: clinical fields only (symptoms, vitals, history), keyed by `case_id`, with **no name/ID**. The identifiers stay on the case record (the nurse and Orchestrator still see them); they simply never enter the classifier/safety-validator input.

**Why identifiers are kept out of the model input:**

- **The classifier doesn't need them.** Acuity is a function of symptoms and vitals; a name cannot make a patient more or less acute.
- **Bias.** A name can leak ethnicity/gender/age into the decision. Excluding identity is a fairness safeguard, not only a privacy one.
- **Leak surface / compliance.** The LLM is the component reading patient-supplied text and (if generative) the one that could be induced to echo its context. If it never received a name/ID, a compromised model has nothing to leak.

**New patient vs. DB down (both continue on intake-only data, different logging):**

- **No record found** (new patient, nothing stored): **normal**, silent, no flag. Triage proceeds on this visit's data.
- **DB unreachable:** **same continue-path** (triage on intake-only data) but **flag the case and alert the technician**, because that is a failure, not an expected empty. The CRM is therefore **non-critical / fail-open** (see the Per-agent failure model).

**Write-back.** This visit's new clinical data is persisted to the CRM (`patch_patient_data`) so the record stays current; when the DB was unreachable, the write-back is deferred and reconciled once it returns.

> **Implementation note.** There is no external CRM in this project. The CRM is a **local
> SQLite database** with mock patient records, behind the same contract the rest of the
> system uses (`fetch_patient_data`, `patch_patient_data`, with `found` / `not-found` /
> `db-error` outcomes). Because the contract is identical, the local stub can later be
> swapped for a real CRM without changing the architecture. Schema and interface are in
> **[Local CRM stub (SQLite)](#local-crm-stub-sqlite)**.

---

## Local CRM stub (SQLite)

_The CRM is the one external dependency the system reads patient history from. In this
project it is not a real external system - it is a local SQLite database with mock records,
implemented behind the exact contract described above so it can be replaced by a real CRM
without touching the rest of the architecture. This section defines that stub's schema,
interface, and failure behavior._

**Why SQLite (not an in-memory mock).** A local SQLite file gives real persistence
(write-backs survive restarts), a real query surface, and a way to simulate a `db-error`
(close or lock the connection) so the CRM's fail-open path can be exercised, not just
described - none of which a plain in-memory dict provides. It needs no server and no
container, and ships as a single file in the repo.

### Schema

One table is enough for the contract. History is stored as JSON so a record maps directly
to the `PatientRecord` the Orchestrator expects.

| Column              | Type                 | Notes                                                  |
| ------------------- | -------------------- | ------------------------------------------------------ |
| `stable_patient_id` | TEXT, primary key    | national ID; the only lookup key                       |
| `name`              | TEXT                 | held by the CRM layer only, never in the model payload |
| `date_of_birth`     | TEXT (ISO date)      | identifier-class data                                  |
| `known_conditions`  | TEXT (JSON array)    | e.g. `["diabetes", "hypertension"]`                    |
| `prior_visits`      | TEXT (JSON array)    | prior visits: date, acuity, notes                      |
| `last_updated`      | TEXT (ISO timestamp) | set on every write-back                                |

_All identifier-class columns (`name`, `date_of_birth`) stay on the CRM/Orchestrator side
under `actor_authorized` and are dropped when the model-facing payload is built - the same
"no identifiers in model input" rule the rest of the spec enforces._

### Interface

The stub implements exactly the two actions the spec already names, plus a health check,
so the Orchestrator code is identical whether the CRM is local or real.

| Operation                                           | Returns                                    | Maps to                            |
| --------------------------------------------------- | ------------------------------------------ | ---------------------------------- |
| `fetch_patient_data(stable_patient_id)`             | `found(record)` / `not_found` / `db_error` | `fetch_patient_data`, arrow 4b     |
| `patch_patient_data(stable_patient_id, visit_data)` | `ok` / `db_error`                          | `patch_patient_data` write-back    |
| `is_available()`                                    | `true` / `false`                           | health check for degrade decisions |

The three `fetch` outcomes map onto the guards already in the spec:
`found` and `not_found` both mean `db_reachable` is true (a new patient with no record is a
normal empty, not a failure); `db_error` means `¬db_reachable`, which triggers the
fail-open degrade path (continue on intake-only data, flag, `alert_technician`, defer the
write-back).

### Simulating `db-error`

The stub exposes a switch (for example an env flag `CRM_SIMULATE_DOWN=true` or a test
helper) that forces `fetch`/`patch` to return `db_error`, so the `AF·db` transition and
the CRM fail-open row in the per-agent failure model can be tested end-to-end rather than
only described. When the switch is cleared, deferred write-backs reconcile as normal.

### Swap-out path

Because the interface above is the whole contract, replacing the stub with a real CRM
means providing another implementation of the same three operations. No state, guard,
transition, or invariant elsewhere in the spec depends on the CRM being local - the stub is
a dependency, not part of the system boundary.

---

## Actors / Agents

This section lists all participants in a case: the Orchestrator, each proposing agent, the humans, and the ops technician. It explains what each one can read, what it can propose, and most importantly, whether it can write State. The key column is "Writes State?" Only the Orchestrator can do this.

| Actor                                             | Type                                              | Reads                                          | Proposes / Emits                                                                                          | Writes State?         | Tech                                       |
| ------------------------------------------------- | ------------------------------------------------- | ---------------------------------------------- | --------------------------------------------------------------------------------------------------------- | --------------------- | ------------------------------------------ |
| **Orchestrator Agent**                            | Controller / planner                              | all planes                                     | (it decides)                                                                                              | **Yes, sole writer**  | State machine / workflow graph             |
| Intake Parser Agent                               | Validator (deterministic)                         | webform / structured intake (mock)             | `Data_Parsed` proposal / field-presence check                                                             | No                    | Deterministic schema validation            |
| Acuity Classifier Agent                           | LLM (+ internal deterministic red-flag pre-check) | model-facing payload (case_id-keyed) + history | `system_proposed_acuity` + confidence; a red-flag rule can force emergent (`acuity_source = rule_forced`) | No                    | LLM + deterministic rules                  |
| Safety Validation Agent                           | Deterministic                                     | proposed classification                        | verdict (pass/fail)                                                                                       | No                    | Prolog / Datalog / Z3 / OPA                |
| Human Escalation Agent                            | Bridge to human                                   | case + verdict                                 | escalation + human response                                                                               | No                    | UI/queue _(to confirm)_                    |
| Waiting Room Monitor Agent                        | Timer / watcher                                   | status + timers                                | timeout / deterioration triggers                                                                          | No                    | _(to confirm)_                             |
| Audit Agent                                       | Logger                                            | event log                                      | persists trace                                                                                            | No                    | append-only store _(to confirm)_           |
| Channel Router                                    | Ingress                                           | raw input                                      | route website submission                                                                                  | No                    |                                            |
| Input Normalizer + PII filter + Sentiment/Urgency | Pre-processor                                     | routed input                                   | model-facing payload (identifiers excluded) + distress/pain score                                         | No                    | Schema-drop (identifiers) + BERT (urgency) |
| CRM / Patient DB                                  | Data store (local SQLite stub)                    | stable patient ID                              | patient record (history)                                                                                  | No                    | CRM (non-critical, fail-open)              |
| Triage Nurse / Charge Nurse                       | Human                                             | board + detail panel                           | status changes, approvals, acuity, missing fields, release sign-off                                       | via Orchestrator only |                                            |
| Technician                                        | Human (ops)                                       | agent-failure alerts                           | fixes / acknowledges                                                                                      | No                    |                                            |

---

## States

This section lists all the possible states a case can have. The states are grouped into three planes and a separate recovery or error group. The control plane tracks the case's place in the workflow. The recovery or error group explains what happens if something goes wrong. The world plane shows what nurses see on the board.

### Control plane (workflow position)

_The happy-path pipeline: where the Orchestrator is in processing a case, from the moment it arrives to the moment its card leaves the board._

| State                     | Type                  | Entry action                                                 | Exit action     | Invariant                            | Arrow      |
| ------------------------- | --------------------- | ------------------------------------------------------------ | --------------- | ------------------------------------ | ---------- |
| `intake_received`         | initial               | log intake; **assign** `order_key`                           |                 | channel + raw payload present        | 1a         |
| `parsing`                 | normal                | invoke Intake Parser                                         |                 | input routed                         | 3          |
| `data_parsed`             | normal                |                                                              |                 | fields extracted OR flagged missing  | 4          |
| `resolving_identity`      | normal                | `fetch_patient_data` (stable-ID lookup)                      |                 | stable patient ID present            | 4b         |
| `redacting_routing`       | normal                | build model-facing payload (drop identifiers), score urgency |                 | no identifiers in model input        | 5, 6       |
| `classifying`             | normal                | invoke Acuity Classifier                                     |                 | redacted payload ready               | 7          |
| `acuity_proposed`         | normal                | compute `acuity_gap`                                         |                 | proposed acuity + confidence present | 8          |
| `safety_validating`       | normal                | invoke Safety Validation                                     |                 | settled acuity present               | 9          |
| `verdict_proposed`        | normal                |                                                              |                 | verdict present                      | 10         |
| `awaiting_human_approval` | **wait / human gate** | invoke Human Escalation; start notify-ladder                 | record response | approval pending                     | 11, 12, 20 |
| `monitoring`              | normal                | start reassessment timer                                     | stop timer      | patient has active status            | 13         |
| `case_closed`             | terminal              | emit final log; **card leaves the board**                    |                 | released, human-signed               | REL        |

`order_key` is assigned when the case enters the system (in `intake_received`). Each case gets a queue position right away, even if its first step is the gate.

> **Note on** `blocked`**.** A blocked action is **not** a resting state of the case. When a formal layer (OPA / Prolog / Z3 / Datalog / Temporal) denies an attempted action, the _attempt_ is refused, logged with the denying layer and a Prolog explanation, and the **case stays in its current state**. This is the `BLK` row in the Transitions table. It matches the rubric's "Violation detected -> Blocked" outcome without inventing a phantom case-state.

### Recovery / error states (control plane)

This section explains error handling. Each row shows where the system can end up if something fails, what kind of recovery happens, and what happens next. The last row covers the general agent-failure catch, explained in the Per-agent failure model.

| State                      | Recovery kind                               | Trigger                                                  | Resolves to                                                                                                                    |
| -------------------------- | ------------------------------------------- | -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `missing_fields_requested` | Request, then retry                         | required field absent (demo case 2)                      | back to `parsing` on `FIELDS_SUBMITTED` (16, then 1b.x)                                                                        |
| `submission_failed`        | Retry / Manual                              | submission failed, nothing usable received (demo case 3) | resubmit, back to `intake_received` (17, then 1a·resubmit), **or** full manual entry by the nurse                              |
| `input_rejected`           | Reject / Abort                              | invalid schema or injection (demo case 4)                | back to `intake_received` + "invalid input" (18, then 1a·rejected)                                                             |
| `reassessment_required`    | Replan                                      | timer timeout / deterioration                            | **re-enter at** `parsing`, nurse re-files (14, then 15)                                                                        |
| `agent_failed`             | **per-agent (see Per-agent failure model)** | any agent errors/times out past its retry budget         | technician resolves, `AGENT_RECOVERED`, **re-enter at the failed stage** (arrow AF·recover); degrade/halt per agent until then |

### World plane (clinical status = board column)

This section shows what the nurse actually sees: the kanban column for each patient. The system sets these columns, except where marked with a hand symbol for human input. The release rule and the manual-edit rule below are the two main constraints.

| Status                  | Who sets it          | Meaning                                                | Reachable from                                            |
| ----------------------- | -------------------- | ------------------------------------------------------ | --------------------------------------------------------- |
| `waiting`               | system               | queued for bed/provider, priority-sorted (1 = highest) | after triage decision                                     |
| `human_review`          | system               | flagged uncertainty; charge nurse must resolve         | acuity gap ≥2 / safety fail / low confidence              |
| `reassessment_required` | system               | vitals/condition changed, re-triage                    | any status, on deterioration/timeout                      |
| `treatment_started`     | human                | active care begun                                      | `waiting` (manual)                                        |
| `formal_validation`     | system               | final sign-off before close                            | `treatment_started`                                       |
| `patient_released`      | **human (required)** | discharged, leaves the board                           | **any active state** (discharge / AMA / transfer / admit) |

> **Release rule:** `patient_released` is reachable from **any** live state, not only the treatment path. Every release **requires an authorized nurse sign-off** (`actor_authorized`) plus a valid `release_reason`. "Left the ward vs. left the hospital" is out of scope; both close the card.
>
> **AMA consequence:** the patient may physically leave before the card closes; the card stays open until a nurse signs. A monitor must **not** treat an unsigned-but-departed card as "still waiting."
>
> **Manual-edit rule:** nurses manually set status only for `treatment_started` (from `waiting`) and for release (any state, with reason). All other statuses are system-set.

> **Execution vs. status (treatment move).** The World-plane status above is the board
> column. When the move into `treatment_started` is _executed_ through a downstream system
> (not a pure manual column change), that execution is an irreversible side effect with its
> own states, because the downstream call can time out with no receipt. Those states are
> `PENDING` -> `CONFIRMED` / `FAILED` / `UNKNOWN`, plus `RECONCILING` and
> `ESCALATED_TO_HUMAN`. The rule that matters: **a timeout is `UNKNOWN`, not `FAILED`.** A
> blind retry from `UNKNOWN` could start treatment twice, so from `UNKNOWN` the system
> first **reconciles** - queries the source of truth (audit / downstream re-query) to learn
> whether the move happened - and only a reconciled `FAILED` permits a retry, carrying the
> same `idempotency_key`. Full state machine, contract, and evidence: see the "Execution State Machine" and "Interface Contract & Failure Handling" sections of [`SYSTEM_MODELING.md`](./SYSTEM_MODELING.md).

---

## Events

Inputs that move the state machine come from external sources (humans or channels), internal agent proposals, timers, or errors. Payload details matter. For example, `APPROVAL_RESPONSE_RECEIVED` includes a resolution choice, and `RELEASE_REQUESTED` includes a reason.

| Event                            | Source                     | Payload                                                                  | Type                      |
| -------------------------------- | -------------------------- | ------------------------------------------------------------------------ | ------------------------- |
| `CASE_SUBMITTED`                 | Channel (website)          | channel                                                                  | external                  |
| `FIELDS_SUBMITTED`               | Nurse                      | missing field values                                                     | external (human)          |
| `MOVE_REQUESTED`                 | Nurse                      | target status                                                            | external (human)          |
| `TREATMENT_COMPLETE`             | Nurse                      | case id                                                                  | external (human)          |
| `APPROVAL_RESPONSE_RECEIVED`     | Charge Nurse               | resolution (one of: use_system_acuity, use_nurse_acuity) + resolver_role | external (human)          |
| `MESSAGE_NORMALIZED`             | Input Normalizer           | unified message                                                          | internal                  |
| `PATIENT_RESOLVED`               | Orchestrator, CRM          | patient record found / not-found / db-error                              | internal                  |
| `DATA_PARSED`                    | Intake Parser              | parsed fields (incl. `nurse_proposed_acuity`, `stable_patient_id`)       | internal (proposal)       |
| `MISSING_FIELDS_DETECTED`        | Intake Parser              | list of gaps                                                             | internal                  |
| `SUBMISSION_FAILED`              | Intake Parser              | error reason                                                             | internal (error)          |
| `INVALID_INPUT_DETECTED`         | Intake Parser / PII filter | reason (one of: invalid_schema, injection)                               | internal (error/security) |
| `REDACT_ROUTE_DONE`              | Understanding/routing      | model-facing payload + urgency                                           | internal                  |
| `ACUITY_PROPOSED`                | Acuity Classifier          | `system_proposed_acuity` + confidence (may be red-flag forced)           | internal (proposal)       |
| `VERDICT_PROPOSED`               | Safety Validation          | pass/fail + reasons                                                      | internal (proposal)       |
| `ESCALATION_PROPOSED`            | Human Escalation           | needed? (bool)                                                           | internal (proposal)       |
| `REASSESSMENT_TIMEOUT`           | Waiting Room Monitor       | patient id                                                               | timer                     |
| `DETERIORATION_DETECTED`         | Waiting Room Monitor       | patient id + signal                                                      | internal                  |
| `TRANSITION_ACCEPTED`            | Orchestrator, user         | new status                                                               | notification              |
| `RELEASE_REQUESTED`              | Nurse                      | reason (one of: discharge, ama, transfer, admit) + actor                 | external (human)          |
| `ACTION_DENIED`                  | Orchestrator (governance)  | denying layer + reason                                                   | internal (governance)     |
| `AGENT_FAILED`                   | Orchestrator               | agent + error + attempts                                                 | internal (error)          |
| `AGENT_RECOVERED`                | Technician, Orchestrator   | agent id                                                                 | external (ops)            |
| `GATE_TIMER_ASSIGNED_NURSE`      | Waiting Room Monitor       | case id                                                                  | timer                     |
| `GATE_TIMER_ESCALATE_ANY_CHARGE` | Waiting Room Monitor       | case id                                                                  | timer                     |
| `EVENT_LOGGED`                   | Orchestrator               | trace record                                                             | emitted                   |

---

## Guards / Conditions

These are the boolean checks that decide which transition happens. This is where policy is set: acuity-gap bands, authorization, and retry budget.

| Guard                      | Expression                                                                                                                                                                                                                                                         | Evaluated by          |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------- |
| `required_fields_complete` | all mandatory fields present (incl. `nurse_proposed_acuity`, `stable_patient_id`)                                                                                                                                                                                  | Orchestrator          |
| `input_is_valid`           | submission matches expected schema ∧ ¬injection                                                                                                                                                                                                                    | Parser / PII filter   |
| `patient_found`            | CRM returned a record for the stable ID                                                                                                                                                                                                                            | Orchestrator          |
| `db_reachable`             | CRM responded (found or empty), i.e. not a DB error                                                                                                                                                                                                                | Orchestrator          |
| `confidence_ok`            | `confidence >= threshold` _(threshold to confirm)_. Optional: wire in (low confidence, then gate) or drop. Not applicable during a classifier outage.                                                                                                              | Orchestrator          |
| `safety_pass`              | verdict == pass                                                                                                                                                                                                                                                    | Safety Validation     |
| `escalation_needed`        | verdict fail ∨ low confidence ∨ policy hit                                                                                                                                                                                                                         | Human Escalation      |
| `move_authorized`          | target status change permitted for this actor (e.g. `waiting -> treatment_started`) ∧ `actor_authorized` ∧ `safety_passed` ∧ `approved`. Enforces no-approval-bypass: a treatment move is refused unless the case already passed safety and any required approval. | Orchestrator          |
| `release_authorized`       | valid `release_reason` ∧ `actor_authorized`. A _reason plus authorization_ check, **not** a source-state check.                                                                                                                                                    | Orchestrator          |
| `actor_authorized`         | role ∧ jurisdiction ∧ data-class OK                                                                                                                                                                                                                                | Orchestrator (policy) |
| `retry_budget_left(agent)` | `retry_count[agent] < N[agent]`                                                                                                                                                                                                                                    | Orchestrator          |
| `acuity_agree`             | `acuity_gap == 0`                                                                                                                                                                                                                                                  | Orchestrator          |
| `acuity_gap_minor`         | `acuity_gap == 1`                                                                                                                                                                                                                                                  | Orchestrator          |
| `acuity_gap_major`         | `acuity_gap >= 2`                                                                                                                                                                                                                                                  | Orchestrator          |
| `actor_is_charge`          | `actor_authorized` ∧ role ∈ {charge_nurse, shift_lead}                                                                                                                                                                                                             | Orchestrator          |

> `actor_authorized`: the person is allowed to act: right **role** (nurse / charge nurse), patient in their **jurisdiction** (ward/shift), and **clearance** matches the data class. Reused wherever an action needs a person behind it.
>
> `release_authorized`: the case may be closed by release when there is a **valid reason** (discharge / AMA / transfer / admit) **and** an authorized signer. State-independent: release can happen from anywhere.
>
> `acuity_gap` = the absolute difference between `nurse_proposed_acuity` and `system_proposed_acuity`.
>
> `safety_passed` / `approved` = case-level flags set when `safety_validating` returns pass and (where required) the human gate resolves. `move_authorized` reads these so treatment can never start on an unvalidated/unapproved case.
>
> `jurisdiction`: the actor's assigned organizational scope (ward/unit/shift). `data-class`: the sensitivity tier of the data being accessed (full patient info vs. redacted), which must match the actor's clearance. `N`: max retries, set per agent, not globally.

---

## Actions

These actions are the side effects a transition can trigger.

| Action                          | Performed by                   | Side-effecting?        | Idempotent?                          | Output                                 | Notes / Arrow                                                                                                                            |
| ------------------------------- | ------------------------------ | ---------------------- | ------------------------------------ | -------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `write_state(plane, value)`     | Orchestrator only              | yes                    | yes (deduped by `trace_id`)          |                                        | the single write path                                                                                                                    |
| `invoke_intake_parser`          | Orchestrator                   | no                     | yes                                  | ParseResult                            | 3 (deterministic field-presence + schema check)                                                                                          |
| `fetch_patient_data`            | Orchestrator, CRM              | no (read)              | yes                                  | PatientRecord                          | 4b, stable-ID lookup; found / not-found / db-error                                                                                       |
| `patch_patient_data`            | Orchestrator, CRM              | yes                    | _(to confirm)_                       |                                        | write-back of this visit's new data; deferred if DB was down                                                                             |
| `build_model_payload`           | Orchestrator, Input Normalizer | no                     | yes                                  | RedactedMsg + scores                   | 5, merge history + new data; **drop identifiers**; key by `case_id`; score urgency                                                       |
| `invoke_acuity_classifier`      | Orchestrator                   | no                     | yes                                  | {system_proposed_acuity, confidence}   | 7 (agent runs its internal red-flag pre-check, then the LLM; a red-flag match returns emergent with `acuity_source=rule_forced`)         |
| `invoke_safety_validation`      | Orchestrator                   | no                     | yes                                  | {verdict, reasons}                     | 9                                                                                                                                        |
| `invoke_human_escalation`       | Orchestrator                   | yes (queues human)     | yes (dedupe by case_id)              | ApprovalRequest                        | 9c / 11                                                                                                                                  |
| `start_reassessment_timer`      | Orchestrator                   | yes                    | yes (dedupe by case_id)              | TimerHandle                            | 13                                                                                                                                       |
| `notify_user(reason)`           | Orchestrator                   | yes                    | yes (dedupe by `case_id` + `reason`) | Notification                           | reassessment / missing / resubmit / invalid-input / accepted / approval                                                                  |
| `emit_event_log`                | Orchestrator, Audit            | yes (append)           | yes                                  | TraceRecord                            | every transition                                                                                                                         |
| `explain_denial`                | Orchestrator, Prolog           | no                     | yes                                  | Explanation                            | BLK, the "why" string for a denied action                                                                                                |
| `auto_resolve_acuity_upward`    | Orchestrator                   | yes                    | yes                                  | acuity                                 | `acuity <- more acute of {nurse, system}` (lower ESI); `acuity_source <- auto_resolved`; recompute `order_key`; log both inputs + choice |
| `apply_human_acuity(choice)`    | Orchestrator                   | yes                    | yes                                  | acuity                                 | `acuity <- choice`; `acuity_source <- human_confirmed`; recompute `order_key`; log resolver + role                                       |
| `sign_release(reason)`          | Orchestrator, nurse            | yes                    | yes                                  | ReleaseRecord                          | record reason + actor; then `case_closed`                                                                                                |
| `alert_technician(agent)`       | Orchestrator, Technician       | yes                    | yes                                  | Alert                                  | fires ops alert; does not block degraded flow                                                                                            |
| `fallback_manual(agent)`        | Orchestrator                   | yes                    | yes                                  |                                        | switch a fail-open agent to its human/manual substitute                                                                                  |
| `resume_at_failed_stage(agent)` | Orchestrator                   | yes                    | yes                                  |                                        | on `AGENT_RECOVERED`, re-enter the pipeline at the stage that failed                                                                     |
| `execute_treatment_move`        | Orchestrator, Tool Gateway     | yes (**irreversible**) | yes (dedupe by `idempotency_key`)    | ActionRequest -> ToolReceipt / timeout | the one irreversible external action; the Gateway is the single execution point. See execution contract below.                           |

> **Execution contract (treatment move).** `move_authorized` decides _that_ the move is
> allowed; this defines _what_ is sent to the executor and _what makes a retry safe_. The
> outgoing request is
> `ActionRequest { request_id, case_id, action_type, action_hash, idempotency_key, issued_at, approval_id, expires_at }`.
> Three checks run **at execution time** (not just at build time), because the world moves
> between decision and execution: `now < expires_at` (the approval is still valid),
> `action_hash` matches (the approved action was not altered), and the approval is bound to
> _this_ case and came from a charge-role actor. The `idempotency_key` lets the Gateway
> recognize a repeated request and refuse to start treatment twice. Evidence retained for
> after-the-fact proof: `request_id` + `idempotency_key` + `issued_at`, the `ToolReceipt`
> **or** a `timeout@T` marker, a `reconcile_record { queried_at, source, result }`, and
> `approval_id` + `actor_is_charge` + `expires_at`. Full model: see the "Interface Contract & Failure Handling" section of [`SYSTEM_MODELING.md`](./SYSTEM_MODELING.md).

### Per-agent failure model (`AGENT_FAILED`)

This section explains how each agent behaves when it errors or times out, making the fail-operational approach concrete. The two key columns are "Critical?" (halt or keep working) and "Fail direction" (open = degrade and continue; closed = stop the line). Retries are attempted first, using the retry-budget guard, and the exhaustion action happens only after retries are used up. This is the agent **crashing**, which is different from demo case 3 where the submission had nothing to parse.

When an error or timeout occurs, the Orchestrator retries up to the agent's own retry budget `N` (`retry_budget_left`). After that, it follows the agent's declared direction. Fallback and technician alert run in parallel and do not block each other. A halted case leaves `agent_failed` only when the technician resolves the outage (`AGENT_RECOVERED`, then `resume_at_failed_stage`).

> The **Intake Parser** is a deterministic validator, so a "crash" is rare; a validation problem is already covered by demo cases 2/3, and a genuine software fault degrades to the same manual-entry path. It is therefore not listed as a separate failure row.

| Agent                   | Critical? | Fail direction                | Retry `N`      | On exhaustion                                                                                                                                                                                                                                        |
| ----------------------- | --------- | ----------------------------- | -------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| CRM / Patient DB        | No        | **open (degrade)**            | _(to confirm)_ | Continue on **intake-only data** (skip history); **flag the case +** `alert_technician`; defer `patch_patient_data` until the DB returns. New-patient empty result is _not_ a failure.                                                               |
| Acuity Classifier       | No        | **open (degrade)**            | _(to confirm)_ | Drop the system acuity; **fall back to** `nurse_proposed_acuity`; **discrepancy gate is disabled for the outage, flag these cases "cross-check off, review later"**; `alert_technician`.                                                             |
| PII filter, schema-drop | **Yes**   | **closed (halt)**             | _(to confirm)_ | Deterministic identifier drop. A hard code fault here **stops the line** (`agent_failed`): continuing could leak identifiers. `alert_technician`; resumes only on `AGENT_RECOVERED`.                                                                 |
| PII filter, BERT/NER    | No        | **open (degrade, safe-drop)** | _(to confirm)_ | The free-text scorer/redactor. If it is down, **drop the free-text fields from the model payload** and drop urgency scores, then continue and flag. No unredacted prose reaches the model, so the privacy invariant still holds. `alert_technician`. |
| Safety Validation       | Yes       | degrade-to-human              | _(to confirm)_ | Do **not** hard-halt: route **every** case to charge nurse (`awaiting_human_approval`) so no-approval-bypass still holds; `alert_technician`.                                                                                                        |
| Human Escalation bridge | Yes       | degrade-to-human              | _(to confirm)_ | `alert_technician`; fall back to a manual notification channel for the gate.                                                                                                                                                                         |

> **Principle:** non-critical, continue via fallback and notify; critical-open, degrade to human and notify; critical-closed, halt and notify, then resume on recovery.
>
> **On the PII filter split:** the free-text fields are the "patient's own words" box and any "Other" free-text entries. Structured identifiers are handled by deterministic schema-drop (the critical-closed row); the probabilistic BERT/NER only guards free text, so it can fail safe by dropping those fields rather than halting the whole line.

---

## Transitions (complete table)

This table is the core of the state machine. Each edge is shown as **current state +** `EVENT` **[guard] -> next state / actions**. The **Validation Layer** column names which layer authorizes or denies the transition; the **Explanation** column is the human-readable reason (also written to the audit log). The **Arrow** column matches the diagram. After the table are the named scenario paths (regression cases) and the forbidden sequences the safety layer must block.

| Current (control)             | Event                                             | Guard                                                 | Validation Layer                       | Actions                                                                                  | Next (control)               | Explanation                                                           | World effect                               | Arrow         |
| ----------------------------- | ------------------------------------------------- | ----------------------------------------------------- | -------------------------------------- | ---------------------------------------------------------------------------------------- | ---------------------------- | --------------------------------------------------------------------- | ------------------------------------------ | ------------- |
|                               | `CASE_SUBMITTED`                                  |                                                       | Schema                                 | route channel; **assign** `order_key`                                                    | `intake_received`            | new case entered                                                      |                                            | 1a            |
| `intake_received`             | `MESSAGE_NORMALIZED`                              |                                                       |                                        | `emit_event_log`                                                                         | `parsing`                    | input normalized                                                      |                                            | 2             |
| `parsing`                     | _(on entry)_                                      |                                                       |                                        | `invoke_intake_parser`                                                                   | `parsing`                    | run validator                                                         |                                            | 3             |
| `parsing`                     | `DATA_PARSED`                                     | `required_fields_complete` ∧ `input_is_valid`         | Schema                                 | `emit_event_log`                                                                         | `data_parsed`                | submission valid                                                      |                                            | 4             |
| `parsing`                     | `MISSING_FIELDS_DETECTED`                         | ¬`required_fields_complete`                           | Schema                                 | `notify_user("request fields")`                                                          | `missing_fields_requested`   | required fields missing                                               |                                            | 16            |
| `parsing`                     | `SUBMISSION_FAILED`                               | (demo case 3)                                         | Schema                                 | `notify_user("resubmit or manual")`                                                      | `submission_failed`          | submission unusable                                                   |                                            | 17            |
| `parsing`                     | `INVALID_INPUT_DETECTED`                          | ¬`input_is_valid`                                     | Schema / PII                           | `notify_user("invalid input")`                                                           | `input_rejected`             | invalid schema or injection                                           |                                            | 18            |
| `missing_fields_requested`    | `FIELDS_SUBMITTED`                                |                                                       |                                        |                                                                                          | `parsing`                    | nurse supplied fields                                                 |                                            | 1b.x          |
| `submission_failed`           | _(auto / manual)_                                 |                                                       |                                        | `notify_user`                                                                            | `intake_received`            | resubmit                                                              |                                            | 1a·resubmit   |
| `input_rejected`              | _(auto)_                                          |                                                       |                                        | `notify_user`                                                                            | `intake_received`            | rejected, await new input                                             |                                            | 1a·rejected   |
| `data_parsed`                 | _(on entry)_                                      |                                                       |                                        | `fetch_patient_data` (stable-ID lookup)                                                  | `resolving_identity`         | look up record                                                        |                                            | 4b            |
| `resolving_identity`          | `PATIENT_RESOLVED` (found)                        | `patient_found`                                       | Datalog (provenance)                   | merge history + new data                                                                 | `redacting_routing`          | record found                                                          |                                            | 4b·found      |
| `resolving_identity`          | `PATIENT_RESOLVED` (not-found)                    | `db_reachable` ∧ ¬`patient_found`                     | Datalog (provenance)                   | continue on intake-only (silent, normal)                                                 | `redacting_routing`          | new patient, no history                                               |                                            | 4b·new        |
| `resolving_identity`          | `PATIENT_RESOLVED` (db-error)                     | ¬`db_reachable`                                       |                                        | continue on intake-only; flag; `alert_technician`; defer `patch_patient_data`            | `redacting_routing`          | DB unreachable, degraded                                              |                                            | AF·db         |
| `redacting_routing`           | _(on entry)_                                      |                                                       | OPA (no-identifiers)                   | `build_model_payload` (drop identifiers; key by `case_id`)                               | `redacting_routing`          | build model payload                                                   |                                            | 5             |
| `redacting_routing`           | `AGENT_FAILED` (PII schema-drop)                  | ¬`retry_budget_left`                                  | OPA (no-identifiers)                   | `alert_technician`                                                                       | `agent_failed`               | redaction faulted, halt                                               |                                            | AF·PII        |
| `redacting_routing`           | `REDACT_ROUTE_DONE`                               |                                                       | OPA (no-identifiers)                   |                                                                                          | `classifying`                | payload clean                                                         |                                            | 6             |
| `classifying`                 | _(on entry)_                                      |                                                       |                                        | `invoke_acuity_classifier` (internal red-flag pre-check, then LLM)                       | `classifying`                | run classifier                                                        |                                            | 7             |
| `classifying`                 | `AGENT_FAILED` (Acuity Classifier)                | ¬`retry_budget_left`                                  |                                        | `fallback_manual` (use `nurse_proposed_acuity`; disable gate; flag) + `alert_technician` | `safety_validating`          | classifier down, use nurse acuity                                     |                                            | AF·classifier |
| `agent_failed`                | `AGENT_RECOVERED`                                 |                                                       |                                        | `resume_at_failed_stage`                                                                 | (the failed stage)           | outage resolved                                                       |                                            | AF·recover    |
| `classifying`                 | `ACUITY_PROPOSED`                                 |                                                       |                                        | `emit_event_log`                                                                         | `acuity_proposed`            | acuity proposed                                                       |                                            | 8             |
| `acuity_proposed`             | _(on entry)_                                      | `acuity_agree`                                        | Z3 (band totality)                     | set `acuity_source = human_confirmed`                                                    | `safety_validating`          | nurse and system agree                                                |                                            | 9a            |
| `acuity_proposed`             | _(on entry)_                                      | `acuity_gap_minor`                                    | Z3 (band totality)                     | `auto_resolve_acuity_upward`                                                             | `safety_validating`          | gap of 1, take more acute                                             |                                            | 9b            |
| `acuity_proposed`             | _(on entry)_                                      | `acuity_gap_major`                                    | Z3 (band totality)                     | `invoke_human_escalation` ("show discrepancy + rationale; request resolution")           | `awaiting_human_approval`    | gap ≥2, charge nurse decides                                          | -> `human_review`                          | 9c            |
| `safety_validating`           | `VERDICT_PROPOSED`                                | `safety_pass`                                         | Prolog / Datalog / OPA                 | `emit_event_log`; set `safety_passed`                                                    | `verdict_proposed`           | safety passed                                                         |                                            | 10            |
| `safety_validating`           | `VERDICT_PROPOSED`                                | ¬`safety_pass`                                        | Prolog / Datalog / OPA                 | `invoke_human_escalation`                                                                | `awaiting_human_approval`    | safety failed, human decides                                          | -> `human_review`                          | 10·fail       |
| `safety_validating`           | `AGENT_FAILED` (Safety Validation)                | ¬`retry_budget_left`                                  |                                        | `invoke_human_escalation` (route all to charge) + `alert_technician`                     | `awaiting_human_approval`    | validator down, route to charge                                       | -> `human_review`                          | AF·safety     |
| `verdict_proposed`            | _(on entry)_                                      | `escalation_needed`                                   | Temporal                               | `invoke_human_escalation`                                                                | `awaiting_human_approval`    | escalation needed                                                     | -> `human_review`                          | 11            |
| `verdict_proposed`            | _(on entry)_                                      | ¬`escalation_needed`                                  | Temporal                               | `start_reassessment_timer`; set `approved`                                               | `monitoring`                 | cleared to queue                                                      | -> `waiting`                               | 11·pass       |
| `awaiting_human_approval`     | `ESCALATION_PROPOSED`                             |                                                       |                                        | `emit_event_log`                                                                         | `awaiting_human_approval`    | escalation recorded                                                   |                                            | 12            |
| `awaiting_human_approval`     | _(auto)_                                          | escalation = true                                     |                                        | `notify_user("request approval")`                                                        | `awaiting_human_approval`    | approval requested                                                    | -> `human_review`                          | 20            |
| `awaiting_human_approval`     | `GATE_TIMER_ASSIGNED_NURSE`                       |                                                       | Temporal                               | notify assigned charge nurse (UI)                                                        | `awaiting_human_approval`    | reminder 1                                                            |                                            | 20a           |
| `awaiting_human_approval`     | `GATE_TIMER_ESCALATE_ANY_CHARGE`                  |                                                       | Temporal                               | re-alert / widen to any charge-role nurse                                                | `awaiting_human_approval`    | reminder 2, widen                                                     |                                            | 20b           |
| `awaiting_human_approval`     | `APPROVAL_RESPONSE_RECEIVED`                      | `actor_is_charge` (acuity branch)                     | Prolog (authorization)                 | `apply_human_acuity` -> `emit_event_log`; set `approved`                                 | `safety_validating`          | charge nurse resolved acuity                                          |                                            | 1b.z·acuity   |
| `awaiting_human_approval`     | `APPROVAL_RESPONSE_RECEIVED`                      | safety-fail: `actor_is_charge` ∧ `correction_changed` | (charge-role correction)               | `apply_correction`; require change to `acuity`/`clinical_status`/`safety_verdict`        | `safety_validating`          | correct-and-revalidate (no override); re-run safety on corrected case | -> `safety_validating`                     | 1b.z·safety   |
| `monitoring`                  | _(on entry)_                                      |                                                       |                                        | `start_reassessment_timer`                                                               | `monitoring`                 | queued, timer running                                                 | -> `waiting`                               | 13            |
| `monitoring`                  | `REASSESSMENT_TIMEOUT` ∨ `DETERIORATION_DETECTED` |                                                       | Temporal                               | `notify_user("reassessment")`                                                            | `reassessment_required`      | re-triage due                                                         | -> `reassessment_required`                 | 14            |
| `reassessment_required`       | _(nurse re-files)_                                |                                                       |                                        |                                                                                          | `parsing`                    | full front-door rerun                                                 |                                            | 15            |
| `monitoring`                  | `MOVE_REQUESTED`                                  | `move_authorized` (target = treatment_started)        | OPA (authorization)                    | `emit_event_log`                                                                         | `monitoring`                 | move authorized                                                       | -> `treatment_started`                     | 1b.y          |
| `monitoring`                  | `TRANSITION_ACCEPTED`                             | `move_authorized`                                     | OPA (authorization)                    | `notify_user("accepted")`                                                                | `monitoring`                 | move confirmed                                                        | -> `treatment_started`                     | 19            |
| `monitoring`                  | `TREATMENT_COMPLETE`                              |                                                       |                                        | `emit_event_log`                                                                         | `monitoring`                 | treatment done, sign off                                              | `treatment_started` -> `formal_validation` | FV            |
| _(any active state)_          | `RELEASE_REQUESTED`                               | `release_authorized` (reason ∧ `actor_authorized`)    | OPA (authorization)                    | `sign_release`                                                                           | `case_closed`                | release signed                                                        | -> `patient_released`                      | REL           |
| _(any state, guarded action)_ | `ACTION_DENIED`                                   | a required guard fails                                | OPA / Prolog / Z3 / Datalog / Temporal | `emit_event_log`; `explain_denial`                                                       | **(stays in current state)** | formal layer denied the attempted action                              |                                            | BLK           |

> **On the** `BLK` **row.** When any guarded action is attempted and its guard fails (unauthorized treatment move, unauthorized release, non-charge nurse resolving a gate, or any symbolic-layer denial), the **attempted action is blocked**, the denying layer and reason are logged, and a Prolog explanation is produced. The **case does not move**; only the attempt is refused. This is Triage Guard's realization of the rubric's "Violation detected -> Blocked" outcome.

> **About the arrow labels:** numbered arrows (`1a`, `1b.x`, `1b.y`, `1b.z`, `2` to `20`) appear on the diagram. Lettered/suffixed rows (`4b`, `9a` to `9c`, `10·fail`, `11·pass`, the `AF·…` agent-failure edges, `20a`/`20b`, `FV`, `REL`, `BLK`) are internal details of those same edges, listed for completeness.

> **Documented scenario paths (regression cases):**
>
> - **Normal entry (demo case 1):** `1a, 2, 3, 4, 4b, 4b·found, 5, 6, 7, 8, 9a, 10, 11·pass, 13`
> - **New patient (no record):** `… 4b, 4b·new, 5 …`
> - **DB down (degrade):** `… 4b, AF·db, 5 …` (intake-only, flagged)
> - **Red-flag forced emergent:** acuity agent returns emergent with `acuity_source=rule_forced` on `ACUITY_PROPOSED`, then the normal gate path by gap vs nurse (`9a/9b/9c`).
> - **Acuity gap ≥2, charge nurse:** `… 8, 9c, 1b.z·acuity, 10, 11·pass, 13`
> - **Acuity gap 1 auto-resolve:** `… 8, 9b, 10, 11·pass, 13`
> - **Safety fail, gate:** `… 10·fail` -> correct-and-revalidate (see resolved safety-fail branch)
> - **Reassessment:** `13, 14, 15, 3 …` (full front-door rerun)
> - **Move to treatment, close:** `1b.y, 19` (`treatment_started`), `FV` (on `TREATMENT_COMPLETE`, `formal_validation`), `REL`
> - **AMA / release from waiting:** `… 13, REL` (with `reason = ama`, nurse sign-off)
> - **Missing fields (demo case 2):** `1a … 16, 1b.x`
> - **Failed submission (demo case 3):** `1a … 17, 1a·resubmit` (or full manual entry)
> - **Invalid / injection (demo case 4):** `1a … 18, 1a·rejected`
> - **Blocked action (denied):** any guarded action with a failing guard, `BLK` (logged + explained, case stays put)
> - **Classifier down (degrade):** `AF·classifier`, nurse acuity, gate off, continue
> - **PII schema-drop down (halt, recover):** `AF·PII`, `agent_failed`, `AF·recover` on `AGENT_RECOVERED`
> - **Safety validator down (degrade-to-human):** `AF·safety`, every case to charge nurse

> **Forbidden sequences (must be provably blocked):**
>
> - **Served out of order:** a queued patient pulled ahead of an emergent one. Blocked by the **bucket-ordering** rule.
> - **Clock rewrites acuity:** `REASSESSMENT_TIMEOUT` mutates acuity directly instead of forcing a re-look. Blocked by the **acuity-write-authority** rule (the timer only triggers 14, 15; the nurse's re-filed form changes acuity).
> - **Approval bypass:** reaching `treatment_started` via the system without safety/approval. Blocked by the **no-approval-bypass** rule (`move_authorized` requires `safety_passed ∧ approved`).
> - **Unauthorized action:** a treatment move or release with a failing guard. Caught by the `BLK` outcome (logged + explained; case does not advance).
> - **Unauthorized release:** a release with no reason or no authorized actor. Blocked by the **release-authorization** rule.
> - **Identifiers reach the model:** name/ID present in the classifier payload. Blocked by the **no-identifiers-in-model-input** rule.
> - **Injection reaches the model:** an injection submission (demo case 4) proceeds past intake to the classifier. Blocked by the **injection-rejected** rule.
> - **System fails to escalate a waiter:** a waiting patient passes the ceiling T without the system raising an escalation. Blocked by the **wait-liveness** rule (the guarantee is that the system escalates, not that a human acts, see below).
> - **Stuck failure:** a case sits in `agent_failed` forever. Resolved by `AF·recover` on `AGENT_RECOVERED`.

---

## Context / State variables (Data plane)

This section lists the data the machine keeps for each case: the acuity model (`nurse_proposed_acuity`, `system_proposed_acuity`, final `acuity`, `acuity_gap`, the `acuity_source` lock), the queue key, and per-agent retry counters.

| Variable                              | Plane   | Type                                                      | Set by                             | Notes                                                                                                                                                  |
| ------------------------------------- | ------- | --------------------------------------------------------- | ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `case_id`                             | Control | id                                                        | intake                             | downstream reference key; the model sees the case by this, never by name/ID                                                                            |
| `channel`                             | Control | enum(website)                                             | Channel Router                     | website intake form                                                                                                                                    |
| `parsed_fields`                       | Data    | struct                                                    | Intake Parser                      | container for the webform fields, including `stable_patient_id`, `nurse_proposed_acuity`, symptoms, vitals, and any free-text; schema _(to define)_    |
| `parsed_fields.stable_patient_id`     | Data    | id                                                        | nurse (via webform)                | **lookup key** for the CRM; held on the case, excluded from the model payload                                                                          |
| `parsed_fields.nurse_proposed_acuity` | Data    | enum/level                                                | nurse (via webform)                | **mandatory**; absent, then demo case 2 / arrow 16; always nurse-supplied, never inferred                                                              |
| `redacted_payload`                    | Data    | struct                                                    | PII filter                         | model-facing; clinical fields only, keyed by `case_id`; **no identifiers**                                                                             |
| `urgency_scores`                      | Data    | {sentiment, distress, pain}                               | Input Normalizer                   |                                                                                                                                                        |
| `system_proposed_acuity`              | Data    | enum/level                                                | Acuity Classifier (incl. red-flag) | the classifier's **proposal** (may be `rule_forced` emergent); input to the gap, not the final value                                                   |
| `acuity`                              | Data    | enum/level                                                | Orchestrator (gate resolution)     | the **final resolved** acuity, written only by 9a/9b/9c; scale _(to confirm, e.g. ESI 1–5)_                                                            |
| `acuity_gap`                          | Data    | int                                                       | derived                            | absolute difference between `nurse_proposed_acuity` and `system_proposed_acuity`                                                                       |
| `acuity_source`                       | Data    | enum(system, rule_forced, auto_resolved, human_confirmed) | Orchestrator                       | provenance of the final `acuity`. `rule_forced` = set by the acuity agent's internal red-flag check (logged for tuning; still overridable at the gate) |
| `confidence`                          | Data    | float                                                     | Acuity Classifier                  | threshold for `confidence_ok`                                                                                                                          |
| `safety_verdict`                      | Data    | {pass/fail, reasons}                                      | Safety Validation                  |                                                                                                                                                        |
| `safety_passed`                       | Data    | bool                                                      | Orchestrator                       | set on arrow 10; read by `move_authorized`                                                                                                             |
| `approved`                            | Data    | bool                                                      | Orchestrator                       | set on 11·pass / 1b.z·acuity; read by `move_authorized`                                                                                                |
| `clinical_status`                     | World   | enum (see World plane)                                    | Orchestrator                       | board column                                                                                                                                           |
| `acuity_bucket`                       | Data    | enum(emergent[1–2], queued[3–5])                          | derived from final `acuity`        | primary sort key                                                                                                                                       |
| `arrival_time`                        | Data    | timestamp                                                 | intake                             | tiebreaker within bucket                                                                                                                               |
| `order_key`                           | World   | (bucket, arrival_time)                                    | Orchestrator                       | **assigned at system entry**; persists across state changes                                                                                            |
| `release_reason`                      | Data    | enum(discharge, ama, transfer, admit)                     | nurse                              | recorded at close                                                                                                                                      |
| `reassessment_timer`                  | World   | timer                                                     | Orchestrator                       | the single per-patient timer; interval set by acuity band; started on entry to `monitoring`; drives `REASSESSMENT_TIMEOUT`                             |
| `gate_timer`                          | World   | timer                                                     | Orchestrator                       | separate per-gated-case timer; drives the `GATE_TIMER_*` approval-reminder ladder (not a reassessment timer)                                           |
| `retry_count[agent]`                  | Control | int per agent                                             | Orchestrator                       | per-agent budget for agent-failure handling                                                                                                            |

---

## Queue ordering rule

This section explains how the waiting queue is sorted, and what does **not** affect your place in line. Only a real acuity change reorders you; state labels and the clock do not. Fairness comes from the sort; liveness comes from the escalation path, not the sort.

`order_key` sorts the waiting queue by two keys, in order:

1. **Bucket:** `emergent` (acuity 1–2) always above `queued` (acuity 3–5).
2. **Arrival time:** within a bucket, earlier arrival first.

Consequences:

- A 1 and a 2 are peers (bucket sorts them, arrival breaks the tie); same for 3/4/5.
- `order_key` is assigned at system entry and **persists across state changes**: going into `reassessment_required` or `human_review` does not move you in line.
- Only a real **acuity** change (via reassessment/deterioration/override) moves you between buckets. State labels and the clock never move you.
- **Timer firing forces a reassessment (14, 15); it does not re-sort the queue.**
- Under overload, ordering guarantees **fairness**, not **service**. Liveness is delivered by the escalation path (the wait-liveness rule), not the sort.

> **Reversible vs. irreversible actions (capacity design).** Queue placement and re-ordering
> are **reversible** and run **automatically**, with no human gate - if a re-order turns out
> wrong, the case is simply moved back. This is the system's fast, automatic containment:
> the moment a patient is suspected high-acuity, they move up the queue without waiting for
> approval. **Irreversible** actions - the treatment move and release - always stay under
> safety validation and human approval, never automatic. Keeping reversible work off the
> human gate is what holds the approval queue stable under load; routing it _through_ the
> gate would overload the scarce human approvers. Capacity analysis: see the "Latency, Capacity & the Reversibility Split" section of [`SYSTEM_MODELING.md`](./SYSTEM_MODELING.md).

---

## Neuro-Symbolic Architecture

_This section states the neurosymbolic split in one place: what the neural component does, what the symbolic layer does, what is never left to the model, and what happens when the two conflict._

**Neural component.** A single LLM, the **Acuity Classifier**, reads the redacted, `case_id`-keyed clinical payload and proposes `system_proposed_acuity` with a confidence. That is the only generative model in the decision path. It runs an internal deterministic **red-flag pre-check** first: if a hard clinical trigger matches, it proposes emergent with `acuity_source = rule_forced` (advisory, overridable at the gate). The Intake Parser (validator), the PII schema-drop, and the Safety Validation layer are deterministic, not neural. BERT/NER is a non-generative classifier used only for free-text redaction and urgency scoring.

**Symbolic layer.** Everything that must not be left to a stochastic model: **OPA** (authorization, no-identifiers, release), **Z3** (constraint consistency and band totality), **Prolog** (authorization inference and explanation), **Datalog** (provenance and information-flow), and **temporal logic** (sequence rules). The state machine is the spine that sequences them.

**What is never left to the model.** Final acuity (resolved at the gate, not the classifier's proposal), authorization, queue ordering, identifier handling, the move into treatment, and release. The model proposes; the symbolic layer disposes.

**On conflict.** When the model proposes something the symbolic layer forbids, the action is **not** taken: the attempt is denied (the `BLK` outcome), logged with the denying layer, and explained via Prolog. When the model and the nurse disagree on acuity by ≥2, the case goes to the charge nurse at the gate; the human decides, and that decision is `human_confirmed` and final.

---

## Safety invariants

These are the properties enforced by the symbolic layer (Prolog, Datalog, Z3, OPA) and the runtime monitor. This is the governance half of the neurosymbolic split. Each property is a checkable proposition with a target formalism.

| Property                         | Family           | Statement                                                                                                                                                                                                                            | Formalism     |
| -------------------------------- | ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------- |
| No bypass of approval            | Safety           | never reach `treatment_started` via the system without passing safety/approval (enforced by `move_authorized` requiring `safety_passed ∧ approved`)                                                                                  | _(to define)_ |
| Sole writer                      | Safety           | no state write originates outside the Orchestrator                                                                                                                                                                                   | _(to define)_ |
| No identifiers in model input    | Safety           | the classifier/safety-validator payload contains no name/ID/DOB/phone; the patient is referenced downstream only by `case_id`. Real identifiers are held only by the Orchestrator/CRM layer under `actor_authorized`.                | OPA/Rego      |
| Reassessment bound               | Bounded          | after `waiting`, a reassessment is scheduled within ≤ T                                                                                                                                                                              | LTL `F≤t`     |
| Escalation liveness              | Liveness         | every `human_review` is eventually resolved by a human                                                                                                                                                                               | LTL `F`       |
| Wait liveness (system escalates) | Liveness/Bounded | within T of entering `waiting`, the system **raises an escalation** (reminder ladder + widen). The system guarantees the alert, not that a human acts. `G(waiting -> F≤T escalation_raised)`                                         | LTL `F≤t`     |
| Injection rejected               | Safety           | an input with `reason = injection` never proceeds to classify. `G(injection_detected -> X ¬classifying)`                                                                                                                             | _(to define)_ |
| Bucket ordering                  | Safety           | `G(¬(queued.order_key < emergent.order_key))`                                                                                                                                                                                        | Z3            |
| Acuity write-authority           | Safety           | `acuity` is written only by (a) initial-classification resolution at the gate, (b) a red-flag rule forcing emergent, (c) reassessment carrying new clinical evidence, or (d) human override. The timer alone never changes `acuity`. | Datalog       |
| Release authorization            | Safety           | every `case_closed` via release carries a valid `release_reason` and an authorized nurse actor                                                                                                                                       | OPA/Rego      |
| Failure recoverability           | Liveness         | a case in `agent_failed` is eventually resumed or manually handled (never stuck): `G(agent_failed -> F resolved)`                                                                                                                    | LTL `F`       |

> **Wait-liveness is honest about what the system controls.** The system cannot force a swamped nurse to act, so it does not promise the patient is reassessed within T; it promises it **raises an escalation** within T. Under overload the guarantee is escalation, not service.
>
> **Red-flag rules are advisory, not a hard floor.** A `rule_forced` emergent acuity is a strong suggestion the charge nurse can override at the gate; the system therefore does **not** guarantee `G(red_flag -> acuity == emergent)`. This is a deliberate design stance: the clinician is the final authority; rules advise, humans decide, and every override is logged (`acuity_source = rule_forced` + resolution) so red-flag firing rates can be tuned. (Stated here as a design note, not an enforced invariant, because it is the deliberate absence of a guarantee.)
>
> **Acuity override vs. safety override.** "Override" in this document always means an
> **acuity** decision - a human choosing a triage level, including overriding an advisory
> red-flag. It never means overriding a **safety-validation** verdict: there is no
> safety-override path in the system. A safety-fail is resolved only by correct-and-revalidate
> (see the resolved safety-fail branch), never by proceeding past a failed check.

---

## Temporal logic rules

The rubric requires temporal rules listed separately, so they are collected here. Most restate a Safety-invariant property as a temporal-logic rule over the event trace; some are pure liveness or until rules. Each describes what must always hold, what must eventually happen, or what must hold until something else occurs.

| Kind          | Rule (LTL)                                              | In words                                                                                | Where it bites                             |
| ------------- | ------------------------------------------------------- | --------------------------------------------------------------------------------------- | ------------------------------------------ |
| Always        | `G(reach_treatment -> safety_passed ∧ approved)`        | never start treatment before safety validation **and** approval                         | `move_authorized` on 1b.y / 19, no-bypass  |
| Always (next) | `G(¬safety_pass -> X awaiting_human_approval)`          | a safety failure moves straight to the human gate on the next step                      | arrow 10·fail                              |
| Always (next) | `G(guard_failed -> X blocked_action)`                   | any guarded action whose guard fails is blocked, logged, and explained                  | arrow BLK                                  |
| Eventually    | `G(awaiting_human_approval -> F resolved)`              | every gated case is eventually resolved by a human                                      | the gate, escalation liveness              |
| Eventually    | `G(agent_failed -> F resolved)`                         | a halted case is eventually resumed or manually handled, never stuck                    | arrow AF·recover, failure recoverability   |
| Bounded       | `G(waiting -> F≤T escalation_raised)`                   | within T the system raises an escalation for every waiter (not: a human acts)           | arrows 13 / 14 / 20a / 20b, wait liveness  |
| Until         | `G(identifiers_present -> ¬model_call U payload_built)` | identifiers must not reach the model until the payload is built without them            | arrows 5, 6, no identifiers in model input |
| Eventually    | `G(action_executed -> F audit_log_created)`             | every state-changing action is eventually written to the audit log                      | `emit_event_log` on every transition       |
| Always (next) | `G(injection_detected -> X ¬classifying)`               | an injection input never reaches the classifier                                         | arrow 18, injection rejected               |
| Always        | `G(¬(queued.order_key < emergent.order_key))`           | an emergent patient is never ordered behind a queued one                                | queue sort, bucket ordering                |
| Always (next) | `G(agent_failed -> X(degrade ∨ halt))`                  | an agent failure moves to a degraded/manual path or a controlled halt, never to nowhere | the `AF·…` edges, per-agent failure model  |

> `resolved` = a human records a decision at the gate (`apply_human_acuity` on the acuity branch, or the safety-branch outcome, see **Safety-fail branch of the human gate (resolved)**); for `agent_failed`, `resolved` = `AGENT_RECOVERED` (resume) or a manual-handling exit.
>
> **How they are checked.** Single-step rules (no-bypass, injection-rejected, safety-fail routing, agent-failure routing, blocked-action) are enforced as transition guards. Liveness and bounded rules (eventually / within T) are checked by the runtime governance monitor against the running event trace (the `emit_event_log` stream), which raises a violation if a deadline passes.

---

## Symbolic governance layer: OPA, Z3, Prolog, Datalog

This section puts the neurosymbolic split into practice. The neural component (the Acuity Classifier LLM) only proposes. Before the Orchestrator writes State, proposals and attempted actions pass through the symbolic layers; if any denies, the action is blocked (`BLK`), never silently taken.

**OPA, policy and authorization (runtime).** A code-as-policy layer checked at the human-approval gate (arrows 11/20), treatment moves (`move_authorized` on 1b.y/19), redaction-to-classification (arrow 6, blocked if identifiers remain), and release (REL). It decides whether the actor holds an authorized role, whether a `sign_release` carries a valid `release_reason`, and whether a treatment move is backed by `safety_passed ∧ approved`. Backs **No identifiers in model input**, **No bypass of approval**, and **Release authorization**. On "not allowed," the action is denied via `BLK`.

**Z3, constraint consistency (mainly design-time, plus a runtime guard).** Z3 does exhaustive proof: it shows a property holds for **every** reachable state, which example tests cannot. Design-time, it proves the acuity bands are **total and exclusive** (for every `acuity_gap ≥ 0` exactly one of 9a/9b/9c fires, so no case falls through the gate) and that the transition guards are satisfiable and mutually consistent. It also proves the queue-ordering constraint (no `queued.order_key < emergent.order_key`) is unsatisfiable, i.e. cannot occur. Backs **Bucket ordering** and the integrity of the transition table. Z3 proves the spec sound before deployment; it does not route cases at runtime (that is OPA/Prolog/temporal).

**Prolog, inference and explanation (runtime).** Backs `actor_is_charge` and the explainability requirement. Facts (who is on shift, in what role) and rules (which action requires which role, what is blocked outright) let it infer whether a user may act, and produce the **why**: approved because the role has authority, or denied because the role is insufficient. If inference denies at the gate, the case stays in `awaiting_human_approval`; the explanation is what the nurse sees and what is written to the audit log. Prolog also produces the `explain_denial` string on the `BLK` row.

**Datalog, relations, information flow, provenance (runtime).** Transitive-relation analysis, two uses. First, information flow: whether a sensitive field (name, ID) can reach an external tool through the node chain without passing through payload construction; a query returning such a path exposes a leak. Second, provenance of the acuity value: only gate resolution, a red-flag forcing emergent, reassessment with new evidence, or an authorized human override are permitted writers; a timer alone is not. Backs **No identifiers in model input** and **Acuity write-authority**.

**How the roles divide.** OPA governs permissions and boundaries at runtime; Z3 proves at design time that no reachable state violates the numeric/logical constraints; Prolog infers and explains individual decisions at runtime; Datalog traces relations and flows at runtime. Together they cover the four symbolic requirements and stop a neural proposal from ever becoming an unchecked state write.

---

## Open decisions

Items still needing a decision before the spec is final. The OCR question is settled (intake is a structured webform); the safety-fail branch is now resolved (below).

1. Acuity scale (ESI 1–5?) and the `confidence_ok` threshold.
2. `confidence_ok`: **wire in** (low confidence, then gate) **or delete** (see Guards).
3. Per-agent retry budgets `N` (see Per-agent failure model).
4. Formalisms for the **no-approval-bypass**, **sole-writer**, and **injection-rejected** rules.
5. `parsed_fields` schema (see Context / State variables).
6. ~~Safety-fail branch of the human gate~~ - **resolved** (see the resolved section below; modeled in the "Safety-Fail Branch" section of SYSTEM_MODELING.md).

---

## Safety-fail branch of the human gate (resolved)

`awaiting_human_approval` is entered for two different reasons, each asking the human a
different question. Both are now resolved. The safety-fail resolution is modeled in full in the "Safety-Fail Branch" section of
[`SYSTEM_MODELING.md`](./SYSTEM_MODELING.md); the summary is below.

`awaiting_human_approval` is entered by **two situations that ask the human different questions**:

- **Acuity discrepancy** (gap ≥2, arrow 9c, then 1b.z·acuity): **resolved.** Question: "which acuity is right?" Options: `use_system_acuity` / `use_nurse_acuity`.
- **Safety-validation failure** (arrows 10·fail and 11 via `escalation_needed`, plus `AF·safety` when the validator is down): **resolved** (arrow 1b.z·safety). There is no acuity dispute here.

**Resolution of the safety-fail branch:**

- **Nurse's options:** **correct-and-revalidate only.** There is **no override-and-proceed**
  and no safety-override path anywhere in the system - a safety-fail is a request to fix,
  never permission to skip the check. (A safety-fail because the validator is _down_ is a
  different case: the human _provides_ the safety judgment the dead validator could not; it
  is still not an override. See the "Safety-Fail Branch" section of SYSTEM_MODELING.md.)
- **Where it sends the case:** back to `safety_validating` (the reassessment path), after
  the system enforces that at least one of `acuity`, `clinical_status`, or `safety_verdict`
  **changed** - otherwise the correction is rejected ("correction required"), which also
  prevents an infinite fail -> gate -> same-input -> fail loop.
- **Role required:** **charge role** (`actor_is_charge`), consistent with the discrepancy branch.
- **Loop guard:** a finite number of correction rounds; on exhaustion the case escalates to
  a more senior clinician on shift rather than looping. The exact bound is fixed from data
  (like the retry budget `N`).
- **Queue position while waiting:** the case leaves normal queue order (it does not compete
  for a bed on a disputed acuity) but stays visible and monitored in the gate, with its own
  waiting service-level target.

**Constraints honored:**

- **No-approval-bypass:** correct-and-revalidate always re-runs safety on the corrected
  version, so there is no path to `treatment_started` that skips safety/approval.
- **No silent return:** a resolution must change `acuity`, `clinical_status`, or
  `safety_verdict`; an unchanged case cannot go back to `waiting`.

_Arrow 1b.z·safety remains a distinct guarded edge, separate from 1b.z·acuity, now with a
defined resolution._
