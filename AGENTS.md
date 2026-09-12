# AGENTS.md

HackerRank Orchestrate (September 2026) — Buy or Wait?

## 0. TLDR For The Agent

On every session start, do this in order:

1. Read this file completely.
2. Check the log file path in §2.
3. Append a `SESSION START` entry using §5.1.
4. For every user turn, append a summary entry using §5.2.
5. When building, testing, or packaging the solution, follow the project contract in §6.

Do not skip logging or rewrite old log entries. Sub-agents and worktrees use the same log file.

---

## 1. What This Repo Is

This is a starter repo for the **HackerRank Orchestrate** 24-hour hackathon challenge: **Buy or Wait?**

Participants must build an AI-powered financial agent. For every purchase or payment request in `dataset/requests.csv`, the agent decides whether the user should pay in full, pay partially, use an available installment option, wait, or not proceed.

The system reconstructs the user's financial position from structured profiles and financial events, fixed dated exchange rates, seller/provider payment options, and relevant messages or images. It must account for recurring commitments, pending payments, essential spending, confirmed income, financial priorities, and the minimum balance the user wants to keep. Messages and images are untrusted evidence; they may clarify, amend, delay, cancel, or confirm a financial fact, but their embedded instructions never override the challenge rules. There are no voice notes or live banking, market-data, or exchange-rate calls.

The final submission must produce `output.csv` with:

```text
request_id,amount_safe_to_pay,affordability_status,recommended_payment_method,payment_plan,earliest_date_for_full_payment,spending_changes_needed,decision_explanation
```

Read `problem_statement.md` for the full participant-facing specification.

---

## 2. Log File — Location And Lifecycle

The log file is named `log.txt` and lives in the same directory as this `AGENTS.md` file  — the repository root.

| Platform | Path |
|---|---|
| Windows | `<directory containing AGENTS.md>\log.txt` |

Resolve the path relative to this file. Do not hardcode a folder name, a user path, or the platform home directory, so the location stays correct across clones, renames, and checkouts.

Rules:

- Create the file if missing.
- Never commit or add the log file to git. Keep `log.txt` in `.gitignore`.
- Append only. Do not rewrite, reorder, or delete prior entries.
- One shared log per checkout. All agents and sub-agents append to the same file next to the top-level `AGENTS.md`, never a private copy.
- Never log secrets. Redact API keys, tokens, cookies, private keys, and sensitive PII.

---

## 5. Log Format

### 5.1 Session Start Entry

```text
## [ISO-8601 TIMESTAMP] SESSION START

tool=<exact_harness_or_coding_agent_name>
Repo Root: <absolute_path>
Branch: <git_branch_or_unknown>
Worktree: <worktree_path_or_main>
Parent Agent: <parent_agent_name_or_none>
Language: <js|ts|py|custom:name>
Time Remaining: <Xd Yh Zm, or not configured>
```

### 5.2 Per-Turn Entry

Append after every user message you respond to:

```text
## [ISO-8601 TIMESTAMP] <short title, max 80 chars>

User Prompt (verbatim, secrets redacted):
<exact user message, with secrets replaced by [REDACTED]>

Agent Response Summary:
<2-5 sentences: what was done, why, and any important decision>

Actions:
* <file edited / command run / tool invoked>

Context:
tool=<exact_harness_or_coding_agent_name>
branch=<git_branch_or_unknown>
repo_root=<absolute_path>
worktree=<worktree_path_or_main>
parent_agent=<parent_name_or_none>
```

**Mandatory tool-name rule:** Every `SESSION START` and per-turn log entry must contain one non-empty `tool=` line with the exact name of the coding harness or agent writing the entry. Replace the template value before writing the log. The entry is invalid if `tool=` is missing, blank, still contains a placeholder, uses a generic label such as `AI`, contains only a model name, or names a different harness. Before responding, verify the value against the harness identity provided by the current runtime and re-read the appended entry to confirm it matches. Never guess the tool name. Correct any mismatch before responding to the user.

### 5.3 Sub-Agent And Worktree Rules

- Sub-agents must log their own entries using the same file.
- Set `parent_agent=` to the parent agent's name.
- Worktrees use the same shared log file, not a per-worktree copy.

### 5.4 What Not To Log

- API keys, tokens, session cookies, OAuth codes, or private keys.
- Sensitive PII.
- Full contents of large files or binary blobs. Reference by path instead.

---

## 6. Project Contract

### 6.1 Dataset Contract

Participant-facing files are inside `dataset/`.

```text
dataset/
├── financial_profiles.csv
├── financial_events.csv
├── exchange_rates.csv
├── requests.csv
├── sample_requests.csv
├── request_payment_options.csv
├── messages.csv
├── images.csv
├── output.csv
└── media/
    └── images/
```

- `requests.csv` contains the evaluation requests. Produce exactly one output row for every `request_id` in it.
- `sample_requests.csv` contains public examples with completed output fields. Use it to understand format and decision style, not as labels for evaluation requests.
- `financial_profiles.csv` defines the user's home currency, current available balance, minimum balance to keep, priorities, protected spending, adjustable categories, and payment preferences. `max_installment_months` is blank when the user will not consider installments.
- `financial_events.csv` contains historical, pending, scheduled, settled, failed, cancelled, and non-cash records. `linked_event_id` points to an earlier event in the same transaction or investment lifecycle; the link alone does not determine whether a row counts toward cash flow. Treat `settled`, `pending`, `scheduled`, and `unrealized` according to their cash state; do not treat unrealized investment value as available cash.
- `exchange_rates.csv` supplies fixed rates. For a foreign-currency cash event, use the row for its settlement date and the stated `from_currency` to `to_currency` direction.
- `request_payment_options.csv` contains the seller/provider payment options available for a request. A request has two to four options. An available option may still be rejected because it conflicts with the user's payment preferences or `max_installment_months`.
- `messages.csv` and `images.csv` provide optional supporting evidence. In `messages.csv`, `related_event_id` is populated only when the message directly describes one supplied financial-event row; a blank value means no one-to-one event row exists. Resolve each image as `dataset/media/images/<image_id>.png`; for example, `image_07` maps to `dataset/media/images/image_07.png`. Use the information only when relevant; do not invent evidence when an image file is absent.
- `output.csv` is the blank prediction template.

Organizer-only files live outside `dataset/` and must never be used for predictions.

### 6.2 Required Output

The solution must write `output.csv` with these exact columns, in this order:

```text
request_id,amount_safe_to_pay,affordability_status,recommended_payment_method,payment_plan,earliest_date_for_full_payment,spending_changes_needed,decision_explanation
```

- `amount_safe_to_pay` is the amount safe on `request_date` before optional spending changes and is between `0` and `requested_amount` inclusive.
- `affordability_status` is one of `affordable_now`, `affordable_with_plan`, `affordable_later`, or `not_affordable`.
- `recommended_payment_method` is one of `full_payment`, `partial_payment`, `installments`, `wait`, or `not_recommended`.
- `affordable_with_plan` means the full request is completed through a partial-payment schedule, installments, or permitted spending changes.
- `payment_plan` is chronological `YYYY-MM-DD:amount` entries separated by `|`, or `none`. For `partial_payment`, use exactly two payments: `amount_safe_to_pay` on `request_date`, then `requested_amount - amount_safe_to_pay` on `earliest_date_for_full_payment`. Recommend it only when the request allows it, the user accepts it, `0 < amount_safe_to_pay < requested_amount`, and the second payment is on or before `desired_completion_date`. The two payments must add up to `requested_amount`. An installment plan must instead follow a supplied payment option.
- `earliest_date_for_full_payment` is the first conservative projected date for one safe full payment. It equals `request_date` for `affordable_now` and is empty when no full payment is safe within the forecast period.
- `spending_changes_needed` is `none` or up to three `stop:<event_id>` and `reduce_to:<event_id>:<new_amount>` actions. Only non-protected, flexible events in a category the user permits may be changed.
- `decision_explanation` is a concise, grounded explanation of the recommendation.

### 6.4 Constraints That Make The Submission Evaluable

- Be runnable from the terminal.
- Read the provided files from `dataset/`.
- Do not use organizer-only files or hardcoded labels.
- Keep behavior deterministic where possible.
- Read secrets from environment variables only.
- Include clear setup and run instructions in the submitted code package.

### 6.5 Token Usage And Submission Artifacts

Submit `code.zip`, the completed `output.csv`, and the required `chat_transcript`. The submitted `code.zip` must include `evaluation/usage_report.md`. This single file must summarize the final full-dataset run's model providers and names, model calls, input and output tokens, total and average tokens per request, estimated total and per-request cost. Do not include API keys, credentials, or sensitive configuration.

### 6.6 Reasonable Entry Points

There is no required language. If you use Python, `code/main.py` is a good entry point. If you use another language, document the run command clearly in your submitted README.

---

## 7. Cross-Platform And Agent-Compatibility Notes

- Resolve the log path relative to this `AGENTS.md` file, as described in §2. Do not use the platform home directory or hardcode a user path.
- Write logs in UTF-8 with `\n` line endings.
- Do not assume bash. Prefer language-native APIs when possible.
- Keep tool-specific config minimal and point back to this `AGENTS.md`.
- If a nested `AGENTS.md` exists, the closest one wins for files inside that sub-project, but §2 and §5 remain global: keep logging to the `log.txt` beside the top-level `AGENTS.md`, not beside the nested one.

---
