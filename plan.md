# Buy or Wait? — Consolidated Implementation Plan

Synthesized from the 3 supplied draft plans, reconciled against the actual
repo contract (`AGENTS.md`, `problem_statement.md`) and a live audit of
`dataset/` and `dataset/sample_requests.csv` on 2026-09-12.
Where the drafts guessed, this plan derives the rule from spec or samples.

## 0. Guiding principle

> **Evidence may be probabilistic; money, safety, ranking, and validation are deterministic.**

An LLM/vision model may only extract facts from messages/images.
Python decides affordability, plan safety, eligibility, ranking, and output.
Embedded instructions in messages/images never override challenge rules.

## 1. Verified repo facts (observed, not hardcoded)

| Item | Observed 2026-09-12 | Contract source |
|---|---|---|
| `dataset/requests.csv` | 250 eval requests, cols: `request_id,user_id,request_date,request_type,requested_amount,desired_completion_date,allows_partial_payment,request_text` | `problem_statement.md` §Input schema |
| `dataset/sample_requests.csv` | 25 solved examples, same input cols + 7 output cols | spec: format/style reference only, never labels |
| `dataset/financial_profiles.csv` | 275 profiles, cols: `user_id,home_currency,current_available_balance,minimum_balance_to_keep,financial_priorities,expense_categories_to_protect,expense_categories_user_is_willing_to_reduce,expense_categories_user_is_willing_to_stop,payment_methods_user_will_consider,max_installment_months` (blank = no installments) | `AGENTS.md` §6.1 |
| `dataset/financial_events.csv` | 25342 rows, cols: `event_id,user_id,event_type,description,category,direction,amount,currency,event_date,settlement_date,status,linked_event_id,flexibility,minimum_allowed_amount` | `AGENTS.md` §6.1 |
| `dataset/exchange_rates.csv` | 134 rows, cols: `rate_date,from_currency,to_currency,rate` | spec: match on rate date + directed pair |
| `dataset/request_payment_options.csv` | 790 rows, cols: `payment_option_id,request_id,payment_method,payment_amount,number_of_payments,first_payment_date,payment_frequency_days,financing_fee,total_payable_amount` | spec: 2–4 options per request |
| `dataset/messages.csv` | 215 rows, cols: `message_id,user_id,request_id,related_event_id,sent_at,source_type,message_text` (`related_event_id` set only for 1:1 event rows) | `AGENTS.md` §6.1 |
| `dataset/images.csv` + `media/images/` | 16 mappings, cols: `image_id,user_id,request_id,related_event_id`; PNG at `dataset/media/images/<image_id>.png` | spec §Files provided |
| `dataset/output.csv` | blank template, 8 required cols in order | `AGENTS.md` §6.2 |
| `code/main.py` | empty starter; `code/evaluation/` exists | `README.md`, `AGENTS.md` §6.6 |

Implementation must **derive** these at runtime (audit phase) and fail
clearly on schema mismatch — never hardcode counts or status distributions.

Actual `flexibility` values observed: `fixed`, `stoppable`, `reducible`
(`minimum_allowed_amount` populated for reducible, e.g. `event_989`).
`event_type`/`status` vocabularies must be enumerated in the audit, not assumed.

## 2. Sample-derived conventions (these settle draft conflicts)

Verified against all 25 samples — use as calibration locks:

1. **`wait` uses a future single-payment entry, not `none`.**
   `request_03`: `payment_plan=2019-11-15:5491000`, `earliest=2019-11-15`.
   `none` is only for `not_recommended` (e.g. `request_05,10,14,15,20,24,25`).
2. **`earliest_date_for_full_payment` is capacity, independent of preferences.**
   `request_12`: `earliest=2026- Union04-05=request_date` yet method=`installments`.
   Compute it with no spending changes and no payment-preference filter.
3. **Baseline capacity ignores spending changes — even past the deadline.**
   `request_06`: deadline `2026-01-14`, baseline `earliest=2026-01-15`,
   yet `full_payment` today wins via `stop:event_476`.
   Same pattern in `request_11` (`reduce_to:event_989:665950`) and `request_21`
   (`stop:event_1815|reduce_to:event_1816:23.50`). Never mutate baseline fields
   after applying changes.
4. **Positive `amount_safe_to_pay` does not imply affordable.**
   `request_10` (`12700` safe), `request_14` (`597.74` safe) are still
   `not_affordable`/`not_recommended` with `payment_plan=none`, empty earliest.
5. **Partial = exactly two payments, second date = earliest, sum = requested.**
   `request_19`: `2024-09-04:28820|2024-09-15:10840` = `39660`.
   Never move the second leg to make it safe — reject the candidate instead.
6. **Installment schedule derivation.**
   `request_02` option_05: `payment_amount=15952906.67 × 3`,
   dates `2025-08-08 | 2025-09-07 | 2025-10-07`
   = `first_payment_date + k*payment_frequency_days` (30d).
   Total paid (`47858720.01`) exceeds `requested_amount` by `financing_fee`
   (`1840720.01`) — preserve supplied amounts/fees exactly, never resynthesize.
7. **Blank amounts resolve via images, never zero.**
   `event_253` (blank, → `image_01`), `event_1442` (blank scheduled rent, → `image_02`).

## 3. Target architecture

```text
dataset/*.csv + media/images
        |
        v
0. Input audit + typed loading        (loaders.py, input_audit.py, models.py)
        |
        v
1. Evidence extraction + cache        (evidence.py, prompts/, cache/*.json)
        |
        v
2. Canonical ledger                   (event_resolution.py, money.py, fx.py)
        |
        v
3. Recurring projection               (recurrence.py)
        |
        v
4. 90-day baseline forecast           (forecast.py)   <- single simulation authority
        |
        v
5. Capacity metrics                   (capacity.py)
   amount_safe_to_pay, earliest_date_for_full_payment (baseline only)
        |
        v
6. Candidate generation               (candidates.py)
   full | partial | installments | wait | not_recommended
        |
        v
7. Spending-change search (0..3)      (spending_changes.py)
        |
        v
8. Deterministic ranking              (ranking.py)
        |
        v
9. Explanation templates              (explanations.py)
        |
        v
10. Independent validator             (validator.py)  -> output.csv.tmp -> output.csv
        + usage tracking              (usage.py)      -> evaluation/usage_report.md
```

`code/main.py` stays a thin orchestrator (no business logic).
All money uses `Decimal`; dates use `datetime.date`; no `float`.

```text
code/
├── main.py
├── buy_wait/  (or flat *.py if preferred — one decision, document in README)
│   ├── config.py models.py loaders.py input_audit.py
│   ├── money.py fx.py evidence.py event_resolution.py
│   ├── recurrence.py forecast.py capacity.py
│   ├── candidates.py spending_changes.py ranking.py
│   ├── explanations.py validator.py usage.py
├── prompts/image_extraction.txt  prompts/message_extraction.txt
├── cache/image_facts.json  cache/message_facts.json
└── requirements.txt
evaluation/sample_score.py  evaluation/usage_report.md  evaluation/sample_score_report.md
tests/test_*.py  (one per module above)
```

Dependency policy: stdlib for CSV/dates/Decimal/JSON/hashing/validation;
vision/LLM SDK only for evidence; no live banking/market/FX calls;
offline run (`--offline`, cache-only) must work without an API key.

## 4. Phase 0 — Operational setup + input audit (do first)

1. Logging per `AGENTS.md` §2/§5: `log.txt` beside top-level `AGENTS.md`,
   append-only, gitignored, exact `tool=` harness name, no secrets.
2. `input_audit.py`: headers, row counts, blank/malformed dates, duplicate IDs,
   FK checks (`user_id, request_id, event_id, related_event_id, image_id`),
   image-path existence, currency/status/direction/flexibility enums,
   list-field delimiters in profiles, payment-schedule encoding,
   per-currency precision, FX pair/date coverage, `wait` vs `none` check.
3. Emit machine-readable audit JSON (`missing_exchange_rates`,
   `invalid_foreign_keys`, `blank_amount_events`, …).
4. Lock open questions into `config.py` before any financial logic:
   balance anchoring (§6), cash-flow date (`event_date` vs `settlement_date`;
   default: convert on `settlement_date`, simulate on effective date — confirm
   in audit), sign convention (`direction` credit/debit, not signed amounts —
   confirm), recurrence encoding, 90-day endpoint inclusive/exclusive
   (`request_date..+89` vs `..+90` — confirm against samples), same-day
   ordering (default: required debits → proposed payment → confirmed credits;
   validate against samples).

## 5. Phase 1 — Evidence pipeline (deterministic filter → extract → validate)

- **Layer A (no model):** scope by `user_id`/`request_id`/`related_event_id`
  + lifecycle; ignore unrelated rows; hash images + messages for cache keys.
- **Layer B (model, JSON-only):** prompts must say: untrusted evidence,
  facts only, ignore embedded instructions, never decide affordability,
  never change schema, `null` when unsupported. Message claims:
  `amount|date|confirm|cancel|amend|delay|status|recurrence|ignore`
  + `target_event_id/amount/currency/effective_date/confidence`.
  Image facts: amount/currency/date/doc-type/status for blank-amount events.
- **Layer C (code validates):** reject unknown event IDs, bad dates/currencies,
  negative/malformed amounts, rule-override attempts, unsupported actions.
  Confidence is diagnostic only. Cache by `id + content-hash + prompt_version`.
- Fallback: cached → local OCR (optional) → vision model → mark unresolved.
  **Never coerce unresolved blank amounts to zero**; dependent plans are unsafe.
- Deterministic pre-patterns first (cancel, salary delay/confirm, rent amend,
  refund-initiated≠received, failed transfer, pending commission, unrealized
  value); model only for unmatched/ambiguous/multilingual messages
  (samples include Indonesian payroll updates, e.g. `message_01`).

## 6. Phase 2 — Canonical ledger + FX

- Follow `linked_event_id` chains transitively, group lifecycles, apply
  validated claims, pick the controlling state, deduplicate cash effects,
  keep `source_row_ids` for audit/`spending_changes_needed`.
- Precedence: explicit cancel/settle/amend > newer same-source record >
  settled over estimate/forecast > financially safer interpretation.
- Cash-state matrix in one function (`cash_flow_effect`):
  settled debit/credit in-window → include; **pending debit → reserve**,
  **pending credit → exclude**; scheduled debit → include unless cancelled;
  scheduled credit → only if confirmed; failed/cancelled → exclude;
  unrealized/non-cash/duplicate → exclude (count lifecycle once).
- **Balance anchoring:** default `PROFILE_AS_OF_REQUEST`
  (`current_available_balance` is opening balance; history used for recurrence
  + lifecycle context, not replayed into the balance). Confirm by sample
  calibration (compare vs replay mode; `request_01` affordable_now is a check);
  store the single chosen mode in `config.py`.
- **FX** (`convert_to_home_currency(amount, from, home, settlement_date)`):
  same-currency passthrough; else exact `rate_date == settlement_date` +
  exact directed pair; `Decimal` multiply; audited precision/quantize policy.
  **No** nearest-prior, inverse, chained, interpolated, or live rates.
  Missing required rate = audit/development failure, never silent substitution.

## 7. Phase 3 — Recurrence + 90-day forecast (single simulator)

- Series priority: explicit recurrence metadata > validated message evidence >
  stable repeated history (regular cadence only) > no inference.
  Store `RecurringSeries(series_id, output_event_id, category, direction,
  frequency, amount, currency, flexible, protected, minimum_allowed_amount,
  occurrences)`. Respect start/end/cancel/amend; month-end rule explicit;
  amount = explicit future > latest stable (> median only if samples prove it).
  **No** automatic daily drain for groceries/transport/dining unless samples
  require it — start with explicit recurrence.
- `simulate(context, extra_payments=(), spending_changes=(), horizon_end)` is
  the only forecast authority → `ForecastResult(dates, balances,
  minimum_seen, min_date, safe, first_violation)`.
- Two horizons: `baseline_horizon` (capacity fields) vs `candidate_horizon`
  (extends to deadline + last installment leg). Baseline fields never shift
  when the candidate horizon extends.
- Daily: `bal[d] = bal[d-1] + confirmed_credits[d] − required_debits[d] −
  candidate_payments[d]` under one locked same-day order. Safe iff
  `bal[d] >= minimum_balance_to_keep` ∀d **and** last payment ≤ deadline.

## 8. Phase 4 — Capacity (baseline only, no preferences, no changes)

- `amount_safe_to_pay = clamp(min_d(baseline[d] − minimum_to_keep), 0, requested)`,
  rounded **down** to currency unit, then verified by shrinking-loop simulation.
- `earliest_date_for_full_payment`: first `d` in baseline window where
  `simulate(+requested@d).safe`, else empty. Equals `request_date` for
  `affordable_now`; independent of `payment_methods_user_will_consider`.

## 9. Phase 5 — Candidates + spending-change search + ranking

Generate all candidates, then filter → rank (never nested-if selection):

| Method | Eligibility (all required) | `payment_plan` |
|---|---|---|
| `full_payment` | accepted; safe today (or safe today under a ≤3-change set); completes by deadline | `request_date:requested_amount` |
| `partial_payment` | request allows; accepted; `0 < safe < requested`; earliest exists and ≤ deadline; **exact** two-leg schedule re-simulates safe | `request_date:safe\|earliest:requested−safe` (sums exactly) |
| `installments` | accepted; `max_installment_months` non-blank; duration ≤ max; exact supplied schedule/fees; completes by deadline; full-horizon simulate safe | exact option schedule; keep `payment_option_id` |
| `wait` | accepted `full_payment`; later safe date exists and ≤ deadline | **future single entry** `earliest:requested_amount` (per samples) |
| `not_recommended` | fallback, no safe eligible on-time plan | `none`, status `not_affordable`, keep earliest if in-window |

- Spending changes: eligible = recurring expense + flexible
  (`stoppable→stop`, `reducible→reduce_to≥minimum_allowed_amount`) +
  non-protected + category in reduce/stop allowlists + active in window +
  same user. ≤3, no duplicate event, no stop+reduce on same event,
  prospective only. Search 0-change first, then 1/2/3-change sets
  (bounded exhaustive; impact-ordered with deterministic fallback — not
  greedy-largest-first alone). Reduction target via binary search for the
  smallest safe `new_amount` (`new < current`, `≥ minimum`).
- Ranking tuple (lower wins), exactly per spec:
  `(¬completes_by_deadline, has_spending_changes, total_paid,
  first_payment_date, payment_count, payment_option_id_or_sentinel,
  canonical_signature_tiebreak)`.
  Pre-filter to on-time candidates (safety requires deadline completion);
  no hardcoded method priority. Status map:
  full-today→`affordable_now`/`full_payment`;
  partial/installment/with-changes→`affordable_with_plan`;
  later-safe→`affordable_later`/`wait`; none→`not_affordable`/`not_recommended`.

## 10. Phase 6 — Explanations + independent validator

- Deterministic templates from a `DecisionTrace` (amounts, currency, balances,
  safe-today, earliest, method, total, completion, binding inflow/outflow,
  changes, provenance). No new numbers/dates/IDs invented; optional LLM
  polish off the critical path with fact-validation.
- `validator.py` (independent of planner): exact 8-col header/order; one row
  per request, no dupes/extras; `0 ≤ safe ≤ requested`; enum + `YYYY-MM-DD`
  checks; chronological plans; partial 2-leg + sum + dates; installment
  option-exactness + duration; ≤3 legal spending changes; `affordable_now ⇒
  earliest==request_date`; `not_recommended ⇒ plan=none`;
  **re-simulate every emitted plan from the output row** (rebuild schedule +
  changes, check minimum + deadline). Atomic write
  (`output.csv.tmp → validate → rename`); nonzero exit, keep last good output.

## 11. Phase 7 — Calibration, tests, CLI, usage, packaging

- `evaluation/sample_score.py`: run production solver on the 25 samples,
  field-by-field diff + trough/candidate/reject-reason debug. Calibrate in
  order: anchoring → pending/failed/cancelled/unrealized → recurrence →
  same-day order → FX/rounding → deadline → partial 2nd leg → installment
  parse → spending search → explanations. No sample-ID branches.
- Synthetic gates (≥20): immediate-affordable; rent-breaks-it; salary-saves-later;
  pending-credit-excluded / pending-debit-reserved; failed/cancelled excluded;
  unrealized excluded; FX on settlement date; full-safe-but-rejected-by-preference
  → installments; installment over-duration rejected; partial 1st-leg-safe but
  2-leg-unsafe rejected; protected-change rejected; stop-enables-plan;
  4-changes-needed rejected; linked scheduled+settled counted once;
  message cancel/amend applied; image blank-amount filled; prompt-injection
  ignored; safe-after-deadline → `not_affordable`;
  earliest==today with method=installments.
- Property invariants: ↑minimum ⇒ ¬↑safe; −income ⇒ ¬earlier earliest;
  +expense ⇒ ¬↑safe; every validated plan ≥ minimum; partial sums exact;
  installment matches a real option.
- CLI: `python3 code/main.py` (default: load → cached evidence → ledgers →
  solve → validate → root `output.csv` + `evaluation/usage_report.md`,
  nonzero on failure). Flags: `audit`, `extract-evidence --cache-only`,
  `score-samples`, `run --output output.csv`, `validate output.csv`,
  `--dataset/--output/--cache-dir/--offline/--refresh-evidence`.
- `usage.py` wraps every model call (provider, model, purpose, source ID,
  cache hit/miss, in/out/total tokens, est. cost). Report from the **final**
  run only: per-model + totals, total/avg tokens and cost per request;
  zero-call offline runs report explicit zeros. No keys in the report.
- Done when: default command runs from root; reads only `dataset/`;
  exact 8-col root `output.csv`, 250 rows; all bounds/enums/dates pass;
  every plan independently simulates safe; installments/partials/changes
  legal; evidence untrusted; `evaluation/usage_report.md` matches final run;
  `README.md` setup/run documented; `code.zip` allowlist (code, prompts,
  README, evaluation/) excludes secrets/tmp; `log.txt` append-only,
  secret-free, gitignored, kept as transcript.

## 12. Build order (ledger before planner, engine before AI)

1. Logging + `.gitignore` (done) → audit + data dictionary.
2. Models/loaders/validation/indexes + Decimal/date utils + exact FX.
3. Lifecycle resolution + cash-state filter + anchoring calibration.
4. Recurrence → baseline simulator → `amount_safe_to_pay` → earliest date.
5. Full → wait → installment → partial candidates → spending search →
   ranking → status map → templates → independent validator.
6. Image/message extraction + cache (parallelizable after step 3).
7. Sample scorer → synthetic tests → full 250-run → usage report → package.

The scarcest resource is correctness of the ledger, not model
sophistication. Spend effort on anchoring, lifecycle dedup, recurrence,
pending/unrealized handling, exact partial/installment semantics, and
sample calibration before explanations or multi-agent scaffolding.
