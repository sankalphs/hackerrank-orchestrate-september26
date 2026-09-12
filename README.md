# HackerRank Orchestrate

Starter repository for the **HackerRank Orchestrate** 24-hour hackathon (September 2026).

## Buy or Wait?

Build an AI-powered financial agent that decides whether a user can safely afford a requested expense.

A user may ask: **"Can I afford this laptop?"**

Answering well takes more than the current balance. The agent must account for recurring expenses, pending payments, essential spending, confirmed income, available payment options, and relevant details buried in messages and images.

### Current implementation

Phases 0–5 provide audited loading, evidence-aware ledger construction, recurrence forecasting, baseline capacity metrics, and deterministic candidate generation. Phase 6 adds deterministic explanations, independent output validation, and atomic publication:

```powershell
python code/main.py plan
```

This writes `code/evaluation/candidate_report.json`. It evaluates accepted full, partial, supplied installment, and wait schedules, searches legal recurring-spending changes up to three actions, and records the ranked candidates. The production path is:

```powershell
python code/main.py run --offline
```

or simply `python code/main.py`. It loads cached evidence, builds plans, renders deterministic explanations, independently re-simulates every emitted plan, atomically writes the root `output.csv`, and records the zero-call offline usage report at `code/evaluation/usage_report.md`. Validate an existing submission with:

```powershell
python code/main.py validate output.csv
```

### Phase 7 completion

Phase 7 adds public-sample calibration, release gates, cache-aware optional
ZenMux evidence extraction, usage accounting, and packaging:

```powershell
python code/main.py score-samples
python code/main.py check
python code/main.py package
```

The paid evidence path is opt-in. Copy `.env.example` to `.env` and set
`ZENMUX_API_KEY`; the checked-in defaults select
`meta/muse-spark-1.3-contributor` through ZenMux. Use:

```powershell
python code/main.py run --use-zenmux --max-model-calls 16
```

Only unresolved evidence misses are sent to the model. Results are keyed by
source content hash, prompt version, and model, so a repeat run reuses the
cache and reports zero new model calls. `--offline` never calls the API.
The model extracts facts from messages/images only; affordability, ranking,
and validation decisions remain deterministic Python logic.

For every request, the agent decides whether the user should pay in full, pay partially, use installments, wait, or not proceed. The recommendation must be personalized: two users with the same balance can deserve different answers based on their commitments, priorities, payment preferences, and willingness to adjust flexible expenses.

A recommendation is safe only if the user can complete the full payment plan, cover essential expenses, and stay above their preferred minimum balance throughout the forecast period.

Read [`problem_statement.md`](./problem_statement.md) for the full task spec, input/output schema, allowed values, conflict-resolution rules, and submission format.

---

## Quick Start

Clone the repository and move into the project directory:

```bash
git clone https://github.com/interviewstreet/hackerrank-orchestrate-september26.git
cd hackerrank-orchestrate-september26
```

Build your solution in `code/main.py`, or use another language and document its entry point clearly.

Your solution must:

- Read the input files from `dataset/`
- Generate one prediction for every request
- Write the final predictions to `output.csv` in the repository root

Run the starter Python entry point with:

```bash
python3 code/main.py
```

After running your solution, confirm that `output.csv` exists in the repository root and contains the required columns and one row for every request.

## Important File Locations

```text
dataset/        Input data and the blank output template. Do not modify the input data.
code/           Your solution code.
output.csv      Final generated predictions in the repository root.
code.zip        ZIP file containing your complete solution for submission.
```

The blank template at `dataset/output.csv` is provided as a reference. Your final generated file must be the root-level `output.csv`.

---

## Repository Layout

```text
.
├── AGENTS.md                         # Rules for AI coding tools + transcript logging
├── problem_statement.md              # Full challenge statement
├── README.md                         # You are here
├── code/                             # Your solution code
├── output.csv                        # Final generated predictions
└── dataset/
    ├── requests.csv                  # 250 requests to evaluate — predict these
    ├── output.csv                    # Blank submission template
    ├── sample_requests.csv           # 25 solved examples
    ├── financial_profiles.csv        # Balances, minimum balance, priorities, preferences
    ├── financial_events.csv          # Historical, pending, and confirmed transactions
    ├── request_payment_options.csv   # Payment options available per request
    ├── exchange_rates.csv            # Fixed, dated conversion rates
    ├── messages.csv                  # Messages tied to users, requests, or events
    ├── images.csv                    # Payroll letters, statements, bills, receipts
    └── media/
        └── images/
```

Only `dataset/requests.csv` requires predictions. Everything else is context. Join user records with `user_id`, request records with `request_id`, supporting evidence with `related_event_id`, and exchange rates with the rate date and currency pair.

Amounts are in the user's `home_currency` — the dataset uses INR, ZAR, IDR, USD, and EUR, and every conversion rate you need is in `exchange_rates.csv`. All dates are `YYYY-MM-DD`. Live exchange rates, market data, and banking access are not required.

---

## What You Need to Build

For every row in `dataset/requests.csv`, produce one row in `output.csv` with:

| Column | Meaning |
|---|---|
| `request_id` | The request being answered |
| `amount_safe_to_pay` | Largest amount safe to pay on `request_date` before optional spending changes, after protecting essentials and the minimum balance |
| `affordability_status` | `affordable_now`, `affordable_with_plan`, `affordable_later`, or `not_affordable` |
| `recommended_payment_method` | `full_payment`, `partial_payment`, `installments`, `wait`, or `not_recommended` |
| `payment_plan` | Chronological `<YYYY-MM-DD>:<amount>` entries joined by `\|`, or `none` |
| `earliest_date_for_full_payment` | Earliest date the full amount is forecast safe as one payment; empty if never within the forecast |
| `spending_changes_needed` | Up to three `stop:<event_id>` / `reduce_to:<event_id>:<amount>` changes joined by `\|`, or `none` |
| `decision_explanation` | Short explanation and the financial facts behind it |

`0 <= amount_safe_to_pay <= requested_amount` must always hold. Installment plans must exactly match a supplied payment option, and only recurring expenses marked flexible may be changed.

`affordable_with_plan` means the full request is completed through a partial-payment schedule, installments, or permitted spending changes. Recommend `partial_payment` only when the request allows it, the user accepts it, `0 < amount_safe_to_pay < requested_amount`, and `earliest_date_for_full_payment` is on or before `desired_completion_date`. Use exactly two payments: pay `amount_safe_to_pay` on `request_date`, then pay the remaining amount on `earliest_date_for_full_payment`. The two payments must add up to `requested_amount`. Unlike installments, partial payment does not need to match a supplied payment option.

---

## Suggested Workflow

1. Inspect `dataset/sample_requests.csv` — 25 requests with completed output columns — to understand the expected format and decision style.
2. Reconstruct each user's financial state from `financial_profiles.csv` and `financial_events.csv`: separate recurring expenses from one-time events, reserve pending transactions, count confirmed salary only on its settlement date, and de-duplicate repeated representations of the same event.
3. When an event has a blank `amount`, find its `event_id` as `related_event_id` in `images.csv` and extract the amount from the linked image. Never treat a blank amount as zero. Pull in any other relevant messages, images, and payment options for the request.
4. Forecast forward and generate a plan that keeps the balance above the minimum at every step.
5. Verify deterministically — bounds, plan feasibility, schedule match, flexible-only spending changes — before writing `output.csv`.
6. Score yourself on the solved samples, then run the full dataset.

### Phase 0: input audit

Phase 0 is implemented as a deterministic, dependency-free audit. Run it from
the repository root with:

```bash
python code/main.py audit --strict
```

The command writes the machine-readable report to
`code/evaluation/input_audit.json`. A successful strict run means the CSV
schemas, dates, numeric fields, enums, foreign keys, image mappings, directed
FX coverage, payment schedules, and sample conventions are structurally sound.
Expected unresolved blank event amounts are reported as warnings so they can be
handled by the later evidence pipeline; they are never coerced to zero.

Use `--dataset PATH` and `--output PATH` to audit another dataset location.
The locked financial-engine assumptions used by later phases live in
`code/buy_wait/config.py` and are included in the audit report.

### Phase 1: evidence pipeline

Run the offline evidence pass with:

```bash
python code/main.py evidence
```

This scopes messages, images, and linked event lifecycles per evaluation
request, extracts conservative deterministic facts from templated messages,
validates every fact against the dataset schema, and writes
`code/evaluation/evidence_report.json`. Content-hashed entries are cached in
`code/cache/evidence_cache.json` (ignored by git). Image facts without a
trusted cached extractor remain unresolved with a null amount; they are never
treated as zero. Use `--cache-only` for a cache-only run, or filter with
`--request-id` / `--user-id` while developing.

The JSON-only extraction contracts are documented in
`code/prompts/message_extraction.txt` and `code/prompts/image_extraction.txt`.

### Phase 2: canonical ledger and FX

Build the typed, lifecycle-aware ledger with:

```bash
python code/main.py ledger
```

The command loads profile balances as the request-time opening anchor, follows
`linked_event_id` chains transitively, applies only validated Phase 1 claims,
deduplicates replacement cash effects, and writes
`code/evaluation/ledger_report.json`. The cash-state matrix is centralized in
`code/buy_wait/event_resolution.py`: settled debits/credits are included,
pending debits are reserved, pending credits are excluded, scheduled credits
require confirmation, and failed/cancelled/unrealized/non-cash rows do not
enter cash flow. Foreign amounts use only an exact directed rate on the
settlement date; missing rates fail instead of falling back to an inverse or
nearest rate.

Phase 2 tests can be run with:

```bash
python -m unittest discover -s tests -p "test_*.py"
```

### Phase 3: recurrence and 90-day forecast

Build the deterministic recurring-series and forecast report with:

```bash
python code/main.py forecast
```

`code/buy_wait/recurrence.py` infers a series only from explicit future
records, validated recurrence evidence, or stable observed cadence. It keeps
multiple income streams separate, uses the latest observed amount, preserves
monthly month-end behavior, and does not invent a daily grocery, transport, or
dining drain. `code/buy_wait/forecast.py` is the single simulation authority:
it starts from the profile balance anchor, includes only Phase 2 cash effects,
projects recurring occurrences, converts generated foreign amounts with the
exact directed settlement-date FX rate, and applies required debits before
confirmed credits before proposed payments. Same-day intermediate balances are
checked against the user's minimum, not just end-of-day balances.

The report is written to `code/evaluation/forecast_report.json` and contains
all 250 request horizons, inferred series, daily balances, minimum trough, and
the first violation date. Use `--request-id`, `--user-id`, or
`--horizon-days` for focused development runs. The reusable API is
`build_forecast_context(...)` followed by `simulate(...)`; spending changes
accept `stop:<event_id>` and `reduce_to:<event_id>:<amount>` forms.

### Phase 4: baseline capacity

Build the capacity report with:

```bash
python code/main.py capacity
```

`code/buy_wait/capacity.py` computes `amount_safe_to_pay` and
`earliest_date_for_full_payment` from the unmodified 90-day baseline only.
The arithmetic slack bound is rounded down to the dataset's cent unit and
then verified by re-simulating the proposed payment. Payment preferences and
spending changes are intentionally ignored at this stage. The report is
written to `code/evaluation/capacity_report.json`.

You may use any language or runtime. Python, JavaScript, and TypeScript are all reasonable choices.

### Phase 6: explanations and independent validation

The final writer uses `code/buy_wait/explanations.py` to build a `DecisionTrace`
from the selected plan and simulator, then renders a short explanation without
introducing unsupported facts. `code/buy_wait/validator.py` reads the output
back as CSV and checks the exact header, request coverage, numeric bounds,
status/method rules, baseline capacity fields, legal spending changes, exact
installment schedules, partial-payment arithmetic, and full-horizon simulated
safety. The temporary file is validated before replacement, so a failed run
does not overwrite the last good output.

---

## Requirements

Your solution must:

- be runnable from the terminal
- read the provided files from `dataset/`
- produce a valid `output.csv` with the exact required columns in the exact required order
- include one prediction for every `request_id` in `dataset/requests.csv`
- not use organizer-only files or hardcoded labels
- keep behavior deterministic where possible

If you use API keys or secrets, read them from environment variables. Never hardcode secrets in the repo.

---

## Evaluation

Your `output.csv` will be compared against hidden ground-truth values.

The scoring will consider:

- accuracy of `amount_safe_to_pay`
- correctness of `affordability_status`
- correctness of `recommended_payment_method` and `payment_plan`
- accuracy of `earliest_date_for_full_payment`
- validity of `spending_changes_needed`
- usefulness and consistency of `decision_explanation`

### Token Usage And Cost Analysis

Your `code.zip` must include one token-usage file:

```text
evaluation/usage_report.md
```

The report must cover model providers and names, model calls, input and output tokens, total and average tokens per request, estimated total and per-request cost. The reported values must correspond to the final full-dataset run that produced your `output.csv`.

---

## Chat Transcript Logging

This repo includes an [`AGENTS.md`](./AGENTS.md) file for AI coding tools. It asks compatible tools to append conversation summaries to a `log.txt` in the repository root — the same directory as `AGENTS.md`:

| Platform | Path |
|---|---|
| macOS / Linux | `<repo root>/log.txt` |
| Windows | `<repo root>\log.txt` |

The path resolves relative to `AGENTS.md`, so it stays correct across clones, renames, and checkouts. `log.txt` is gitignored — upload it as your chat transcript at submission time. Do not paste secrets into the chat.

In case, the harness you are using is not in the repo root, you can explicitly ask the agent to look for the AGENTS.md in this folder & then continue.

---

## Submission

Submit the following files as instructed by HackerRank:

| File | Description |
|---|---|
| `code.zip` | Full runnable solution, prompts/configuration, README, and the required `evaluation/` folder |
| `output.csv` | Predictions for every row in `dataset/requests.csv` |
| `chat_transcript` | The `log.txt` described above, showing how you developed or used the system |

Before submitting, confirm:

- `output.csv` has one row per row in `dataset/requests.csv` (250 rows plus the header).
- `output.csv` has the exact required columns in the exact required order.
- Every `amount_safe_to_pay` satisfies `0 <= amount_safe_to_pay <= requested_amount`.
- Every installment plan matches a supplied payment option, and every spending change targets a flexible recurring expense.
- Your runnable code, setup instructions, and `evaluation/` folder are included in `code.zip`.
