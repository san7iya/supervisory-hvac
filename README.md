# Supervisory HVAC AI

A supervisory (not real-time) LLM-based optimization layer over EnergyPlus building
simulations. The loop runs in chunks: EnergyPlus simulates a window of days, an LLM
reasons over the resulting energy/comfort trend and proposes a setpoint adjustment,
and a separate Python validation layer -- range clamp, directional-consistency,
deadband, and cumulative-movement checks -- decides whether that proposal is actually
applied before the next chunk runs. The LLM never touches the IDF directly; it can
only produce a structured proposal that the validation boundary either passes through
or rejects. See [docs/architecture-notes.md](docs/architecture-notes.md) for the full
build log and the reasoning behind each design decision (this README doesn't
duplicate it).

## Setup

**EnergyPlus.** Version 25.1.0. Scripts default to `D:\EnergyPlusV25-1-0`
(`EPLUS_DIR`) for the install and `D:\Honeywell-env\run1\` (`RUN_DIR`) for the
working directory -- containing `baseline.idf` and `weather.epw` before
anything is run. Both default from the `EPLUS_DIR`/`RUN_DIR` environment
variables if set, so a different machine just needs those exported, not a
source edit; the defaults above apply if you leave them unset.

**Python.** 3.10. Install dependencies from `requirements.txt`:

```
pip install -r requirements.txt
```

**LLM provider.** Set via the `LLM_PROVIDER` env var:
- `ollama` (default) -- expects a local Ollama server at `http://localhost:11434`
  with `qwen2.5:7b-instruct-q4_K_M` pulled.
- `groq` -- cloud fallback, requires `GROQ_API_KEY`. Model defaults to
  `llama-3.1-8b-instant`, overridable via `GROQ_MODEL` (the run behind the current
  results used `GROQ_MODEL=llama-3.3-70b-versatile`).

**.env.** No `.env.example` exists yet -- it should be added. Currently the only
variable read from `.env` (via `src/supervisory_hvac/env.py`'s minimal loader) is:

```
GROQ_API_KEY=...
```

## How to run

Commands, in the order the build actually happened:

```bash
# 1. Baseline: unmodified rule-based schedule, through the same chunking
#    machinery as the AI loop (so both arms absorb the same chunk-boundary
#    warmup artifact -- see docs/architecture-notes.md).
python scripts/run_chunked_baseline.py

# 2. AI-supervised loop. Choose a provider:
export LLM_PROVIDER=groq
export GROQ_API_KEY=...        # or rely on .env
export GROQ_MODEL=llama-3.3-70b-versatile   # optional override
python scripts/run_ai_supervised_loop.py > data/run_ai_supervised_70b.log 2>&1

# 3. Dashboard
streamlit run app/dashboard.py

# 4. MCP server round-trip test (optional -- proves the MCP wrapper against
#    real evidence data; not wired into the orchestration loop above)
LLM_PROVIDER=groq PYTHONPATH=src python scripts/test_mcp_server.py
```

Two things worth knowing before step 3 surprises you:

- Both scripts write their chunk outputs under `RUN_DIR` (e.g.
  `D:\Honeywell-env\run1\chunks_baseline`, `...\chunks_ai_supervised`), wiping and
  recreating that directory on every run. The dashboard does **not** read those
  live directories -- it reads the committed snapshots under
  `data/evidence/chunks_baseline` and `data/evidence/chunks_ai_supervised`, so a
  fresh run's results won't show up until those directories are copied over.
- Logging is stdout-only by design -- `run_ai_supervised_loop.py` writes no log
  file itself. The dashboard's decision log is parsed from
  `data/run_ai_supervised_70b.log`, which only exists because that run's stdout
  was redirected to it. Redirect stdout the same way if you want the dashboard to
  reflect a new run.

## Project structure

```
src/supervisory_hvac/   Core library
  chunker.py             date-range chunking + RunPeriod field edits
  eplus_runner.py         subprocess wrapper around energyplus.exe
  idf_edit.py            whitelisted (object, field) setpoint writes -- only module that touches the IDF
  llm_reasoning.py       LLM proposal via native tool-calling (Ollama / Groq); classifies a
                          failed proposal's failure_type (transient_network, non_transient_4xx,
                          tool_call_malformed, config_error, schema_invalid) so a request that
                          never reached validation is distinguishable from a validation rejection
  validation.py          the 4-check guardrail layer between proposal and execution
  metrics.py             kWh / PMV extraction from EnergyPlus CSV output
  window_summary.py      builds the trailing-chunk text summary fed to the LLM
  log_parser.py          parses run_ai_supervised_loop.py's stdout log for the dashboard,
                          including the failure_type tag on rejected/failed proposals
  env.py                 minimal .env loader
  mcp_server.py          MCP server exposing 5 core tools (get_zone_temps, get_energy_kwh,
                          get_pmv_comfort, propose_setpoint_adjustment, validate_proposal) as
                          thin wrappers over the functions above; not wired into the
                          orchestration loop

scripts/                Entry points and one-off proof scripts
  run_chunked_baseline.py   generates the baseline comparison series
  run_ai_supervised_loop.py the main loop (chunk -> reason -> validate -> apply)
  run_chunked_loop.py       earlier no-AI plumbing proof, superseded by the above
  add_comfort_outputs.py    adds PMV Output:Variable requests to an IDF
  test_mcp_server.py        in-memory MCP client/server round-trip test, verified against
                             real evidence data
  test_*.py                 manual verification scripts (not pytest), run directly

app/
  dashboard.py            Streamlit results dashboard (baseline vs AI, decision log)

data/
  evidence/                committed CSV snapshots the dashboard actually reads
  run_ai_supervised_70b.log   real captured stdout from the documented run
  decision_log_70b.json       earlier hand-transcription; kept for provenance, not read by the dashboard

docs/
  architecture-notes.md   running build log (decisions + evidence, in the order they showed up)
```

## Current results

From the run recorded in `data/run_ai_supervised_70b.log` (`llama-3.3-70b-versatile`
via Groq, 5 chunks of 7 days each, Jan 1-31):

- **Total facility electricity: +3.28%** (3,609.5 kWh baseline -> 3,728.0 kWh
  AI-supervised). The only accepted proposal raised the heating setpoint, so this
  is the full cost of that one edit, not a blend of several.
- **Comfort delta on the accepted edit: +0.16 PMV** (-1.03 -> -0.87, chunk 2, the
  first chunk the accepted change took effect on). Less negative = warmer = closer
  to neutral comfort.
- **4 of 5 proposals were rejected**, all four by the deadband check, all four for
  the same reason: the proposed heating setpoint would have landed within the
  minimum 1.0C deadband of the (untouched) cooling setpoint -- the exact setpoint
  pair independently reproduced to trigger a deterministic EnergyPlus
  `DualSetPointWithDeadBand` fatal error (`data/evidence/crash_reproduction_eplusout.err`).
  This run had zero EnergyPlus crashes because the check caught it before
  EnergyPlus ever ran on that proposal.

These are energy-cost and comfort-improvement numbers together, not a pure savings
result -- the accepted edit traded electricity for comfort, and the validation layer
is what stopped four other edits from crashing the simulation outright.

## Known limitations

- **Only the heating_setpoint path is validated end-to-end against a real run.**
  The whitelist and validation code handle `cooling_setpoint` symmetrically, but
  in every real run so far the model has only ever proposed raising the heating
  setpoint -- the cooling path has only been exercised by synthetic test cases
  (`scripts/test_validation_deadband_cap.py`), not real LLM output.
- **The cumulative-movement cap (±2.5C) has never been tripped by real data.** No
  real run has moved a field far enough from baseline to approach it; the only
  evidence it works is the synthetic test case built to isolate it from the
  deadband check.
- **No same-chunk retry, by design.** If a proposal is rejected (schema-invalid,
  wrong direction, deadband, or cap), the loop keeps the last valid schedule and
  moves on to the next chunk -- it does not ask the LLM again within the same
  chunk.
- **Whitelisted edits are scoped to two shared schedules**
  (`HTGSETP_SCH_NO_OPTIMUM`, `CLGSETP_SCH_NO_OPTIMUM`), covering `Core_ZN`,
  `Perimeter_ZN_1`, `Perimeter_ZN_3`, and `Perimeter_ZN_4`; `Perimeter_ZN_2` runs
  on a separate schedule not in scope.
- **No `.env.example` in the repo yet** -- see Setup above for the one variable
  it would need to document (`GROQ_API_KEY`).

## Links

- Architecture doc: [docs/architecture-notes.md](docs/architecture-notes.md) (running
  build log; a distilled 1-2 page architecture doc is referenced in it as a planned
  Section 12 output -- **TODO**, not yet a separate file)
- Dashboard: run locally via `streamlit run app/dashboard.py` (**TODO**: hosted link,
  if any)
- Demo video: **TODO**