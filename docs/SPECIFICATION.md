# Triage Guard: Architecture and State-Machine Specification

**Project:** Triage Guard, an AI-Governed ER Triage System

**Authors:** Idan Beck, Barak Amir

**What this is.** This document is the complete specification for **Triage Guard**, an AI-governed emergency-room (ER) triage system. It is designed to be self-contained, with each section starting with a brief introduction explaining its content and purpose. Please read the **Conventions & scope** section first, since all later sections rely on its definitions.

**Companion diagram.** This document matches the Triage Guard architecture diagram at [`triage-guard-system.png`](../assets/triage-guard-system.png). The **arrow numbers** in the Transitions table and scenario paths are the same as those shown on the diagram, so you can easily find any row in both places. Agent names, world statuses, and the "notify user" list also correspond directly to the diagram. Mermaid versions of the architecture and the control-plane state machine are included in the companion file **[`diagrams.md`](diagrams.md)**.

**Open items.** The safety-fail branch of the human gate - previously the one unresolved core decision - is now **resolved**; the resolution is recorded in the resolved section below and modeled in detail in the "Safety-Fail Branch" section of [`SYSTEM_MODELING.md`](./SYSTEM_MODELING.md). Document intake uses **mock questionnaire data (no live OCR)**, and the method along with its demo cases is explained in **Document intake & demo data**. In other sections, items that still need to be finalized are marked as _(to confirm)_ or _(to define)_.

**Companion system model.** A focused risk-model of the highest-risk parts of the system - the treatment-move execution (including its `UNKNOWN` state and reconciliation), the interface contract, the safety-fail branch, and the capacity/reversibility design - is in [`SYSTEM_MODELING.md`](./SYSTEM_MODELING.md). Sections below reference it where relevant.

**Contents**

- [Problem definition & system value](#problem-definition--system-value)
- [How the pieces fit together](#how-the-pieces-fit-together)
- [Tech stack](#tech-stack)
- [Conventions & scope](#conventions--scope)
- [Document intake & demo data](#document-intake--demo-data)
- [Identity resolution & patient data](#identity-resolution--patient-data)
- [Local CRM stub (Postgres)](#local-crm-stub-postgres)
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

_This is a one-screen map of the system, giving the rest of the document a clear starting point. The system runs as a LangGraph state graph: a deterministic workflow whose allowed transitions are declared as a graph, supported by specialist agents and services._

- **Input Channel** is where a case starts: the **website** intake form (a structured webform).
- **Input processing** uses an **API Gateway** to pass along the raw input.
- **Intake Parser (ingestion)** reads the submitted form and turns it into a single, unified message. In this version, the form uses **mock / structured data** (no live OCR). A real deployment could add an OCR or extraction adapter behind the same interface.
- **Understanding, safety & routing** includes a **PII / sensitive-data filter** (keeps identifiers off the model payload), a **policy gate**, and a **sentiment/urgency** scorer (distress or pain).
- **Agent Core** is six specialist agents: Intake Parser, Acuity Classifier, Safety Validation, Human Escalation, Waiting Room Monitor, and Audit, sequenced by a LangGraph `StateGraph`. Each node reads the shared graph state and returns its own results to be merged into it; routing is decided by the guards, declared as conditional-edge maps so the set of legal moves is data the framework validates rather than a convention.
- **Knowledge & Memory** covers the Knowledge Base, policy vector store, CRM (patient profile and history), and session memory.
- **Monitoring & Evaluation** includes user and agent feedback, an evaluation pipeline (test cases and regression checks), logs and traces, and a metrics dashboard.

---

## Tech stack

_This section shows the tools and technologies we are using in the project._

| Component              | Tech                                                      | Role                                                                                                                                                 |
| ---------------------- | --------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| Workflow orchestration | **LangGraph**                                             | A deterministic `StateGraph` over the specialist agents (Intake Parser, Acuity Classifier, Safety Validation, Human Escalation, Waiting Room Monitor, Audit). The Transitions table below is transcribed into its conditional-edge maps; per-node `RetryPolicy` carries the retry budgets and `interrupt()` carries the human gate. |
| Triage standard        | **ESI (Emergency Severity Index) 1-5, v5 handbook**       | the acuity scale. Decision point D (danger-zone vitals) is computed deterministically and **annotates** the case; A, B and C are the classifier's judgment |
| Observability          | **Langfuse**                                              | traces, logs, and eval pipeline feeding **Monitoring & Evaluation**                                                                                  |
| Package management     | **uv**                                                    | Python dependency management                                                                                                                         |

---

## Conventions & scope

_This section introduces the key terms and concepts used throughout the document. It covers naming conventions, the three planes where a case can exist, who is allowed to write State, and how failures are handled. Please start here, since later sections depend on these definitions._

- **Events** are `UPPERCASE_SNAKE`; **states** are `lowercase_snake`; **guards** are boolean predicates; **actions** are verbs.
- The machine has **three state planes**:
  - **Control**: where the Flow is in processing a case (its position in the pipeline).
  - **Data**: the parsed or derived payload, including fields, acuity, confidence, and verdict.
  - **World**: the patient's clinical status on the board (the kanban column).
  - _Control and World evolve semi-independently: a case can hold control-state_ `monitoring` _while its World status cycles_ `waiting -> treatment_started -> released`_. They are kept as separate planes so the machine is not the cross-product of the two._
- **State rule:** the agents propose; they do not write. Each Flow step calls its agent, reads the result, and writes it into the shared Flow state (`self.state`), which passes automatically from one step to the next. The exception is `execution_state` for the treatment move, written by a single step only, so a lost receipt cannot start treatment twice (see the Execution section).
- **Design stance: fail-operational.** Unless a critical component fails, the system keeps working. Non-critical agents switch to a human or manual fallback. Critical agents either switch to human fallback or stop completely (see the **Per-agent failure model**).
- **Ingestion stance: no live OCR.** The triage intake is a **structured webform** (mock data in this build); there is no OCR pipeline, but a real deployment could add one behind the same interface. Acuity is always supplied by the nurse and never inferred from the raw input. See **Document intake & demo data**.
- **Identity vs. the case.** Patient identifiers live only in the CRM. Identity is resolved once, at intake: the nurse enters the patient's national ID, and the CRM returns its own record number, `stable_patient_id`. The case then carries only `stable_patient_id`, never a name or national ID, and the model reasons over a `case_id`-keyed clinical payload. See **Identity resolution & patient data** and invariants **I11** and **I12**.
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

1. **Identity resolution (needs the real ID), at intake.** The nurse enters the patient's **national ID**. Intake looks the patient up in the CRM once by that ID and receives the CRM's **`stable_patient_id`** (its internal record number), the patient's history (no name or date of birth) and the age band. Only these enter the case; the national ID does not (I11).
2. **Payload construction.** The system merges that history with this visit's new data and builds the **model-facing payload**: approved clinical fields only, each a fixed-choice value or a number, keyed by `case_id` (I12). Anything that needs the patient's name, such as the board, asks the CRM by `stable_patient_id` at display time and stores nothing.

> **Target design, 2026-09-19.** The code still treats `stable_patient_id` as the value the nurse types, passes it into the case, and looks it up again in a graph step (`resolving_identity`, arrow 4b). Adding `national_id` to the CRM and moving the lookup to intake is on the implementation to-do list.

**Why identifiers are kept out of the model input:**

- **The classifier doesn't need them.** Acuity is a function of symptoms and vitals; a name cannot make a patient more or less acute.
- **Bias.** A name can leak ethnicity/gender/age into the decision. Excluding identity is a fairness safeguard, not only a privacy one.
- **Leak surface / compliance.** The LLM is the component reading patient-supplied text and (if generative) the one that could be induced to echo its context. If it never received a name/ID, a compromised model has nothing to leak.

**New patient vs. DB down (both continue on intake-only data, different logging):**

- **No record found** (new patient, nothing stored): **normal**, silent, no flag. Triage proceeds on this visit's data.
- **DB unreachable:** **same continue-path** (triage on intake-only data) but **flag the case and alert the technician**, because that is a failure, not an expected empty. The CRM is therefore **non-critical / fail-open** (see the Per-agent failure model).

**Write-back.** This visit's new clinical data is persisted to the CRM (`patch_patient_data`, by `stable_patient_id`) so the record stays current; when the DB was unreachable, the write-back is deferred and reconciled once it returns (I17). Not yet implemented: `patch_patient` exists but is never called.

> **Implementation note.** There is no external CRM in this project. The CRM is a **local
> Postgres database** with mock patient records, behind the same contract the rest of the
> system uses (`fetch_patient_data`, `patch_patient_data`, with `found` / `not-found` /
> `db-error` outcomes). Because the contract is identical, the local stub can later be
> swapped for a real CRM without changing the architecture. Schema and interface are in
> **[Local CRM stub (Postgres)](#local-crm-stub-postgres)**.

---

## Local CRM stub (Postgres)

_The CRM is the one external dependency the system reads patient history from. In this
project it is not a real external system - it is a local Postgres database (`crm`, on the shared server in `db/docker-compose.yml`) with mock records,
implemented behind the exact contract described above so it can be replaced by a real CRM
without touching the rest of the architecture. This section defines that stub's schema,
interface, and failure behavior._

**Why a real database (not an in-memory mock).** It gives real persistence
(write-backs survive restarts), a real query surface, and a way to simulate a `db-error`
so the CRM's fail-open path can be exercised, not just described - none of which a
plain in-memory dict provides. It shares the Postgres server that also holds the
triage checkpoints and timers (moved from SQLite on 2026-09-18).

### Schema

One table is enough for the contract. History is stored as JSON so a record maps directly
to the `PatientRecord` the Flow expects.

| Column              | Type                 | Notes                                                  |
| ------------------- | -------------------- | ------------------------------------------------------ |
| `stable_patient_id` | TEXT, primary key    | the CRM's internal record number (e.g. `P-1001`); the only patient reference the case carries |
| `national_id`       | TEXT, unique         | national ID; typed by the nurse at intake; the lookup key; never leaves the CRM   |
| `name`              | TEXT                 | held by the CRM layer only, never in the model payload |
| `date_of_birth`     | TEXT (ISO date)      | identifier-class data                                  |
| `known_conditions`  | TEXT (JSON array)    | e.g. `["diabetes", "hypertension"]`                    |
| `prior_visits`      | TEXT (JSON array)    | prior visits: date, acuity, notes                      |
| `last_updated`      | TEXT (ISO timestamp) | set on every write-back                                |

_All identifier-class columns (`name`, `date_of_birth`) stay on the CRM side
under `actor_authorized` and are dropped when the model-facing payload is built - the same
identifier rule the rest of the spec enforces (I11)._

### Interface

The stub implements exactly the two actions the spec already names, plus a health check,
so the Flow code is identical whether the CRM is local or real.

| Operation                                           | Returns                                    | Maps to                            |
| --------------------------------------------------- | ------------------------------------------ | ---------------------------------- |
| `fetch_patient_data(national_id)`                   | `found(record)` / `not_found` / `db_error` | `fetch_patient_data`, arrow 4b     |
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

This section lists all participants in a case: each proposing agent, the humans, and the ops technician. It explains what each one can read and what it can propose. The steps run inside a LangGraph `StateGraph`, which calls each agent in the declared order, evaluates the guards on its conditional edges, and holds the shared, checkpointed state the nodes read and update.

| Actor                                             | Type                                              | Reads                                          | Proposes / Emits                                                                                          | Tech                                       |
| ------------------------------------------------- | ------------------------------------------------- | ---------------------------------------------- | --------------------------------------------------------------------------------------------------------- | ------------------------------------------ |
| Intake Parser Agent                               | Validator (deterministic)                         | webform / structured intake (mock)             | `Data_Parsed` proposal / field-presence check                                                             | Deterministic schema validation            |
| Acuity Classifier Agent                           | LLM + deterministic ESI decision point D          | model-facing payload (case_id-keyed) + history | `system_proposed_acuity` + confidence + any danger-zone vitals breaches (annotation only)                 | LLM + ESI v5 Figure 6-1                    |
| Safety Validation Agent                           | Deterministic                                     | proposed classification                        | verdict (pass/fail)                                                                                       | Prolog / Datalog / Z3 / OPA                |
| Human Escalation Agent                            | Bridge to human                                   | case + verdict                                 | escalation + human response                                                                               | UI/queue _(to confirm)_                    |
| Waiting Room Monitor Agent                        | Timer / watcher                                   | status + timers                                | timeout / deterioration triggers                                                                          | _(to confirm)_                             |
| Audit Agent                                       | Logger                                            | event log                                      | persists trace                                                                                            | append-only store _(to confirm)_           |
| Output Verification Agent                         | Validator (deterministic)                         | agent output + expected schema + prior state   | `VERIFICATION_PASSED` / `VERIFICATION_FAILED` + violation list                                            | Schema validation + Datalog + OPA          |
| Channel Router                                    | Ingress                                           | raw input                                      | route website submission                                                                                  |                                            |
| Input Normalizer + PII filter                     | Pre-processor                                     | routed input                                   | model-facing payload (identifiers excluded)                                                               | Schema-drop (identifiers)                  |
| CRM / Patient DB                                  | Data store (local Postgres stub)                  | stable patient ID                              | patient record (history)                                                                                  | CRM (non-critical, fail-open)              |
| Triage Nurse / Charge Nurse                       | Human                                             | board + detail panel                           | status changes, approvals, acuity, missing fields, release sign-off                                       |                                            |
| Technician                                        | Human (ops)                                       | agent-failure alerts                           | fixes / acknowledges                                                                                      |                                            |

> **Verification is post-agent; guards are pre-agent.** An agent proposal is not read into shared state until it passes output verification. This is the second half of a two-part contract. The **guards** in the Transitions table check the pre-conditions before an agent runs: is the state ready, is the actor authorized, is the input valid. The **Output Verification Agent** checks the post-conditions after it runs: does the output match its schema, are the values in range, did any invariant break. Guards ask whether the agent may run; verification asks whether what it returned is usable. Verification is deterministic, so it belongs on the symbolic side of the split, and it emits `VERIFICATION_PASSED` with the checked output or `VERIFICATION_FAILED` with the violations.

---

## States

This section lists all the possible states a case can have. The states are grouped into three planes and a separate recovery or error group. The control plane tracks the case's place in the workflow. The recovery or error group explains what happens if something goes wrong. The world plane shows what nurses see on the board.

### Control plane (workflow position)

_The happy-path pipeline: where the Flow is in processing a case, from the moment it arrives to the moment its card leaves the board._

| State                     | Type                  | Entry action                                                 | Exit action     | Invariant                            | Arrow      |
| ------------------------- | --------------------- | ------------------------------------------------------------ | --------------- | ------------------------------------ | ---------- |
| `intake_received`         | initial               | log intake; **assign** `order_key`                           |                 | channel + raw payload present        | 1a         |
| `parsing`                 | normal                | invoke Intake Parser                                         |                 | input routed                         | 3          |
| `data_parsed`             | normal                |                                                              |                 | fields extracted OR flagged missing  | 4          |
| `resolving_identity`      | normal                | `fetch_patient_data` (stable-ID lookup)                      |                 | stable patient ID present            | 4b         |
| `redacting_routing`       | normal                | build model-facing payload (drop identifiers), score urgency |                 | no identifiers in model input (I12)  | 5, 6       |
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

> **Release rule:** `patient_released` is reachable from **any** live state, not only the treatment path. Every release **requires a charge-role sign-off** (`actor_is_charge`) plus a valid `release_reason`. "Left the ward vs. left the hospital" is out of scope; both close the card.
>
> **AMA consequence:** the patient may physically leave before the card closes; the card stays open until a nurse signs. A monitor must **not** treat an unsigned-but-departed card as "still waiting."
>
> **Manual-edit rule:** nurses manually set status only for `treatment_started` (from `waiting`) and for release (any state, with reason). All other statuses are system-set.

> **Future design, not a current requirement.** This describes an external ward system that this project will never have (decided 2026-09-19; the single-execution-writer invariant was dropped for the same reason). Today a move into treatment is a board column change, guarded by I5 and I6.
>
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
| `PATIENT_RESOLVED`               | Flow, CRM                  | patient record found / not-found / db-error                              | internal                  |
| `DATA_PARSED`                    | Intake Parser              | parsed fields (incl. `nurse_proposed_acuity`, `stable_patient_id`)       | internal (proposal)       |
| `MISSING_FIELDS_DETECTED`        | Intake Parser              | list of gaps                                                             | internal                  |
| `SUBMISSION_FAILED`              | Intake Parser              | error reason                                                             | internal (error)          |
| `INVALID_INPUT_DETECTED`         | Intake Parser / PII filter | reason (one of: invalid_schema, injection)                               | internal (error/security) |
| `REDACT_ROUTE_DONE`              | Understanding/routing      | model-facing payload + urgency                                           | internal                  |
| `ACUITY_PROPOSED`                | Acuity Classifier          | `system_proposed_acuity` + confidence                                    | internal (proposal)       |
| `VERDICT_PROPOSED`               | Safety Validation          | pass/fail + reasons                                                      | internal (proposal)       |
| `ESCALATION_PROPOSED`            | Human Escalation           | needed? (bool)                                                           | internal (proposal)       |
| `VERIFICATION_PASSED`            | Output Verification        | verified output (the agent's checked proposal)                           | internal (proposal)       |
| `VERIFICATION_FAILED`            | Output Verification        | agent id + reason + violation list                                       | internal (error)          |
| `REASSESSMENT_TIMEOUT`           | Waiting Room Monitor       | patient id                                                               | timer                     |
| `DETERIORATION_DETECTED`         | Waiting Room Monitor       | patient id + signal                                                      | internal                  |
| `TRANSITION_ACCEPTED`            | Flow, user                 | new status                                                               | notification              |
| `RELEASE_REQUESTED`              | Nurse                      | reason (one of: discharge, ama, transfer, admit) + actor                 | external (human)          |
| `ACTION_DENIED`                  | Flow (governance)          | denying layer + reason                                                   | internal (governance)     |
| `AGENT_FAILED`                   | Flow                       | agent + error + attempts                                                 | internal (error)          |
| `AGENT_RECOVERED`                | Technician, Flow           | agent id                                                                 | external (ops)            |
| `GATE_TIMER_ASSIGNED_NURSE`      | Waiting Room Monitor       | case id                                                                  | timer                     |
| `GATE_TIMER_ESCALATE_ANY_CHARGE` | Waiting Room Monitor       | case id                                                                  | timer                     |
| `EVENT_LOGGED`                   | Flow                       | trace record                                                             | emitted                   |

---

## Guards / Conditions

These are the boolean checks that decide which transition happens. This is where policy is set: acuity-gap bands, authorization, and retry budget.

| Guard                      | Expression                                                                                                                                                                                                                                                         | Evaluated by        |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------- |
| `required_fields_complete` | all mandatory fields present (incl. `nurse_proposed_acuity`, `stable_patient_id`)                                                                                                                                                                                  | Flow guard          |
| `input_is_valid`           | submission matches expected schema ∧ ¬injection                                                                                                                                                                                                                    | Parser / PII filter |
| `patient_found`            | CRM returned a record for the stable ID                                                                                                                                                                                                                            | Flow guard          |
| `db_reachable`             | CRM responded (found or empty), i.e. not a DB error                                                                                                                                                                                                                | Flow guard          |
| `no_active_duplicate`      | no open case — any control state other than the two terminal ones, `case_closed` and `input_rejected` (both route straight to the graph's `END`, never re-enter the pipeline) — other than this case itself, already exists for this `stable_patient_id`                                                                                                                                  | Datalog (provenance) |
| `confidence_ok`            | `confidence >= threshold` _(threshold to confirm)_. Optional: wire in (low confidence, then gate) or drop. Not applicable during a classifier outage.                                                                                                              | Flow guard          |
| `safety_pass`              | verdict == pass                                                                                                                                                                                                                                                    | Safety Validation   |
| `escalation_needed`        | verdict fail ∨ low confidence ∨ policy hit                                                                                                                                                                                                                         | Human Escalation    |
| `move_authorized`          | target status change permitted for this actor (e.g. `waiting -> treatment_started`) ∧ `actor_authorized` ∧ `safety_passed` ∧ `approved`. Enforces I5 (no bypass): a treatment move is refused unless the case already passed safety and any required approval. | Flow guard          |
| `release_authorized`       | valid `release_reason` ∧ `actor_is_charge`. A _reason plus authorization_ check, **not** a source-state check.                                                                                                                                                    | Flow guard          |
| `actor_authorized`         | role ∧ jurisdiction ∧ data-class OK                                                                                                                                                                                                                                | Flow guard (policy) |
| `retry_budget_left(agent)` | `retry_count[agent] < N[agent]`                                                                                                                                                                                                                                    | Flow guard          |
| `acuity_agree`             | `acuity_gap == 0`                                                                                                                                                                                                                                                  | Flow guard          |
| `acuity_gap_minor`         | `acuity_gap == 1`                                                                                                                                                                                                                                                  | Flow guard          |
| `acuity_gap_major`         | `acuity_gap >= 2`                                                                                                                                                                                                                                                  | Flow guard          |
| `actor_is_charge`          | `actor_authorized` ∧ role ∈ {charge_nurse, shift_lead}                                                                                                                                                                                                             | Flow guard          |
| `output_verified`          | agent output matches its expected schema ∧ all field values in range ∧ no data-plane invariant broken ∧ no identifier leak in a redacted payload                                                                                                                   | Output Verification |
| `safety_violation`         | a verification failure that a retry cannot fix: an identifier reached a redacted payload, or a safety invariant was broken. Splits `¬output_verified` into structural (this guard true, halt) vs. recoverable (this guard false, retry)                            | Output Verification |

> `actor_authorized`: the person is allowed to act: right **role** (nurse / charge nurse), patient in their **jurisdiction** (ward/shift), and **clearance** matches the data class. Reused wherever an action needs a person behind it.
>
> `release_authorized`: the case may be closed by release when there is a **valid reason** (discharge / AMA / transfer / admit) **and** a charge-role signer (charge nurse or shift lead). State-independent: release can happen from anywhere.
>
> `acuity_gap` = the absolute difference between `nurse_proposed_acuity` and `system_proposed_acuity`.
>
> `safety_passed` / `approved` = case-level flags set when `safety_validating` returns pass and (where required) the human gate resolves. `move_authorized` reads these so treatment can never start on an unvalidated/unapproved case.
>
> `jurisdiction`: the actor's assigned organizational scope (ward/unit/shift). `data-class`: the sensitivity tier of the data being accessed (full patient info vs. redacted), which must match the actor's clearance. `N`: max retries, set per agent, not globally.

---

## Actions

These actions are the side effects a transition can trigger.

| Action                                   | Performed by                | Side-effecting?        | Idempotent?                          | Output                                 | Notes / Arrow                                                                                                                                                     |
| ---------------------------------------- | --------------------------- | ---------------------- | ------------------------------------ | -------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `invoke_intake_parser`                   | Flow step                   | no                     | yes                                  | ParseResult                            | 3 (deterministic field-presence + schema check)                                                                                                                   |
| `fetch_patient_data`                     | Flow step, CRM              | no (read)              | yes                                  | PatientRecord                          | 4b, stable-ID lookup; found / not-found / db-error                                                                                                                |
| `patch_patient_data`                     | Flow step, CRM              | yes                    | _(to confirm)_                       |                                        | write-back of this visit's new data; deferred if DB was down                                                                                                      |
| `build_model_payload`                    | Flow step, Input Normalizer | no                     | yes                                  | RedactedMsg + scores                   | 5, merge history + new data; **drop identifiers**; key by `case_id`; score urgency                                                                                |
| `invoke_acuity_classifier`               | Flow step                   | no                     | yes                                  | {system_proposed_acuity, confidence, danger_zone_vitals} | 7 (the LLM proposes a level from the ESI criteria — decision points A/B/C by judgment — then decision point D is computed and attached as an annotation)        |
| `annotate_danger_zone_vitals`            | Acuity Classifier           | no                     | yes                                  | which vitals breached                 | ESI decision point D, computed inside the classifier against the age-banded table and returned with the proposal. **Annotates only** — never changes any acuity. Surfaces on the patient card. See the note under Safety invariants |
| `invoke_safety_validation`               | Flow step                   | no                     | yes                                  | {verdict, reasons}                     | 9                                                                                                                                                                 |
| `invoke_human_escalation`                | Flow step                   | yes (queues human)     | yes (dedupe by case_id)              | ApprovalRequest                        | 9c / 11                                                                                                                                                           |
| `start_reassessment_timer`               | Flow step                   | yes                    | yes (dedupe by case_id)              | TimerHandle                            | 13                                                                                                                                                                |
| `notify_user(reason)`                    | Flow step                   | yes                    | yes (dedupe by `case_id` + `reason`) | Notification                           | reassessment / missing / resubmit / invalid-input / accepted / approval                                                                                           |
| `emit_event_log`                         | Flow step, Audit            | yes (append)           | yes                                  | TraceRecord                            | every transition                                                                                                                                                  |
| `verify_output(agent, output)`           | Output Verification         | no (read)              | yes                                  | {passed, violations}                   | runs after each agent proposal; checks schema, value ranges, invariants, and identifier leaks; emits `VERIFICATION_PASSED` / `VERIFICATION_FAILED`                |
| `explain_denial`                         | Flow step, Prolog           | no                     | yes                                  | Explanation                            | BLK, the "why" string for a denied action                                                                                                                         |
| `auto_resolve_acuity_to_nurse`           | Flow step                   | yes                    | yes                                  | acuity                                 | `acuity <- nurse_proposed_acuity`; `acuity_source <- auto_resolved`; call `assign_order_key`; log both inputs + choice                                            |
| `apply_human_acuity(choice)`             | Flow step                   | yes                    | yes                                  | acuity                                 | `acuity <- choice`; `acuity_source <- human_confirmed`; call `assign_order_key`; log resolver + role                                                              |
| `assign_order_key(acuity, arrival_time)` | Flow step                   | yes                    | yes                                  | order_key                              | the single writer of `order_key`; the one function that computes `(acuity, arrival_time)`, called at intake for the initial key and again whenever acuity changes |
| `sign_release(reason)`                   | Flow step, nurse            | yes                    | yes                                  | ReleaseRecord                          | record reason + actor; then `case_closed`                                                                                                                         |
| `alert_technician(agent)`                | Flow step, Technician       | yes                    | yes                                  | Alert                                  | fires ops alert; does not block degraded flow                                                                                                                     |
| `fallback_manual(agent)`                 | Flow step                   | yes                    | yes                                  |                                        | switch a fail-open agent to its human/manual substitute                                                                                                           |
| `resume_at_failed_stage(agent)`          | Flow step                   | yes                    | yes                                  |                                        | on `AGENT_RECOVERED`, re-enter the pipeline at the stage that failed                                                                                              |
| `execute_treatment_move`                 | Flow step, Tool Gateway     | yes (**irreversible**) | yes (dedupe by `idempotency_key`)    | ActionRequest -> ToolReceipt / timeout | the one irreversible external action; the Gateway is the single execution point. See execution contract below.                                                    |

> **Future design, not a current requirement.** This describes an external ward system that this project will never have (decided 2026-09-19; the single-execution-writer invariant was dropped for the same reason). Today a move into treatment is a board column change, guarded by I5 and I6.
>
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

When an error or timeout occurs, the Flow retries up to the agent's own retry budget `N` (`retry_budget_left`). After that, it follows the agent's declared direction. Fallback and technician alert run in parallel and do not block each other. A halted case leaves `agent_failed` only when the technician resolves the outage (`AGENT_RECOVERED`, then `resume_at_failed_stage`).

> The **Intake Parser** is a deterministic validator, so a "crash" is rare; a validation problem is already covered by demo cases 2/3, and a genuine software fault degrades to the same manual-entry path. It is therefore not listed as a separate failure row.

| Agent                   | Critical? | Fail direction                | Retry `N`      | On exhaustion                                                                                                                                                                                                                                        |
| ----------------------- | --------- | ----------------------------- | -------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| CRM / Patient DB        | No        | **open (degrade)**            | _(to confirm)_ | Continue on **intake-only data** (skip history); **flag the case +** `alert_technician`; defer `patch_patient_data` until the DB returns. New-patient empty result is _not_ a failure.                                                               |
| Acuity Classifier       | No        | **open (degrade)**            | _(to confirm)_ | Drop the system acuity; **fall back to** `nurse_proposed_acuity`; **discrepancy gate is disabled for the outage, flag these cases "cross-check off, review later"**; `alert_technician`.                                                             |
| PII filter, schema-drop | **Yes**   | **closed (halt)**             | _(to confirm)_ | Deterministic identifier drop. A hard code fault here **stops the line** (`agent_failed`): continuing could leak identifiers. `alert_technician`; resumes only on `AGENT_RECOVERED`.                                                                 |
| Safety Validation       | Yes       | degrade-to-human              | _(to confirm)_ | Do **not** hard-halt: route **every** case to charge nurse (`awaiting_human_approval`) so I5 (no bypass) still holds; `alert_technician`.                                                                                                        |
| Human Escalation bridge | Yes       | degrade-to-human              | _(to confirm)_ | `alert_technician`; fall back to a manual notification channel for the gate.                                                                                                                                                                         |

> **Principle:** non-critical, continue via fallback and notify; critical-open, degrade to human and notify; critical-closed, halt and notify, then resume on recovery.
>
> **On the PII filter:** intake carries no free-text field, so identifier handling is entirely deterministic schema-drop (the critical-closed row). There is no probabilistic redactor and therefore no degrade path for one. Removing free text is what makes this true — a prose box would reintroduce identifiers that schema-drop cannot catch.

---

## Transitions (complete table)

This table is the core of the state machine. Each edge is shown as **current state +** `EVENT` **[guard] -> next state / actions**. The **Validation Layer** column names which layer authorizes or denies the transition; the **Explanation** column is the human-readable reason (also written to the audit log). The **Arrow** column matches the diagram. After the table are the named scenario paths (regression cases) and the forbidden sequences the safety layer must block.

| Current (control)             | Event                                             | Guard                                                        | Validation Layer                           | Actions                                                                                                  | Next (control)               | Explanation                                                           | World effect                               | Arrow                  |
| ----------------------------- | ------------------------------------------------- | ------------------------------------------------------------ | ------------------------------------------ | -------------------------------------------------------------------------------------------------------- | ---------------------------- | --------------------------------------------------------------------- | ------------------------------------------ | ---------------------- |
|                               | `CASE_SUBMITTED`                                  |                                                              | Schema                                     | route channel; **assign** `order_key`                                                                    | `intake_received`            | new case entered                                                      |                                            | 1a                     |
| `intake_received`             | `MESSAGE_NORMALIZED`                              |                                                              |                                            | `emit_event_log`                                                                                         | `parsing`                    | input normalized                                                      |                                            | 2                      |
| `parsing`                     | _(on entry)_                                      |                                                              |                                            | `invoke_intake_parser`                                                                                   | `parsing`                    | run validator                                                         |                                            | 3                      |
| `parsing`                     | `DATA_PARSED`                                     | `required_fields_complete` ∧ `input_is_valid`                | Schema                                     | `emit_event_log`                                                                                         | `data_parsed`                | submission valid                                                      |                                            | 4                      |
| `parsing`                     | `MISSING_FIELDS_DETECTED`                         | ¬`required_fields_complete`                                  | Schema                                     | `notify_user("request fields")`                                                                          | `missing_fields_requested`   | required fields missing                                               |                                            | 16                     |
| `parsing`                     | `SUBMISSION_FAILED`                               | (demo case 3)                                                | Schema                                     | `notify_user("resubmit or manual")`                                                                      | `submission_failed`          | submission unusable                                                   |                                            | 17                     |
| `parsing`                     | `INVALID_INPUT_DETECTED`                          | ¬`input_is_valid`                                            | Schema / PII                               | `notify_user("invalid input")`                                                                           | `input_rejected`             | invalid schema or injection                                           |                                            | 18                     |
| `missing_fields_requested`    | `FIELDS_SUBMITTED`                                |                                                              |                                            |                                                                                                          | `parsing`                    | nurse supplied fields                                                 |                                            | 1b.x                   |
| `submission_failed`           | _(auto / manual)_                                 |                                                              |                                            | `notify_user`                                                                                            | `intake_received`            | resubmit                                                              |                                            | 1a·resubmit            |
| `input_rejected`              | _(auto)_                                          |                                                              |                                            | `notify_user`                                                                                            | `intake_received`            | rejected, await new input                                             |                                            | 1a·rejected            |
| `data_parsed`                 | _(on entry)_                                      |                                                              |                                            | `fetch_patient_data` (stable-ID lookup)                                                                  | `resolving_identity`         | look up record                                                        |                                            | 4b                     |
| `resolving_identity`          | `PATIENT_RESOLVED` (found)                        | `patient_found` ∧ ¬`no_active_duplicate`                     | Datalog (provenance)                       | `notify_user("case already open")`                                                                       | `input_rejected`             | duplicate active case for this patient                                |                                            | 4b·duplicate           |
| `resolving_identity`          | `PATIENT_RESOLVED` (found)                        | `patient_found` ∧ `no_active_duplicate`                      | Datalog (provenance)                       | merge history + new data                                                                                 | `redacting_routing`          | record found                                                          |                                            | 4b·found               |
| `resolving_identity`          | `PATIENT_RESOLVED` (not-found)                    | `db_reachable` ∧ ¬`patient_found`                            | Datalog (provenance)                       | continue on intake-only (silent, normal)                                                                 | `redacting_routing`          | new patient, no history                                               |                                            | 4b·new                 |
| `resolving_identity`          | `PATIENT_RESOLVED` (db-error)                     | ¬`db_reachable`                                              |                                            | continue on intake-only; flag; `alert_technician`; defer `patch_patient_data`                            | `redacting_routing`          | DB unreachable, degraded                                              |                                            | AF·db                  |
| `redacting_routing`           | _(on entry)_                                      |                                                              | OPA (no-identifiers)                       | `build_model_payload` (drop identifiers; key by `case_id`)                                               | `redacting_routing`          | build model payload                                                   |                                            | 5                      |
| `redacting_routing`           | `AGENT_FAILED` (PII schema-drop)                  | ¬`retry_budget_left`                                         | OPA (no-identifiers)                       | `alert_technician`                                                                                       | `agent_failed`               | redaction faulted, halt                                               |                                            | AF·PII                 |
| `redacting_routing`           | `REDACT_ROUTE_DONE`                               |                                                              | OPA (no-identifiers)                       |                                                                                                          | `classifying`                | payload clean                                                         |                                            | 6                      |
| `classifying`                 | _(on entry)_                                      |                                                              |                                            | `invoke_acuity_classifier` (LLM proposes, then the ESI vitals rule)                                             | `classifying`                | run classifier                                                        |                                            | 7                      |
| `classifying`                 | `AGENT_FAILED` (Acuity Classifier)                | ¬`retry_budget_left`                                         |                                            | `fallback_manual` (use `nurse_proposed_acuity`; disable gate; flag) + `alert_technician`                 | `safety_validating`          | classifier down, use nurse acuity                                     |                                            | AF·classifier          |
| `agent_failed`                | `AGENT_RECOVERED`                                 |                                                              |                                            | `resume_at_failed_stage`                                                                                 | (the failed stage)           | outage resolved                                                       |                                            | AF·recover             |
| `classifying`                 | `ACUITY_PROPOSED`                                 |                                                              |                                            | `emit_event_log`                                                                                         | `acuity_proposed`            | acuity proposed                                                       |                                            | 8                      |
| `acuity_proposed`             | _(on entry)_                                      | `acuity_agree`                                               | Z3 (band totality)                         | set `acuity_source = human_confirmed`                                                                    | `safety_validating`          | nurse and system agree                                                |                                            | 9a                     |
| `acuity_proposed`             | _(on entry)_                                      | `acuity_gap_minor`                                           | Z3 (band totality)                         | `auto_resolve_acuity_to_nurse`                                                                           | `safety_validating`          | gap of 1, take the nurse's value                                      |                                            | 9b                     |
| `acuity_proposed`             | _(on entry)_                                      | `acuity_gap_major`                                           | Z3 (band totality)                         | `invoke_human_escalation` ("show discrepancy + rationale; request resolution")                           | `awaiting_human_approval`    | gap ≥2, charge nurse decides                                          | -> `human_review`                          | 9c                     |
| `safety_validating`           | `VERDICT_PROPOSED`                                | `safety_pass`                                                | Prolog / Datalog / OPA                     | `emit_event_log`; set `safety_passed`                                                                    | `verdict_proposed`           | safety passed                                                         |                                            | 10                     |
| `safety_validating`           | `VERDICT_PROPOSED`                                | ¬`safety_pass`                                               | Prolog / Datalog / OPA                     | `invoke_human_escalation`                                                                                | `awaiting_human_approval`    | safety failed, human decides                                          | -> `human_review`                          | 10·fail                |
| `safety_validating`           | `AGENT_FAILED` (Safety Validation)                | ¬`retry_budget_left`                                         |                                            | `invoke_human_escalation` (route all to charge) + `alert_technician`                                     | `awaiting_human_approval`    | validator down, route to charge                                       | -> `human_review`                          | AF·safety              |
| `verdict_proposed`            | _(on entry)_                                      | `escalation_needed`                                          | Temporal                                   | `invoke_human_escalation`                                                                                | `awaiting_human_approval`    | escalation needed                                                     | -> `human_review`                          | 11                     |
| `verdict_proposed`            | _(on entry)_                                      | ¬`escalation_needed`                                         | Temporal                                   | `start_reassessment_timer`; set `approved`                                                               | `monitoring`                 | cleared to queue                                                      | -> `waiting`                               | 11·pass                |
| `awaiting_human_approval`     | `ESCALATION_PROPOSED`                             |                                                              |                                            | `emit_event_log`                                                                                         | `awaiting_human_approval`    | escalation recorded                                                   |                                            | 12                     |
| `awaiting_human_approval`     | _(auto)_                                          | escalation = true                                            |                                            | `notify_user("request approval")`                                                                        | `awaiting_human_approval`    | approval requested                                                    | -> `human_review`                          | 20                     |
| `awaiting_human_approval`     | `GATE_TIMER_ASSIGNED_NURSE`                       |                                                              | Temporal                                   | notify assigned charge nurse (UI)                                                                        | `awaiting_human_approval`    | reminder 1                                                            |                                            | 20a                    |
| `awaiting_human_approval`     | `GATE_TIMER_ESCALATE_ANY_CHARGE`                  |                                                              | Temporal                                   | re-alert / widen to any charge-role nurse                                                                | `awaiting_human_approval`    | reminder 2, widen                                                     |                                            | 20b                    |
| `awaiting_human_approval`     | `APPROVAL_RESPONSE_RECEIVED`                      | `actor_is_charge` (acuity branch)                            | Prolog (authorization)                     | `apply_human_acuity` -> `emit_event_log`; set `approved`                                                 | `safety_validating`          | charge nurse resolved acuity                                          |                                            | 1b.z·acuity            |
| `awaiting_human_approval`     | `APPROVAL_RESPONSE_RECEIVED`                      | safety-fail: `actor_is_charge` ∧ `correction_changed`        | (charge-role correction)                   | `apply_correction`; require change to `acuity`/`clinical_status`/`safety_verdict`                        | `safety_validating`          | correct-and-revalidate (no override); re-run safety on corrected case | -> `safety_validating`                     | 1b.z·safety            |
| `monitoring`                  | _(on entry)_                                      |                                                              |                                            | `start_reassessment_timer`                                                                               | `monitoring`                 | queued, timer running                                                 | -> `waiting`                               | 13                     |
| `monitoring`                  | `REASSESSMENT_TIMEOUT` ∨ `DETERIORATION_DETECTED` |                                                              | Temporal                                   | `notify_user("reassessment")`                                                                            | `reassessment_required`      | re-triage due                                                         | -> `reassessment_required`                 | 14                     |
| `reassessment_required`       | _(nurse re-files)_                                |                                                              |                                            |                                                                                                          | `parsing`                    | full front-door rerun                                                 |                                            | 15                     |
| `monitoring`                  | `MOVE_REQUESTED`                                  | `move_authorized` (target = treatment_started)               | OPA (authorization)                        | `emit_event_log`                                                                                         | `monitoring`                 | move authorized                                                       | -> `treatment_started`                     | 1b.y                   |
| `monitoring`                  | `TRANSITION_ACCEPTED`                             | `move_authorized`                                            | OPA (authorization)                        | `notify_user("accepted")`                                                                                | `monitoring`                 | move confirmed                                                        | -> `treatment_started`                     | 19                     |
| `monitoring`                  | `TREATMENT_COMPLETE`                              |                                                              |                                            | `emit_event_log`                                                                                         | `monitoring`                 | treatment done, sign off                                              | `treatment_started` -> `formal_validation` | FV                     |
| _(any active state)_          | `RELEASE_REQUESTED`                               | `release_authorized` (reason ∧ `actor_is_charge`)            | OPA (authorization)                        | `sign_release`                                                                                           | `case_closed`                | release signed                                                        | -> `patient_released`                      | REL                    |
| _(any state, guarded action)_ | `ACTION_DENIED`                                   | a required guard fails                                       | OPA / Prolog / Z3 / Datalog / Temporal     | `emit_event_log`; `explain_denial`                                                                       | **(stays in current state)** | formal layer denied the attempted action                              |                                            | BLK                    |
| _(any proposing state)_       | `VERIFICATION_PASSED`                             | `output_verified`                                            | Output Verification                        | write proposal to shared state; `emit_event_log`                                                         | (the edge's normal next)     | output verified, proceed                                              |                                            | V·pass                 |
| _(any proposing state)_       | `VERIFICATION_FAILED` (recoverable)               | ¬`output_verified` ∧ `retry_budget_left(agent)`              | Output Verification                        | discard output; `emit_event_log`; re-invoke the step                                                     | (stay, re-run step)          | malformed output, retry                                               |                                            | V·retry                |
| _(any proposing state)_       | `VERIFICATION_FAILED` (recoverable, budget spent) | ¬`output_verified` ∧ ¬`retry_budget_left(agent)`             | Output Verification                        | discard output; take the step's `AGENT_FAILED` path                                                      | (same as `AF·<agent>`)       | retries spent, degrade as agent-failed                                | (per that agent's row)                     | V·exhausted            |
| _(any proposing state)_       | `VERIFICATION_FAILED` (structural)                | ¬`output_verified` ∧ `safety_violation`                      | Output Verification / OPA                  | discard output; `alert_technician`; `emit_event_log`                                                     | `agent_failed`               | safety/invariant breach, halt                                         |                                            | V·halt                 |
| `redacting_routing`           | `VERIFICATION_FAILED` (structural)                | ¬`output_verified` ∧ identifier present in payload           | Output Verification / OPA (no-identifiers) | discard payload; `alert_technician`; `emit_event_log`                                                    | `agent_failed`               | identifier leaked into redacted payload, halt                         |                                            | V·halt·PII             |
| `classifying`                 | `VERIFICATION_FAILED` (recoverable)               | ¬`output_verified` ∧ `retry_budget_left(acuity_classifier)`  | Output Verification                        | discard output; `emit_event_log`; re-invoke classifier                                                   | `classifying`                | classifier output malformed, retry                                    |                                            | V·retry·classifier     |
| `classifying`                 | `VERIFICATION_FAILED` (recoverable, budget spent) | ¬`output_verified` ∧ ¬`retry_budget_left(acuity_classifier)` | Output Verification                        | discard output; `fallback_manual` (use `nurse_proposed_acuity`; disable gate; flag) + `alert_technician` | `safety_validating`          | classifier output unusable, use nurse acuity                          |                                            | V·exhausted·classifier |
| `safety_validating`           | `VERIFICATION_FAILED` (recoverable)               | ¬`output_verified` ∧ `retry_budget_left(safety_validation)`  | Output Verification                        | discard output; `emit_event_log`; re-invoke validator                                                    | `safety_validating`          | verdict malformed, retry                                              |                                            | V·retry·safety         |
| `safety_validating`           | `VERIFICATION_FAILED` (recoverable, budget spent) | ¬`output_verified` ∧ ¬`retry_budget_left(safety_validation)` | Output Verification                        | discard output; `invoke_human_escalation` (route all to charge) + `alert_technician`                     | `awaiting_human_approval`    | validator output unusable, route to charge                            | -> `human_review`                          | V·exhausted·safety     |

> **On the** `BLK` **row.** When any guarded action is attempted and its guard fails (unauthorized treatment move, unauthorized release, non-charge nurse resolving a gate, or any symbolic-layer denial), the **attempted action is blocked**, the denying layer and reason are logged, and a Prolog explanation is produced. The **case does not move**; only the attempt is refused. This is Triage Guard's realization of the rubric's "Violation detected -> Blocked" outcome.
>
> **On output verification.** Every proposal edge in this table (`DATA_PARSED`, `ACUITY_PROPOSED`, `VERDICT_PROPOSED`, and the rest) passes through an output-verification step before the Flow reads the proposal into shared state (`V·pass`). A verification failure falls into one of two categories. **Recoverable** failures (schema mismatch, value out of range, null or wrong type) mean the step is healthy but its output is malformed: the output is discarded and the step is re-invoked on its existing `retry_budget_left(agent)`, shared with crash retries (`V·retry`). When that budget is spent, the case takes the same degraded path as the step's own `AGENT_FAILED` row (`V·exhausted`), so there is one degrade path per step, not two. **Structural** failures (an identifier in a redacted payload, or any safety-invariant breach) are not retried, because a retry cannot fix them: the output is discarded and the case halts in `agent_failed`, with `alert_technician`, and resumes only on `AGENT_RECOVERED` (`V·halt`). The rows above show the general rule and the three edges where it bites hardest: redaction (`V·halt·PII`, the identifier-leak halt), the classifier (`V·retry·classifier` / `V·exhausted·classifier`, the LLM output most likely to be malformed), and safety validation (`V·retry·safety` / `V·exhausted·safety`). Across all categories one rule never changes: **a malformed or unsafe output is never written to state.** The check is deterministic, so it sits on the symbolic side of the neuro-symbolic split.

> **About the arrow labels:** numbered arrows (`1a`, `1b.x`, `1b.y`, `1b.z`, `2` to `20`) appear on the diagram. Lettered/suffixed rows (`4b`, `9a` to `9c`, `10·fail`, `11·pass`, the `AF·…` agent-failure edges, the `V·…` output-verification edges, `20a`/`20b`, `FV`, `REL`, `BLK`) are internal details of those same edges, listed for completeness.

> **Documented scenario paths (regression cases):**
>
> - **Normal entry (demo case 1):** `1a, 2, 3, 4, 4b, 4b·found, 5, 6, 7, 8, 9a, 10, 11·pass, 13`
> - **New patient (no record):** `… 4b, 4b·new, 5 …`
> - **DB down (degrade):** `… 4b, AF·db, 5 …` (intake-only, flagged)
> - **Danger-zone vitals:** the ESI decision point D check is computed and attached to the case as an annotation. It changes no acuity and alters no path — the case follows the normal gate route by gap vs nurse (`9a/9b/9c`) exactly as if the vitals were normal.
> - **Acuity gap ≥2, charge nurse:** `… 8, 9c, 1b.z·acuity, 10, 11·pass, 13`
> - **Acuity gap 1 auto-resolve:** `… 8, 9b, 10, 11·pass, 13`
> - **Safety fail, gate:** `… 10·fail` -> correct-and-revalidate (see resolved safety-fail branch)
> - **Reassessment:** `13, 14, 15, 3 …` (full front-door rerun)
> - **Move to treatment, close:** `1b.y, 19` (`treatment_started`), `FV` (on `TREATMENT_COMPLETE`, `formal_validation`), `REL`
> - **AMA / release from waiting:** `… 13, REL` (with `reason = ama`, charge-role sign-off)
> - **Missing fields (demo case 2):** `1a … 16, 1b.x`
> - **Failed submission (demo case 3):** `1a … 17, 1a·resubmit` (or full manual entry)
> - **Invalid / injection (demo case 4):** `1a … 18, 1a·rejected`
> - **Blocked action (denied):** any guarded action with a failing guard, `BLK` (logged + explained, case stays put)
> - **Classifier down (degrade):** `AF·classifier`, nurse acuity, gate off, continue
> - **PII schema-drop down (halt, recover):** `AF·PII`, `agent_failed`, `AF·recover` on `AGENT_RECOVERED`
> - **Safety validator down (degrade-to-human):** `AF·safety`, every case to charge nurse
> - **Malformed classifier output (recoverable):** `V·retry·classifier` until the budget is spent, then `V·exhausted·classifier` (= `AF·classifier`), nurse acuity, gate off
> - **Identifier leak caught at verification (structural):** `V·halt·PII`, `agent_failed`, `AF·recover` on `AGENT_RECOVERED`

> **Forbidden sequences (must be provably blocked):**
>
> - **Served out of order:** a patient placed ahead of a more acute one. Blocked by **I1 Acuity ordering**.
> - **Clock rewrites acuity:** `REASSESSMENT_TIMEOUT` mutates acuity directly instead of forcing a re-look. Blocked by **I3 Acuity write-authority** (the timer only triggers 14, 15; the nurse's re-filed form changes acuity).
> - **Approval bypass:** a case entering the queue or treatment without passing safety and any required approval in its current triage. Blocked by **I5 No bypass**.
> - **Unauthorized action:** any human action by a person whose role does not permit it. Blocked by **I14 Authorization**; caught by the `BLK` outcome (logged + explained; case does not advance).
> - **Release without a reason:** blocked by **I9 Release**.
> - **Identifiers reach the model:** blocked by **I11 Identifier storage** (the case holds no identifiers at all) and **I12 Model input**.
> - **Prose or injection reaches the model:** blocked by **I12 Model input** (only fixed-choice or numeric fields reach the model).
> - **System fails to escalate a waiter:** a waiting case passes T without the system raising an escalation. Blocked by **I15 Escalation** (the guarantee is that the system escalates, not that a human acts).
> - **Stuck or dropped case:** a patient's case stops running before release (a halt, a missing-fields round trip, an exhausted correction loop). Blocked by **I10 No dropped case**.
> - **Duplicate active case:** two open cases for one patient. Blocked by **I19 No duplicate active case**.

---

## Context / State variables (Data plane)

This section lists the data the machine keeps for each case: the acuity model (`nurse_proposed_acuity`, `system_proposed_acuity`, final `acuity`, `acuity_gap`, the `acuity_source` lock), the queue key, and per-agent retry counters.

| Variable                              | Plane   | Type                                                      | Set by                             | Notes                                                                                                                                                  |
| ------------------------------------- | ------- | --------------------------------------------------------- | ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `case_id`                             | Control | id                                                        | intake                             | downstream reference key; the model sees the case by this, never by name/ID                                                                            |
| `channel`                             | Control | enum(website)                                             | Channel Router                     | website intake form                                                                                                                                    |
| `parsed_fields`                       | Data    | struct                                                    | Intake Parser                      | container for the webform fields: `nurse_proposed_acuity`, `chief_complaint` (a fixed category, not prose), vitals and the other structured answers; no free text (I12); schema _(to define)_ |
| `stable_patient_id`                   | Data    | id                                                        | CRM, at intake                     | the CRM's internal record number; the case's only patient reference; excluded from the model payload (I12)             |
| `parsed_fields.nurse_proposed_acuity` | Data    | enum/level                                                | nurse (via webform)                | **mandatory**; absent, then demo case 2 / arrow 16; always nurse-supplied, never inferred                                                              |
| `redacted_payload`                    | Data    | struct                                                    | PII filter                         | model-facing; clinical fields only, keyed by `case_id`; **no identifiers**                                                                             |
| `urgency_scores`                      | Data    | {sentiment, distress, pain}                               | Input Normalizer                   |                                                                                                                                                        |
| `system_proposed_acuity`              | Data    | enum/level                                                | Acuity Classifier                  | the classifier's **proposal**; input to the gap, not the final value                                                                                   |
| `acuity`                              | Data    | enum/level                                                | Flow step (gate resolution)        | the **final resolved** acuity, written only by 9a/9b/9c; scale: **ESI 1–5**                                                            |
| `acuity_gap`                          | Data    | int                                                       | derived                            | absolute difference between `nurse_proposed_acuity` and `system_proposed_acuity`                                                                       |
| `acuity_source`                       | Data    | enum(system, auto_resolved, human_confirmed)              | Flow step                          | provenance of the final `acuity`. `auto_resolved` = gap of 1, settled to the nurse's value                                                             |
| `confidence`                          | Data    | float                                                     | Acuity Classifier                  | threshold for `confidence_ok`                                                                                                                          |
| `safety_verdict`                      | Data    | {pass/fail, reasons}                                      | Safety Validation                  |                                                                                                                                                        |
| `safety_passed`                       | Data    | bool                                                      | Flow step                          | set on arrow 10; read by `move_authorized`                                                                                                             |
| `approved`                            | Data    | bool                                                      | Flow step                          | set on 11·pass / 1b.z·acuity; read by `move_authorized`                                                                                                |
| `clinical_status`                     | World   | enum (see World plane)                                    | Flow step                          | board column                                                                                                                                           |
| `acuity_bucket`                       | Data    | enum(emergent[1–2], queued[3–5])                          | derived from final `acuity`        | display label on the board only; **not** a sort key                                                                                                    |
| `arrival_time`                        | Data    | timestamp                                                 | intake                             | tiebreaker within the same acuity                                                                                                                      |
| `order_key`                           | World   | (acuity, arrival_time)                                    | `assign_order_key`                 | **assigned at system entry**; persists across state changes                                                                                            |
| `release_reason`                      | Data    | enum(discharge, ama, transfer, admit)                     | nurse                              | recorded at close                                                                                                                                      |
| `reassessment_timer`                  | World   | timer                                                     | Waiting Room Monitor               | the single per-patient timer; interval set by acuity band; started on entry to `monitoring`; drives `REASSESSMENT_TIMEOUT`                             |
| `gate_timer`                          | World   | timer                                                     | Waiting Room Monitor               | separate per-gated-case timer; drives the `GATE_TIMER_*` approval-reminder ladder (not a reassessment timer)                                           |
| `retry_count[agent]`                  | Control | int per agent                                             | Flow step                          | per-agent budget for agent-failure handling                                                                                                            |

---

## Queue ordering rule

This section explains how the waiting queue is sorted, and what does **not** affect your place in line. Only a real acuity change reorders you; state labels and the clock do not. Fairness comes from the sort; liveness comes from the escalation path, not the sort.

`order_key` sorts the waiting queue by two keys, in order:

1. **Acuity:** lower ESI first, so a more acute patient is always ahead (I1).
2. **Arrival time:** within the same acuity, earlier arrival first.

Consequences:

- A 1 is always ahead of a 2, a 2 ahead of a 3, and so on. `acuity_bucket` (emergent 1–2 / queued 3–5) remains only as a display label.
- `order_key` is assigned at system entry and **persists across state changes**: going into `reassessment_required` or `human_review` does not move you in line.
- A case disputed at the gate (gap ≥ 2) queues by the nurse's acuity until the charge nurse settles it.
- Only a real **acuity** change (via reassessment/deterioration/override) changes your key (I2). State labels and the clock never do.
- **Timer firing forces a reassessment (14, 15); it does not re-sort the queue.**
- Under overload, ordering guarantees **fairness**, not **service**. A strict acuity sort could leave a low-acuity patient waiting indefinitely (starvation, a liveness failure); that is handled by the escalation path (I15), not the sort.

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

**Neural component.** A single LLM, the **Acuity Classifier**, reads the redacted, `case_id`-keyed clinical payload and proposes `system_proposed_acuity` with a confidence, grounded in the ESI criteria supplied in its prompt. That is the only generative model in the decision path, and the only component performing ESI decision points A, B and C — all three require clinical judgment over the case, and ESI defines the high-risk criterion at point B by judgment with worked examples rather than as a closed list. ESI decision point D (danger-zone vitals) is computed deterministically inside the same actor, against the age-banded table, and **annotates** the case; it changes no acuity, because the handbook treats D as a judgment applied in clinical context rather than a mechanical threshold (see the note under Safety invariants). All four decision points therefore live in the Acuity Classifier — classification is one actor's job — with only D on the symbolic side of the split. The Intake Parser (validator), the PII schema-drop, and the Safety Validation layer are deterministic, not neural.

**Symbolic layer.** Everything that must not be left to a stochastic model: **OPA** (authorization, no-identifiers, release), **Z3** (constraint consistency and band totality), **Prolog** (authorization inference and explanation), **Datalog** (provenance and information-flow), and **temporal logic** (sequence rules). The LangGraph state graph is the spine that sequences them. The Acuity Classifier only proposes, and the symbolic layer decides; each node returns its own results into the graph state.

**What is never left to the model.** Final acuity (resolved at the gate, not the classifier's proposal), authorization, queue ordering, identifier handling, the move into treatment, and release. The model proposes; the symbolic layer disposes.

**On conflict.** When the model proposes something the symbolic layer forbids, the action is **not** taken: the attempt is denied (the `BLK` outcome), logged with the denying layer, and explained via Prolog. When the model and the nurse disagree on acuity by ≥2, the case goes to the charge nurse at the gate; the human decides, and that decision is `human_confirmed` and final.

---

## Safety invariants

These are the properties enforced by the symbolic layer (OPA, Z3, Prolog, Datalog) and the runtime temporal monitor. This is the governance half of the neurosymbolic split. Each property is a checkable proposition, and each one can fail: for each, a wrong system exists that it would catch. Reviewed and finalized 2026-09-19.

| #   | Property | Family | Statement | Temporal rule | Checked by |
| --- | -------- | ------ | --------- | ------------- | ---------- |
| I1 | Acuity ordering | Safety | No patient is ordered ahead of a more acute one. | `G(in_queue(a) ∧ in_queue(b) ∧ acuity(a) < acuity(b) → key(a) < key(b))` | tests; temporal monitor |
| I2 | Stable queue key | Safety | Once a patient has entered the system, their `order_key` changes only when their acuity changes. | `G(key_changed → acuity_changed)` | tests; temporal monitor |
| I3 | Acuity write-authority | Safety | Acuity is set only by the automatic settle (gap 0 or 1), a charge nurse at the human gate (acuity choice or safety correction), a re-triage, or the nurse's own value when the classifier is down. | `G(acuity_written → auto_settle ∨ charge_at_gate ∨ retriage ∨ nurse_fallback)` | Datalog (provenance); temporal monitor |
| I4 | Gap bands | Safety | For every gap between the nurse's and the model's acuity, exactly one outcome applies: 0 keeps the agreed level, 1 takes the nurse's level, 2 or more goes to the charge nurse. | `G(gap_known → ((gap=0 → keep) ∧ (gap=1 → nurse_level) ∧ (gap≥2 → at_gate)))` | Z3 (design time); per-gap tests |
| I5 | No bypass | Safety | A case enters the queue or treatment only after passing safety validation, and any required approval, in its current triage. Otherwise it waits at the human gate. | `G(enter_queue ∨ enter_treatment → safety_passed_this_triage ∧ approved_this_triage)`; `G(safety_failed ∨ safety_unavailable → (¬enter_queue ∧ ¬enter_treatment) U at_gate)` | OPA; temporal monitor |
| I6 | Single treatment start | Safety | A case starts treatment at most once. | `G(treatment_started → X G ¬treatment_started)` | guard; temporal monitor |
| I7 | Correct, then revalidate | Safety | A case that failed safety continues only after its acuity or clinical status is corrected, and it passes safety again. | `G(safety_failed → ¬leaves_gate U (corrected ∧ safety_passed))` | tests; temporal monitor |
| I8 | Bounded correction loop | Liveness (bounded) | After a set number of failed correction rounds, the case is handed to the next level of authority (per I14) and stops looping. | `G(correction_rounds ≥ N → F handed_up)` | tests; temporal monitor |
| I9 | Release | Safety + liveness | A release is possible at any point before the case is closed, and only with a valid reason. | `G(closed_by_release → valid_reason)`; `G(release_requested ∧ valid_reason ∧ ¬closed → F closed)` | OPA; tests per pause; temporal monitor |
| I10 | No dropped case | Safety | A patient's case stops running only when the patient is released. | `G(patient_case ∧ run_ended → released)` | graph wiring tests; temporal monitor |
| I11 | Identifier storage | Safety | Patient identifiers are stored only in the CRM, and shown only on authorized staff screens. | `G(stored(identifier, x) → x = CRM)`; `G(shown(identifier, s) → authorized_screen(s))` | Datalog (information flow) |
| I12 | Model input | Safety | The model receives only approved fields, each holding a fixed-choice value or a number. | `G(model_call → ∀f ∈ payload: approved(f) ∧ closed_value(f))` | OPA (closed-type schema) |
| I13 | Output validity | Safety | An agent output is saved only if it has the right shape and ranges and doesn't contradict itself. | `G(output_saved → well_formed ∧ in_range ∧ ¬self_contradictory)` | schema at creation; Prolog / Z3 (contradiction check) |
| I14 | Authorization | Safety | Every human action is performed only by a person whose role, according to the server's staff records, permits it. | `G(human_action(p, a) → permits(role(p), a))` | Prolog (roles, permissions, explanation) |
| I15 | Escalation | Liveness (bounded) | Whenever a case is waiting, in the queue or at the gate, the system escalates it within T, and keeps escalating, widening who is alerted, until someone acts. | `G(waiting ∧ ¬acted → F≤T escalated)` | timers; temporal monitor |
| I16 | Reassessment | Liveness (bounded) | Within T of entering the queue (T set by acuity), the patient is moved to reassessment required. | `G(enter_queue → F≤T(acuity) (reassessment_required ∨ ¬in_queue))` | timers + sweeper; temporal monitor |
| I17 | CRM write-back | Liveness (bounded) | Each visit's clinical data reaches the CRM within T of the CRM being reachable. | `G(visit_data_pending ∧ crm_reachable → F≤T crm_updated)` | timers; temporal monitor |
| I18 | Audit | Safety | Every change to a case, and every refused attempt, is written to its audit log with a reason, in one fixed record structure. | `G(step_ran → audit_record_added)` | checkpoint cross-check: every step adds a record |
| I19 | No duplicate active case | Safety | A patient with an active case — not yet released and not itself rejected as a duplicate — cannot have a second, distinct case opened for them via intake. | `G(new_case(p, c) ∧ (∃c' ≠ c: active_case(p, c')) → ¬enter_pipeline(c))` | Datalog (provenance) |
| I20 | Case immutability after close | Safety | Once a case is closed, none of its fields (acuity, clinical status, control state) change again. | `G(closed(c) → G ¬field_changed(c))` | guard; temporal monitor |
| I21 | Audit log append-only | Safety | An audit record, once written, is never modified or deleted. | `G(audit_record_added(r) → G(¬modified(r) ∧ ¬deleted(r)))` | Datalog (provenance); DB constraint |
| I22 | Single active writer per case | Safety | No two staff actions mutate the same case at the same time. | `G(¬(write(p1, c) ∧ write(p2, c) ∧ concurrent ∧ p1 ≠ p2))` | guard/lock; tests |

All T values live in one table in `triage-app/app/budgets.py`.

> **I15 is honest about what the system controls.** The system cannot force a swamped nurse to act, so it does not promise that a human acts within T; it promises it **raises an escalation** within T, and keeps raising it. Under overload the guarantee is escalation, not service.
>
> **The danger-zone vitals check annotates; it never changes an acuity.** ESI v5 treats decision point D as a judgment applied in clinical context, not a mechanical threshold, and its own worked examples prove it: a patient with a heart rate of 102 against a limit of 100, all other vitals normal, is assigned level 3 and explicitly not uptriaged (handbook ch. 6, Example Four); a patient at SpO2 91% *is* uptriaged, but reasoned from an infected wound and steroid-induced immunosuppression rather than from the number (Example Five). Code cannot distinguish those two cases. So the check is computed exactly and attached to the patient card, and two human-or-model judgments — the nurse's at intake and the classifier's — decide the level between them. The system therefore does **not** guarantee `G(danger_zone_vitals -> acuity == emergent)`. This is the same stance the retired red-flag rules carried: rules inform, judgment decides.
>
> **The known cost.** Decision point D exists to catch the "well-appearing ill" — the patient whose presentation reassures and whose numbers do not. That is precisely the case where both judgments may miss it, and the annotation is then the only signal. It needs a display surface; until the patient card exists the annotation reaches no one.
>
> **What is implemented is ESI decision point D, not ESI.** Decision points A (life-saving intervention), B (high-risk / altered mental status / severe distress) and C (resource count) are the classifier's judgment and are not encoded as rules. Do not read the presence of the vitals check as ESI conformance.
>
> **Acuity override vs. safety override.** "Override" in this document always means an
> **acuity** decision - a human choosing a triage level, including overriding an advisory
> vitals-raised proposal. It never means overriding a **safety-validation** verdict: there is no
> safety-override path in the system. A safety-fail is resolved only by correct-and-revalidate
> (see the resolved safety-fail branch), never by proceeding past a failed check.

---

## Temporal logic rules

Each invariant's temporal rule is in the **Temporal rule** column of the Safety invariants table above: the same rule, written over the case's event trace (its audit log). Propositions ending in `_this_triage` are enriched state: set when the event happens, reset when a new triage begins.

> **How they are checked.** Single-step rules are enforced as guards and policies before a step writes state (OPA, Prolog, the model-input schema). Z3 proves the gap bands (I4) and the output contradiction rules (I13) at design time. Datalog checks provenance (I3) and information flow (I11). The temporal monitor reads each case's audit log in order and flags the exact record where any rule breaks, including the deadlines (I15–I17). No rule uses `X` ("next step") for routing: real paths insert extra steps (for example `safety_fallback`), so ordering rules use `U` or `F≤T` instead.

---

## Symbolic governance layer: OPA, Z3, Prolog, Datalog

This section puts the neurosymbolic split into practice. The neural component (the Acuity Classifier LLM) only proposes. Before a step writes to `self.state`, proposals and attempted actions pass through the symbolic layers; if any denies, the action is blocked (`BLK`), never silently taken.

**OPA, policy and authorization (runtime).** A code-as-policy layer checked at the human-approval gate (arrows 11/20), treatment moves (`move_authorized` on 1b.y/19), redaction-to-classification (arrow 6, blocked if identifiers remain), and release (REL). It decides whether the actor holds an authorized role, whether a `sign_release` carries a valid `release_reason`, and whether a treatment move is backed by `safety_passed ∧ approved`. Backs **I5 No bypass**, **I9 Release** and **I12 Model input**. On "not allowed," the action is denied via `BLK`.

**Z3, constraint consistency (mainly design-time, plus a runtime guard).** Z3 does exhaustive proof: it shows a property holds for **every** reachable state, which example tests cannot. Design-time, it proves the acuity bands are **total and exclusive** (for every `acuity_gap ≥ 0` exactly one of 9a/9b/9c fires, so no case falls through the gate) and that the transition guards are satisfiable and mutually consistent. It also checks the output contradiction rules (I13): no combination of the classifier's ESI answers and its level may be accepted while contradicting itself. Backs **I4 Gap bands**, **I13 Output validity** and the integrity of the transition table. Z3 proves the spec sound before deployment; it does not route cases at runtime (that is OPA/Prolog/temporal).

**Prolog, inference and explanation (runtime).** Backs **I14 Authorization** and the explainability requirement. Facts (who is on shift, in what role) and rules (which action requires which role, what is blocked outright) let it infer whether a user may act, and produce the **why**: approved because the role has authority, or denied because the role is insufficient. If inference denies at the gate, the case stays in `awaiting_human_approval`; the explanation is what the nurse sees and what is written to the audit log. Prolog also produces the `explain_denial` string on the `BLK` row.

**Datalog, relations, information flow, provenance (runtime).** Transitive-relation analysis, two uses. First, information flow: whether a sensitive field (name, ID) can reach an external tool through the node chain without passing through payload construction; a query returning such a path exposes a leak. Second, provenance of the acuity value: only gate resolution, reassessment with new evidence, or an authorized human override are permitted writers; a timer alone is not, and neither is the vitals rule, which writes only the classifier's proposal. Backs **I11 Identifier storage** and **I3 Acuity write-authority**.

**How the roles divide.** OPA governs permissions and boundaries at runtime; Z3 proves at design time that no reachable state violates the numeric/logical constraints; Prolog infers and explains individual decisions at runtime; Datalog traces relations and flows at runtime. Together they cover the four symbolic requirements and stop a neural proposal from ever becoming an unchecked state write.

---

## Open decisions

Items still needing a decision before the spec is final. The OCR question is settled (intake is a structured webform); the safety-fail branch is now resolved (below).

1. ~~Acuity scale~~ - **resolved: ESI 1-5** (2026-09-13, see `plans/2026-09-13-acuity-classifier-design.md`). The `confidence_ok` threshold is still open.
1b. ~~ESI decision point D thresholds~~ - **resolved 2026-09-13**, transcribed from the ESI v5 handbook (`docs/Emergency_Severity_Index_Handbook.pdf`, Figure 6-1) into the design doc.
2. `confidence_ok`: **wire in** (low confidence, then gate) **or delete** (see Guards).
3. Per-agent retry budgets `N` (see Per-agent failure model).
4. ~~Formalisms for the no-approval-bypass, sole-writer, and injection-rejected rules~~ - **resolved 2026-09-19**: invariants reviewed and restated as I1–I18 with temporal rules (see Safety invariants).
5. `parsed_fields` schema (see Context / State variables). Intake is **structured only** as of 2026-09-13 — roughly ten dropdown / tick-box fields plus vitals; the free-text field is removed. The exact field list needs a pass with a clinician. Two fields are required by ESI decision point D and are not currently collected: **respiratory rate**, and **age band** (`<1mo`, `1-12mo`, `1-3y`, `3-5y`, `5-12y`, `12-18y`, `>18y`). The age band comes from the CRM record; pass the band rather than the date of birth, which is a direct identifier.
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

- **No bypass (I5):** correct-and-revalidate always re-runs safety on the corrected
  version, so there is no path to `treatment_started` that skips safety/approval.
- **No silent return:** a resolution must change `acuity`, `clinical_status`, or
  `safety_verdict`; an unchanged case cannot go back to `waiting`.

_Arrow 1b.z·safety remains a distinct guarded edge, separate from 1b.z·acuity, now with a
defined resolution._
