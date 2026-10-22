# JEV memory acceptance status

Updated: 2026-10-22. Contract: [implementation plan](plan.md).

Implementation is in progress. The runtime is installed and scope handling has initial core evidence. Real inference and the complete user workflows remain open.

| Criterion | Status | Evidence / next action |
|---|---|---|
| G0 — Real local inference | OPEN | Isolated `.venv-jev` has EdgeJev 0.4.0 at verified revision `dce469472e0e6eea396256e9bf866082dcb9490b`; ONNX Runtime 1.31.0 exposes CPUExecutionProvider. Pinned model download is running; conversion/inference have not run. |
| G1 — Scoped corrections | OPEN | Added shared temporary/weekday-night resolution to decision and safety readers. Six focused scope/persistence checks pass, including editable export/import and restart. Parser, Apply/Undo and browser acceptance remain open. |
| G2 — Contextual preferences | OPEN | Separate explicit preferences from observed/learned temperatures, then demonstrate their shadow effect. |
| G3 — Evidence corrections | OPEN | Add auditable exception handling and verify the recomputed routine, consolidation and Undo. |
| G4 — Durable Apply/Cancel/Undo | OPEN | Implement shared core operations, revisions and restart/rollback integration. |
| G5 — Existing behaviour preserved | OPEN | Existing voice (9), safety (15), and plan-memory (20) checks passed for the current loader/scope changes. Static browser and remaining workflow regressions have not been checked. |
| G6 — Language quality | OPEN | Frozen [reference set](../../../examples/jev/README.md): 20 supported requests and 10 clarification cases. SHA-256 is recorded in `examples/jev/holdout-manifest.json`; no model evaluation has run. |
| G7 — Browser workflow | OPEN | Implement and exercise local live mode against persistent Python memory. |
| G8 — Reproducible handoff | OPEN | Deliver tested setup, source/model revisions, walkthrough and acceptance evidence. |

Hardware, audio, physical device execution and energy outcomes remain separate acceptance work.

## Reproducible model setup

Prerequisites: Git, uv and Python 3.13 on the development machine. From the repository root:

```sh
uv venv --python 3.13 .venv-jev
uv pip install --python .venv-jev/Scripts/python.exe -r requirements/jev-build-lock.txt
.venv-jev/Scripts/python.exe -m tools.build_jev
```

These are the Windows development paths. Model conversion itself is not accepted yet. The build recipe downloads the source checkpoint at revision `1720e3e3357cfe1e281542e223f8273b0890ca34`, builds laya INT8 through the pinned upstream exporter, verifies installed runtime provenance, and writes hashes plus actual smoke-inference results to `models/jev-int8/moqi-model-manifest.json` after successful conversion.

Current download transport is slow. The primary Hugging Face transfer remains the active source download; alternate direct range downloads failed with connection/idle timeouts. No completed checkpoint, converted model or classification accuracy is claimed.

## Focused verification completed

```sh
python -m unittest discover -s tests -p test_jev_runtime.py -v
python -m unittest discover -s tests -p test_voice.py -v
python -m unittest discover -s tests -p test_constraint_scopes.py -v
python -m unittest discover -s tests -p test_safety.py -v
python -m unittest discover -s tests -p test_plan_memory.py -v
```

Results: 3 model-directory boundary checks, 9 voice-adapter checks, 6 scope checks, 15 safety checks and 20 plan-memory checks passed. These establish the listed loader/core behaviours, not real model inference or browser workflow acceptance.
