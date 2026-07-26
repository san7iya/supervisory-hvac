# Supervisory HVAC AI — Architecture (distilled)

Short-form companion to [architecture-notes.md](architecture-notes.md) (the full running
build log). This is the deliverable-4 report: tool-calling architecture, prompt
engineering strategy, latency management, and handling of lengthy simulation logs.
Numbers and limitations here match the [README](../README.md); nothing below is
softened relative to it.

## 1. Three-tier framing and tool-calling architecture

The system maps onto a standard BMS hierarchy:

- **Field level** — EnergyPlus itself, driven by `eplus_runner.py` (a timeout-guarded
  subprocess wrapper) and `idf_edit.py` (the only module with write authority over the
  IDF, via a `{object_type, object_name, field_name}` whitelist — never value-matching,
  see architecture-notes' Milestone 2 finding). The EnergyPlus install and working
  directory are read from `EPLUS_DIR`/`RUN_DIR` env vars (defaulting to this machine's
  paths), so running on a different machine is an env-var change, not a source edit.
- **Supervisory level** — the episodic loop in `run_ai_supervised_loop.py`: after each
  chunk's EnergyPlus run, `metrics.py` extracts kWh/PMV, `window_summary.py` turns the
  trailing chunks into text, `llm_reasoning.py` asks the LLM to reason and propose (via
  native tool-calling, never free text), and `validation.py` gates the proposal before
  `idf_edit.py` applies it for the *next* chunk.
- **Management level** — the Streamlit dashboard (`app/dashboard.py`) and the
  baseline-vs-AI comparison; nothing at this level has write access to the simulation.

Concrete data flow: **EnergyPlus (chunk N) → metrics.py/window_summary.py →
llm_reasoning.py (reasoning-only, structured JSON via tool-calling) → validation.py
(4 checks) → idf_edit.py (eppy write) → EnergyPlus (chunk N+1)**. The LLM is advisory
only — it can never touch the IDF; `llm_reasoning.py` returns a `Proposal` or
`RejectedProposal` dataclass, nothing else, and never regex-salvages JSON from prose.

**MCP status:** `mcp_server.py` wraps five tools (`get_zone_temps`, `get_energy_kwh`,
`get_pmv_comfort`, `propose_setpoint_adjustment`, `validate_proposal`) as thin
pass-throughs over the functions above — no reimplemented logic. It has been built and
verified with an in-memory client/server round-trip against real evidence data
(`scripts/test_mcp_server.py`). It is **not wired into `run_ai_supervised_loop.py`** —
the orchestration loop still calls the underlying functions directly. This is a
deliberate, not-yet-taken integration step, consistent with every other module in this
build being proven standalone before integration.

## 2. Prompt engineering strategy

The prompt/schema design went through a real failure-driven evolution, not a single pass:

1. **Free-form numeric setpoint (initial design).** The LLM chose an exact °C value
   directly.
2. **Failure observed:** against `llama-3.1-8b-instant`, three independent real calls
   (different trailing windows, real chunk data) all inverted the Fanger PMV sign
   convention — reading more-negative PMV as *more* comfortable — and used that
   backwards premise to push an already-too-cold zone colder. Separately, `llama-3.1-70b`
   runs showed the reasoning text and the numeric proposal disagreeing with each other
   (text arguing one direction, number moving the other).
3. **Prompt-only fix attempted first:** explicit PMV-sign convention text and a worked
   example added to the system prompt. Partial improvement, not a structural fix — an
   in-range, well-formed number built on backwards reasoning still passes a prompt-level
   instruction; nothing forces the model to act on it.
4. **Structural fix (current state):** the tool schema no longer accepts a numeric
   setpoint at all. The model chooses only `direction` (`raise`/`lower`, enum) and
   `magnitude` (`small`/`medium`/`large`, enum); the actual °C delta is computed
   deterministically in code (`MAGNITUDE_DELTA_C`), removing the second independent
   number the model's reasoning text could ever contradict. This closes the bug **by
   construction**, not by better wording — confirmed empirically across 9 real
   proposals under the new schema (5 in the January run, 4 in the July run), with zero
   recurrences of either failure mode since the schema change.

## 3. Latency management

Two independent decisions, at different layers:

- **Episodic/chunked supervisory control, not per-timestep intervention** — the core
  latency-management decision. The LLM is invoked once per multi-day chunk (7 days in
  both evidence runs), not once per simulation timestep, so LLM round-trip latency is
  amortized over days of simulated time rather than gating every timestep. This is also
  why the AI-supervised and baseline runs must chunk identically (documented chunking
  warmup artifact, ~6.7% on this model, unrelated to policy).
- **Retry-with-backoff on transient LLM failures** — `llm_reasoning.py` retries up to
  3 attempts with a fixed 2s/4s backoff, but **only** for failures classified as
  transient (timeouts, connection errors, 5xx). Non-transient failures (4xx bad
  request, malformed tool-call generation, missing API key) fail fast with no retry,
  since retrying a payload/config problem just reproduces the same failure. This
  classification is exposed as `failure_type`
  (`transient_network` / `non_transient_4xx` / `tool_call_malformed` / `config_error` /
  `schema_invalid`) so a request that never reached validation is distinguishable from
  a validation rejection, both in the loop's own log and in the dashboard. Separately,
  EnergyPlus execution failures get their own retry/rollback (up to 3 attempts, revert
  to `last_known_good.idf`) — a field-level concern, unrelated to LLM latency.

## 4. Handling lengthy simulation logs

Two distinct "log" problems, handled by two different modules:

- **EnergyPlus's own output** (`eplusout.csv`, `eplusmtr.csv`) is never fed to the LLM
  raw. `metrics.py` extracts just total facility kWh and average occupied-zone PMV/temp
  per chunk (substring/prefix column matching, deliberately not exact-key lookups, since
  EnergyPlus trailing-spaces whatever column lands last in the CSV header).
  `window_summary.py` turns a trailing window of chunks' extracted metrics into a short
  plain-text summary — this, not the CSV, is what the LLM actually sees. Missing comfort
  data (chunks predating the PMV output additions) is reported as "not available"
  rather than fabricated.
- **The orchestrator's own stdout log** (`run_ai_supervised_loop.py` writes no log file
  itself; it's captured via shell redirection, e.g. `data/run_ai_supervised_70b.log`) is
  parsed by `log_parser.py` using a fixed set of tagged-line regexes
  (`[CHUNK_START]`, `[CHUNK_RESULT]`, `[PROPOSED_ACTION]`, `[VALIDATION]`, …) into
  per-chunk decision records for the dashboard, including the `failure_type`/
  `check_that_decided` tag on rejected or failed proposals. Older logs that predate the
  `failure_type` tag fall back to a reason-text heuristic (`_classify_rejection`) for
  the same classification.

## 5. Validation layer

Four independent checks between proposal and execution, run in this order (direction →
range → deadband → cumulative cap), each real-evidence-backed:

1. **Range clamp** (`SETPOINT_MIN_C`/`MAX_C` = 18–26°C) — catches an absurd number
   regardless of reasoning.
2. **Directional-consistency** — catches a proposal whose direction contradicts the
   observed PMV (e.g. lowering heating while already too cold). This is the check the
   PMV-sign inversion failure (Section 2) directly motivated; it compares
   `proposal.direction` against the PMV state directly, not two numbers.
3. **Deadband** (minimum 1.0°C gap between heating and cooling setpoints) — catches a
   proposal that's fine in isolation but would push the two setpoints too close
   together. **January evidence:** the exact heating/cooling pair the deadband check
   rejects was independently reproduced standalone as a deterministic, fatal EnergyPlus
   `DualSetPointWithDeadBand` error (`data/evidence/crash_reproduction_eplusout.err`) —
   4 of 5 January proposals were rejected here, and the run had zero EnergyPlus crashes
   as a result. **July evidence:** the same check fired again in chunk 4, this time
   triggered from the *cooling-moved* side (cooling had been lowered to 23.39°C in chunk
   2; a later heating-raise proposal would have closed the gap to 23.61°C) — confirming
   the check symmetrically on real, not synthetic, LLM output.
4. **Cumulative-movement cap** (±2.5°C from each field's true baseline) — catches
   unbounded drift across many accepted proposals over time, independent of the
   per-proposal deadband check.

## 6. Results

Two independent real-data runs, presented as complementary evidence, not a single number:

- **January (heating path, single edit):** +3.28% total facility electricity
  (3,609.5 → 3,728.0 kWh), from the one proposal accepted out of five (a heating-setpoint
  raise). Comfort improved on the accepted edit (avg PMV −1.03 → −0.87). The other four
  proposals were all rejected by the deadband check, mapping to the reproducible crash
  above.
- **July (cooling path, blended edits):** +0.70% total facility electricity
  (3,451.1 → 3,475.4 kWh), from three accepted proposals (two heating raises, one
  cooling lower) rather than one — smaller and blended by design, reflecting
  proportionate response to warm-season conditions. Chunk 2 is the first real
  cooling-direction proposal accepted (PMV +0.06, cooling lowered 23.89°C → 23.39°C);
  chunk 4 is the deadband check firing from the cooling-moved side (see Section 5).

## 7. Known limitations (unchanged from README)

- The cumulative-movement cap has never been tripped by real data — both real runs'
  proposals stayed well inside it. Its only evidence is a synthetic test built to
  isolate it from the deadband check.
- No same-chunk retry, by design: a rejected proposal keeps the last valid schedule and
  the loop moves on to the next chunk rather than re-asking the LLM.
- MCP server is built and round-trip tested but **not** wired into the orchestration
  loop.
- Whitelisted edits cover `Core_ZN`, `Perimeter_ZN_1`, `Perimeter_ZN_3`, `Perimeter_ZN_4`
  via two shared schedules; `Perimeter_ZN_2` runs on a separate schedule outside the
  whitelist's scope.