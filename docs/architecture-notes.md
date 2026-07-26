# Architecture notes (running log)

Captured during the build, in the order decisions/evidence actually showed up.
This feeds the final 1-2 page architecture doc (Section 12) -- raw reasoning
here, distilled prose there.

## Why the field whitelist must key on field name/position, not value (Milestone 2)

Section 5 specifies a "field whitelist" as part of the validation layer, on
general safety-boundary grounds (the LLM is advisory, Python executes). During
the eppy proof-of-concept, that requirement got a concrete example instead of
just a principle:

An edit script matched the occupied cooling setpoint field by **value**
(`find any field == 23.89C, set it to 25.5C`) rather than by field name. In
`CLGSETP_SCH_NO_OPTIMUM` (a single `Schedule:Compact` object -- confirmed only
one object with that name exists in the IDF), three fields happened to equal
23.89C: the weekday 7:00 occupied setpoint, the weekday 18:00 occupied
setpoint, and the `SummerDesignDay` branch's setpoint. Value-matching edited
all three -- including the design-day sizing schedule, which was never the
intended target and has downstream effects on equipment sizing, not just
operational setpoints.

The failure this demonstrates: **a value-matching edit strategy can silently
reach into a field the caller never intended to touch, just because it shares
a numeric value with the intended field.** Field name/position addressing
(e.g. "Field_8 of object X") doesn't have this failure mode -- it can only
ever touch the exact field it names, regardless of what value currently lives
there. This is the concrete justification for Section 5's whitelist being
defined as (object name, field name) pairs, not (object name, value) pairs --
stronger than "whitelisting is good practice" because there's now a specific
mechanism it closes off.

Practical implication for Milestone 5: the validation/whitelist layer's
allowed-edit list must be expressed as `{object_type, object_name, field_name}`
tuples, resolved via eppy's `fieldnames`/`getattr` by name -- never via a
search-and-replace-by-value pass over an object's fields.

## Chunked re-invocation has a measurable warmup artifact -- baseline and AI run must chunk identically (Milestone 3)

Measured directly: the same Jan 1-31 window, same IDF, same weather file,
gives **3,383.7 kWh** as one continuous EnergyPlus run vs **3,609.5 kWh**
as five sequential 7-day chunks (+6.7%). Confirmed this isn't a warmup
*convergence* failure (`0 Severe Errors` during warmup in every chunk) --
it's structural: each chunk is a separate `energyplus.exe` process, so each
one's warmup re-establishes zone thermal-mass initial conditions from that
week's own repeated conditions, rather than inheriting the state a
continuous run would have evolved to by that point in the month.

This is the concrete cost of the supervisory/episodic architecture Section 0
already chose deliberately -- not a new problem, but it has a specific
consequence for Section 3's baseline methodology that wasn't spelled out
there: **the ~6.7% gap is entirely a chunking artifact, not a policy effect.**
If the baseline run (single continuous run, rule-based schedule) is compared
against the AI-supervised run (chunked, episodic) without accounting for
this, the measured "% savings" would be contaminated by an apples-to-oranges
chunking difference on top of whatever the setpoint policy actually did.

Implication: **the baseline run must use the exact same chunk boundaries as
the AI-supervised run** (same chunk_days, same window), not a single
continuous run, or the two need a documented correction factor. Simplest
fix, and the one to use: generate the baseline by running the rule-based
schedule through the identical chunked-loop machinery, so both arms of the
comparison absorb the same warmup artifact equally and it cancels out of the
delta.

## Hitting the air-temperature setpoint does not guarantee comfort -- PMV also tracks radiant effects (Milestone 4 prep)

After enabling Fanger PMV reporting, the first all-hours average per zone
looked suspiciously cold (avg temp ~19.1C, avg PMV ~-1.10 -- "noticeably
cold" on the 7-point scale) for a building whose occupied heating setpoint is
21.11C. Investigated with real hourly data from `chunk_01` rather than
assuming either "that's fine" or "something's broken":

1. **Not a heating-undersizing bug.** On a normal weekday, `CORE_ZN` air
   temp hits exactly 21.11C by ~10am and holds it precisely through 7pm.
   The setpoint is being tracked correctly.
2. **Most of the "cold" in the all-hours average is unoccupied setback,
   correctly.** The 21.11C setpoint only applies weekdays 7:00-19:00 (~36%
   of the week); the rest sits at the 15.56C setback by design. An all-hours
   average necessarily skews cold -- expected, not a defect, but it means an
   all-hours PMV/temp average is the wrong number to hand to anything trying
   to reason about *occupant-experienced* comfort.
3. **Even restricted to occupied hours, PMV stays notably negative (-0.6 to
   -0.9) while air temp sits exactly at setpoint.** Checked why: Fanger PMV
   depends on mean radiant temperature, not just air temp. At the same hour,
   same near-setpoint air temp, the interior zone (`CORE_ZN`, no exterior
   walls) reads least-cold (-0.66); perimeter zones with exterior walls read
   worse (-0.72 to -0.91), consistent with radiant heat loss to cold
   envelope surfaces in a Colorado January. `PERIMETER_ZN_2` was worst on
   both temp (20.57C, below setpoint) and PMV -- the same zone flagged
   earlier for warmup non-convergence in the Milestone 1 baseline run,
   suggesting it's a genuinely harder-to-control zone in this model, not a
   coincidence.

**Why this matters beyond "interesting physics":** it's a concrete
demonstration of exactly the gap Section 0 frames as the reason PMV-aware
supervision has value over a traditional rule-based BMS -- a schedule that
perfectly hits its air-temperature setpoint can still leave perimeter-zone
occupants uncomfortable, because air-temperature control alone can't see
radiant asymmetry. That's a legitimate Section 9 narrative point, not
marketing: the failure mode PMV-aware AI supervision would need to catch is
observable in this model's own baseline behavior.

**Practical implication, still open:** `average_pmv`/`average_zone_temp_c`
in `metrics.py` currently average across all hours (occupied + unoccupied),
which dilutes the occupant-relevant comfort signal fed to the LLM with ~64%
setback-period hours. Restricting to occupied hours (via `BLDG_OCC_SCH_wo_SB`,
the actual occupancy schedule -- not the heating setpoint schedule's window,
which happens to align here but is a different concept) would give a more
representative comfort number. Not yet implemented -- awaiting a decision on
whether to do this before or after the first live LLM test.

## EnergyPlus CSV quirk: trailing space on the last column's header (Milestone 4 prep)

While spot-checking `PERIMETER_ZN_4`'s PMV column by exact key lookup, hit a
`KeyError` despite the column visibly being in the CSV header. Cause:
EnergyPlus writes a trailing space on whatever column happens to be
**last** in `eplusout.csv` (e.g. `...PMV [](Hourly) ` with a trailing space)
-- not specific to that zone, just whichever column lands last positionally.

`metrics.py`'s actual functions (`average_pmv`, `average_zone_temp_c`) are
unaffected -- they match via substring containment (`suffix in c`) and
`c.split(":", 1)[0]` prefix extraction, neither of which cares about
trailing whitespace at the end of the full header string. But any future
code doing exact-string column lookups against `eplusout.csv` headers will
silently break on whichever column happens to be last. Prefer substring/
prefix matching over exact-key lookups against EnergyPlus CSV headers for
this reason.

## The PMV-sign inversion: the concrete failure case Section 5's validation layer exists to catch (Milestone 4)

This is the most important finding in the build so far -- not because a
small model misreading a sign is surprising, but because it's a
reproducible, real example of exactly the failure category Section 5
argues for defending against, with the model's own backwards reasoning on
record verbatim. Also, not incidentally, this is the "one visible
rejection moment" Section 12 wants for the demo video.

**Setup:** `scripts/test_llm_reasoning.py` run against `llama-3.1-8b-instant`
via Groq (the Section 8 cloud fallback -- used because Ollama's local pull
was blocked on slow CDN throughput, see below), fed real window summaries
built from Milestone 3's actual chunk outputs (`summarize_chunks()`, real
kWh/PMV/temp data, no synthetic inputs). Three independent calls, three
different windows (last-2, last-3, all-5 chunks). All three produced
schema-valid tool calls -- 100% structural success. All three were
*substantively wrong* in the same way:

```
Window: last_2_chunks (chunk_04, chunk_05) -- avg PMV -1.05, -1.21 (both "too cold")
  Proposal: heating_setpoint -> 20.0C  (current occupied setpoint: 21.11C -- COLDER)
  Reasoning (verbatim): "Adjust heating setpoint to reduce energy consumption and
  increase comfort based on recent simulated data showing low PMV values and
  relatively cool occupied-zone temperatures"

Window: last_3_chunks (chunk_03..05) -- avg PMV -0.92, -1.05, -1.21 (all "too cold")
  Proposal: heating_setpoint -> 18.3C  (COLDER)
  Reasoning (verbatim): "The office has experienced cooler than normal recent
  temperatures and higher energy consumption suggesting a potential heating setback."

Window: all_5_chunks -- avg PMV -1.10, -1.03, -0.92, -1.05, -1.21 (all "too cold")
  Proposal: heating_setpoint -> 17.9C  (COLDER, most extreme)
  Reasoning (verbatim): "The energy used has decreased by 59.8% and the comfort
  level has increased with a more negative PMV value in the recent simulated
  chunks. This indicates a potential opportunity for reducing the heating
  setpoint without impacting occupancy comfort, suggesting a temperature decrease."
```

**The error, precisely:** on the Fanger 7-point scale, more-negative PMV
means *colder*, not more comfortable. The third proposal's reasoning states
the opposite ("comfort level has increased with a more negative PMV value")
and uses that inverted premise to justify dropping the setpoint further --
while the zone was already measurably too cold in every single one of these
windows. All three calls moved the heating setpoint in the same wrong
direction, independent of window size -- this isn't a one-off fluke from a
single unlucky call.

**Why range clamping alone doesn't fix this** (discussed with the user
before building Milestone 5): a tighter numeric band only catches "the
number is absurd." An in-range proposal (e.g. 18.5C) built on the exact same
backwards reasoning sails through any range check untouched. Range clamping
and correctness are orthogonal -- clamping bounds the blast radius of a bad
number, it cannot detect that a well-formed, in-range number is wrong for
the stated reason.

**What actually catches this class of failure:** a directional-consistency
check -- if the current window's observed PMV already indicates the zone is
outside comfort bounds in a known direction, any proposal must move the
relevant setpoint in the corrective direction, not away from it. This is
checkable in a few lines against data already present in every window
summary; it requires no semantic understanding of *why* the model was
wrong, only that the proposed *action* is consistent with the observed
*state*. Built in Milestone 5 as a second, independent gate alongside the
range clamp: range clamp catches "absurd," directional-consistency catches
"backwards." The reasoning-text log remains a third line of defense for
subtler cases neither structural check can catch.

**Open question, not blocking:** this test used Groq's `llama-3.1-8b-instant`
(the Section 8 fallback), not the Ollama-hosted Qwen2.5-7B-Instruct primary
path -- Ollama's model pull was measured at ~36 KB/s sustained against its
actual blob host (Cloudflare R2), confirmed via raw `curl` bypassing the
`ollama` client entirely, i.e. a real CDN/network issue, not a client bug.
Once the local pull finishes, rerun against the primary model to learn
whether the PMV-sign confusion is a small-model-in-general failure mode (in
which case the directional check is load-bearing on both provider paths) or
specific to this particular model. Doesn't change Milestone 5's design
either way -- the check is provider-agnostic by construction -- but worth
having the data.