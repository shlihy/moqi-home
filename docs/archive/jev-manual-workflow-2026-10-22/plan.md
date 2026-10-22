# JEV in Moqi: implementation plan and acceptance contract

Plan date: 2026-10-22. Status: implementation in progress; acceptance is open.

## Outcome

A user can tell Moqi what to change, inspect its interpretation, apply the change to household memory, see the effect on subsequent shadow decisions, and undo it. Deliver the workflows in this order: scoped corrections, contextual preferences, corrections to learned routines. Run real local EdgeJev inference in a text playground before adding voice or physical device control.

The active implementation goal is to satisfy G0–G8 below. Writing this plan does not satisfy that goal. Hardware acceptance is a separate follow-up and does not block this software milestone.

## Scope and decisions

- Chinese household language is the primary acceptance language. Retain existing English commands; broader English interpretation is a later extension.
- Use EdgeJev's local laya backend initially. Record the runtime version, upstream revision, model configuration and artifact hashes actually used. Jev's hosted service is a different model and is not a substitute for local-model acceptance.
- Use a local browser UI served by a Python process bound to loopback. This process owns the model and persistent SQLite memory. The browser displays and submits user actions; it does not implement another learning algorithm.
- Preserve the existing static/Pyodide playground as a separate, clearly labelled mode. It remains usable without a Python service and cannot claim to have run EdgeJev.
- A model interpretation produces a preview requiring Apply. Cancel, ambiguity and model failure leave household memory unchanged. Applying a memory change does not send a device command.
- Use synthetic households for the public walkthrough. Keep model files, local statements, household data, credentials and databases outside published source assets.
- Reuse the existing learning, evidence, safety and versioned-memory mechanisms. Explanations derive from stored changes and evidence.
- Voice, phone input, HA controls, ITX performance and physical energy outcomes follow this milestone. The first interface is the text playground accepted in the planning discussion.

## Starting evidence, gaps and implementation path

These baseline observations were recorded when the plan was created, before implementation. Current progress is in the acceptance report. Paths are relative to the project root.

| Evidence | Finding | Implementation path |
|---|---|---|
| `moqi/voice.py`: `LocalCorrectionParser` uses rules first, then four Choice questions | Model candidates cover a narrow prohibition grammar; explanatory intents share unnecessary device/action questions | Ask questions appropriate to the identified intent; preserve rule/model provenance and expose uncertainty |
| `moqi/decision.py` and `moqi/understanding.py`: constraint scope is `always` or `night` | Temporary and weekday-scoped corrections are not representable | Extend the memory constraint contract and every applicable decision/safety reader |
| `moqi/playground.py`: parser is constructed without an agent | Current browser interactions do not exercise EdgeJev | Add a local model-backed mode; keep the static sandbox truthful |
| `moqi/api.py`, `moqi/memory_store.py`, `moqi/understanding.py` | Shared SQLite memory, changes and rollback already exist | Put correction application in the Python core and include new memory records in persistence/export/rollback |
| `moqi/understanding.py` fits temperatures from human actions | Spoken preferences need a distinct source and context | Store explicit preferences separately from empirical observations and learned distributions |
| `moqi/core.py` has annotation history; `moqi/memory.py` reconciles attribution | A one-off human exception is still human behaviour; relabelling it as a device action would be wrong | Add an auditable learning-eligibility correction and rebuild the affected learning contribution |
| Current Python module lookup and local model-file check on 2026-10-22 | EdgeJev runtime and `models/jev-int8` artifacts are absent | Complete M0 before claiming real inference |

## Responsibilities and data flow

Input text plus selected household context -> typed interpretation -> deterministic scope/value resolution -> visible proposed memory change -> Apply -> core transaction -> versioned memory -> shadow decision -> evidence-based explanation.

JEV classifies meaning against bounded choices. Code resolves timezone-aware dates, exact numbers, units, entity IDs and memory references. A language-model probability is neither a learned habit probability nor execution permission.

Implementation should have these responsibilities; module names are proposed, not existing entry points:

| Responsibility | Location |
|---|---|
| Rule/model interpretation and provenance | Existing `moqi/voice.py`, with bounded grammar in `moqi/decision.py` or a focused parser module if needed |
| Typed change proposal, resolution, Apply/Cancel/Undo | New focused `moqi/corrections.py`, exposed through `HomeMemory` |
| Durable constraints, explicit preferences, evidence corrections and revision history | Existing memory-store/understanding layer, extended with documented schema changes |
| Filtering candidates and explaining effective restrictions/preferences | Existing decision, memory and safety consumers; no UI-only policy |
| Local browser service and real model loading | New `moqi/playground_server.py`; loopback service calls the same core operations |
| Interpretation, before/after views, clock/context controls and undo | Existing playground assets, with a clearly selected local mode |

Change proposals need stable IDs, kind, entity/target reference, context/scope, proposed value, provenance and application status. Record the resolved interval or recurrence, the affected revision and enough evidence to explain the change. Reuse existing fields where their meaning matches; do not create a second generic memory framework.

Replace our internal use of `choice='Noul'` for an unknown result with explicit parse states such as interpreted, needs clarification and failed. Update the actual playground, benchmark and parser callers together. In upstream Jev, Noul represents a true/false probability; abstention is an application decision.

## Delivery sequence

### M0 — Establish real local inference

Install a reproducible model runtime in an isolated environment, acquire/build the local laya artifacts, and record hashes and runtime/provider details. Inspect the selected runtime's actual artifact contract before relying on the old three-file assumption. Build on the development machine; do not perform model conversion on the ITX PC as a prerequisite.

Run Chinese paraphrases which bypass the rule parser. Show rule, EdgeJev and unavailable states explicitly. Report cold-load time, model-only warm inference latency, full interpretation latency and process peak memory on the measured machine. Published Xeon timings are not ITX measurements.

Acceptance: G0. Missing artifacts, unavailable model downloads or runtime incompatibility are reported as a blocker; fake-agent tests cannot close this stage.

### M1 — Scoped corrections

Support an explicit device and prohibited automatic action with permanent, one-off and weekday scope. Resolve phrases using the household timezone and show the exact result before Apply.

Initial scope contract:

- “以后晚上不要自动关餐厅灯”: recurring restriction in the existing household night window, initially 23:00–07:00.
- “今晚不要自动关餐厅灯”: effective from Apply until the next 07:00 local time. Before 07:00, expiry is that morning; at or after 07:00, expiry is the next morning. The preview shows both timestamps so this convention is visible.
- “只有工作日晚上不要自动关餐厅灯”: the night occurrence is anchored to its start date. Existing explicit holiday/workday overrides determine whether that occurrence is a workday; otherwise Monday–Friday is used.
- Missing device or an unresolved conflicting scope produces clarification, with no Apply-ready change.

Extend current `always`/`night` memory records without changing their meaning. Expiry must be evaluated from stored scope and query time, so it works after restart and does not require a timer to delete the restriction. The new local workflow previews changes before applying them; existing callers keep their documented behaviour unless deliberately migrated.

Acceptance: G1, G4 and the M1 portion of G5–G7. Demonstrate a changed shadow outcome before and after expiry and undo.

### M2 — Contextual preferences

Support explicit temperature preferences for an identified room/device and an explicit activity context, initially working and sleeping. Example: “我工作的时候，书房空调设到23°C更舒服。”

Store the value, unit, context, provenance and revision as an explicit statement, separate from the measured room temperature and the distribution learned from physical adjustments. Activity must come from an explicit sandbox/user context; ordinary motion is not evidence that someone is working or asleep.

An applicable explicit preference takes priority over a learned preference for that context, while prohibitions and existing safety checks still apply. Other contexts remain unchanged. In the shadow view, expose both the learned baseline and the effective preference, including its source. A conflicting new preference previews the record it would replace; Apply supersedes it and Undo restores the prior effective value.

An explicit statement does not count as repeated behavioural evidence or increase earned trust. Initially preferences belong to the configured household room/device and activity; personal identity is not inferred. If no learned baseline exists, show the explicit preference without inventing one.

JEV identifies the preference and context; exact temperature and unit parsing belong in code. Requests with an unclear value/context require clarification.

Acceptance: G2, G4 and the M2 portion of G5–G7. Demonstrate that working changes the shadow temperature target while sleeping/unknown context does not inherit that preference.

### M3 — Correct learned memories through their evidence

Support selecting a recent routine and inspecting its supporting events before submitting “那天来了客人，那次不是平时的习惯。” A statement without a resolvable event/routine reference asks the user to select the relevant evidence.

Apply a learning exception to the selected evidence, retain its original human attribution and raw observation, and recompute the affected routine's contribution and shadow result. Keep the event available for history while excluding the specified exceptional episode from routine training and associated routine scoring. Do not add fabricated failures or infer a new guest routine from one statement.

Also allow a genuine attribution correction, such as selecting an event and stating that an existing automation caused it. Reuse annotation history and reconciliation where appropriate. The UI must distinguish an attribution correction from an exception to normal behaviour.

Undo restores eligibility or the prior attribution and recomputes the affected understanding. Derived memory rebuilds must continue respecting active corrections. When the required evidence is no longer retained, explain what cannot be recomputed instead of claiming a successful repair.

Acceptance: G3, G4 and the M3 portion of G5–G7. Demonstrate a visible change to evidence counts/derived prediction, persistence through consolidation, and recovery through Undo.

### M4 — Complete the user walkthrough and handoff

Provide one launch path from a clean supported development environment, dependency/model setup instructions, a synthetic household, and a guided browser walkthrough covering M1–M3. Loading a saved demo must identify it as a recording; local live mode must identify its real model/backend.

Run the user workflow in the browser, including Apply, Cancel, expiry, context switching, evidence correction, restart and Undo. Record software/model revisions, expected/actual outcomes, observed latency and remaining hardware work. Update the existing README/playground/voice/framework documentation and public-source allowlist as needed so the deliverable can be reviewed without private data.

Acceptance: G5–G8. Preparing a reviewable source change is in scope; publishing or changing the household deployment is a later explicit release action.

## Acceptance criteria: the active goal

All entries start OPEN. A criterion closes only when its stated evidence exists.

| ID | Required user-visible result | Acceptance evidence |
|---|---|---|
| G0 | Real local EdgeJev interprets Chinese paraphrases beyond the bounded rule grammar, with no cloud call after setup | Pinned runtime/model manifest plus actual model results and measured resource report; rule-only success is reported separately |
| G1 | Temporary, recurring-night and weekday corrections affect only the intended device/action and interval | Browser preview/Apply/expiry/Undo walkthrough, including midnight and the 07:00 boundary; matching core behaviour after restart |
| G2 | A stated comfort preference changes the effective shadow target only in its named context, while preserving the empirical baseline | Working versus sleeping/unknown-context walkthrough; conflict replacement, restart and Undo evidence |
| G3 | A selected exception or attribution correction changes the routine's actual evidence contribution without rewriting observations | Before/after evidence and shadow result, consolidation/restart stability, Undo restoration; original event payloads and execution receipts unchanged |
| G4 | Memory changes are explicit, explainable, durable and reversible | Preview/Cancel/Apply/Undo for each kind; repeat Apply does not duplicate effects; a revision history survives restart and integrates with existing local memory export/rollback |
| G5 | The new semantics do not change unrelated household behaviour or introduce device control | Focused regression checks for legacy always/night constraints, existing learning and safety readers, raw-event preservation and static playground; inspect actual call paths |
| G6 | Real language interpretation is useful beyond demonstrations and handles uncertainty honestly | Frozen, separately labelled household-language evaluation, defined below; full-field outcomes and provenance exported, with failures visible |
| G7 | The real browser workflow works against persistent Python memory | Browser interaction evidence for all three stages; refresh/restart preserves applied changes; inaccessible model/server and invalid requests have visible failures and no fabricated success |
| G8 | Another developer can reproduce and review the feature | Tested setup/launch instructions, synthetic walkthrough, model/source revisions and acceptance report; public artifacts contain no household data, credentials or bundled private models |

### G6: interpretation quality, without tuning to the answer sheet

Before tuning question templates, freeze a small household-language evaluation set with expected fields reviewed separately from model output. Keep paraphrase families together so a near-copy used for development does not appear in the holdout. Separate supported/unambiguous requests from genuinely ambiguous or unsupported ones.

Cover scope and negation, the overnight boundary, weekday recurrence, exact values/units, explicit activity, replacement preferences, event/routine references, one-off exceptions, attribution corrections and ambiguous/multi-device requests. Include quoted or cancelled instructions so merely mentioning a command does not create a request to apply it.

Initial software release targets, chosen for this review-first workflow rather than claimed as measured performance:

- At least 90% full-field correctness among model interpretations presented as Apply-ready on supported holdout requests.
- At least 80% of supported unambiguous holdout requests become Apply-ready, preventing success by abstaining on everything. Report the per-workflow breakdown and sample denominators alongside aggregate results.
- No wrong-entity or temporary-to-permanent interpretation in the mandatory acceptance scenarios; no ambiguous/failed interpretation applies a memory change.
- All mandatory scope, persistence, evidence-preservation and Undo scenarios pass end to end. Aggregate language accuracy cannot override a failed core scenario.

Full-field correctness includes kind, entity/target reference, action/value, context and resolved scope. Measure rule-only, model-only and hybrid results separately on the same examples. Probability outputs are reported as model scores; they do not establish household calibration or justify autonomous application. Threshold/template changes after inspecting holdout failures require a fresh holdout for the next acceptance claim.

Latency has no unmeasured ITX pass claim: report actual development-machine cold/warm and end-to-end results with input/option sizes. Show progress during inference and failures clearly. Establish a numerical ITX usability target only after measuring that hardware.

## Goal completion and blocker handling

Complete the active goal only when G0–G8 have evidence and the browser walkthrough is complete. Record criterion status and evidence links in the [acceptance report](acceptance.md) as implementation progresses. If the real model or another required component is unavailable, keep the relevant criteria open and state the concrete blocker; do not replace it with simulated acceptance.

Hardware follow-up: verify the Celeron instruction set/runtime, measure coexistence with HA on its actual RAM, connect real entities read-only, then accept audio and physical execution separately. The original conflict between mandatory safety announcements and night-time silence remains a physical-execution policy decision; it does not prevent this memory-only software work.

## References and relationship to the original plan

- Original intent and language/control division: [complete plan, sections 7–8](../../默契-完整方案.md).
- Existing boundaries: [framework](../../framework.md), [voice acceptance](../../voice-acceptance.md), [playground](../../playground.md).
- [TypeSafe primitives and System One](https://docs.typesafe.ai/concepts/system-one): typed decisions and Noul semantics.
- [TypeSafe documented limitations](https://docs.typesafe.ai/model-jaggedness/jev-1.13): perform arithmetic/time comparison in code. These limits describe that hosted model, not measured EdgeJev behaviour.
- [EdgeJev maintainer repository](https://github.com/yzfly/edgejev): local runtime, model builds and backend/precision tradeoffs. Upstream pages were reviewed on 2026-10-21; recheck concrete version/artifact requirements during M0.

This plan extends language-to-memory capability in Moqi. The original statistical habit-learning and safety responsibilities remain in their existing layers. No paper outcome, Chinese-model accuracy or hardware latency is claimed by this planning document.
