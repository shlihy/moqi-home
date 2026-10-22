# Household-language evaluation cases

`development.jsonl` is for question-template development. `holdout.jsonl` is a frozen acceptance set, authored before the new templates and reviewed as statements with reference fields rather than model answers. Family IDs identify the linguistic constructions; preserve the development/holdout separation.

The supported synthetic entities are `switch.dining_light` (餐厅灯), `switch.fan` (风扇), `climate.study` (书房空调) and `climate.bedroom` (卧室空调). Activity context supports working and sleeping. An explicit `selected_event` is trusted UI selection; the model must not invent one.

Interpretation labels contain semantic scope kinds. Separate deterministic acceptance cases resolve their dates using explicit event time and the household timezone. Entries expecting clarification must never become applied changes.

The initial holdout contains twenty supported requests and ten ambiguity/unsupported cases, covering all three workflows. It is a small software acceptance set, not a household calibration study. Record exact denominators and per-workflow errors with any reported score. Freeze its SHA-256 before template tuning; if tuning uses its failures, retire it from acceptance and create a fresh set as required by the plan.
