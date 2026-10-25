# Family Finances

Family Finances is a password-protected, local-first dashboard for household
spending and savings. It imports transactions through Plaid, helps clean and
categorize spending data, visualizes long-term trends, and tracks manually
entered savings balances.

The default display name is **Family Finances**, but each installation can give
the app any name from the Settings page.

## Privacy and security model

- The app listens only on `127.0.0.1` and is not exposed to the local network or
  public internet.
- The database, Plaid credentials, access tokens, transactions, balances, and
  preferences are stored in one authenticated encrypted vault.
- The app password derives a key that unwraps the vault's random encryption
  key. Decrypted data exists only in memory while the app is unlocked.
- Vault files, password metadata, legacy databases, and backups are excluded
  from Git.
- Plaid Link handles bank authentication; institution usernames and passwords
  are not stored by this application.
- Login attempts are throttled, sessions expire, POST requests use CSRF
  protection, and sensitive responses are marked `no-store`.
- Keep full-disk encryption enabled and use encrypted backups. Application-level
  encryption cannot protect an unlocked app from a compromised computer.

This is a personal project and has not received an independent security audit.
Use it at your own risk. It is not affiliated with, endorsed by, or sponsored by
Plaid or any financial institution.

## Features

- Household and individual-account spending views
- Monthly trends, year-over-year comparisons, and moving averages
- Category and merchant exploration down to individual transactions
- Separate account purposes for bank cash flow and spending analysis, with a
  combined option for debit-card or single-account workflows
- Credit-card transactions retained for spending, category, and trend analysis
  without contributing to money-in, money-out, or net-cash-flow totals
- Full-history transaction cleanup with reusable categories and bulk editing
- Multiple named Plaid connections with combined household totals
- Cached account balances refreshed during Plaid syncs
- Manual, dated savings tracking and update reminders
- User-created accounts classified as pre-tax, post-tax, or taxable
- An editable savings goal, initially `$10,000`, with per-account eligibility
- A user-editable app name
- A read-only local Ollama assistant with calculated summaries and visible supporting data
- Named financial plans, saved assumptions, and live scenario comparisons
- An encrypted feedback inbox for this installation

## Categorization and rule review

Optional **Fast classifier + Qwen** categorization uses a local GLiClass v4 model
for clear cases, then local Qwen for uncertain cases. The classifier uses the
installation's existing category labels, treatments, and bounded examples. It
accepts scores of at least 0.60 with a lead of at least 0.25; these scores are
ranking signals, not calibrated probabilities of correctness. Qwen chooses an
existing category, using **Other** when no specific label is supported. Other
must already exist with a non-transfer treatment. Mixed-mode assignments do not
enter the AI review queue, and Other never creates recurring rules. Assistant
chat and existing-category review continue to use Qwen.

The default remains Qwen-only, so installations without the optional classifier
runtime continue to work. To enable the mixed flow, install Python 3.12 separately
from the app environment, then run these commands in the checkout:

```sh
python3.12 -m venv .classifier-venv
.classifier-venv/bin/python -m pip install -r requirements-classifier.txt
.classifier-venv/bin/python setup_classifier.py
```

Setup downloads pinned public model files from Hugging Face. Restart the app with
`FAMILY_FINANCES_CATEGORIZATION_ENGINE=hybrid` in its environment; when using the
provided launcher, add `export FAMILY_FINANCES_CATEGORIZATION_ENGINE=hybrid` to
the ignored `.local.env`. Set it to `qwen` to return to Qwen-only categorization.
Settings identifies when the mixed flow is selected. Local AI must still be
enabled, and the existing Qwen model must be installed in Ollama.

The optional worker currently supports macOS and Linux. It uses the Apple GPU
on Apple Silicon and CPU on other supported systems. An explicit
`FAMILY_FINANCES_CLASSIFIER_DEVICE=cpu` selects CPU on a Mac. The optional model
and runtime occupy roughly 2 GB; actual memory use and latency depend on hardware,
labels, and transaction text. They are excluded from Git.

Inference loads cached safetensors offline with remote model code disabled.
A bounded private subprocess pipe adds no network listener and sends no account
identifiers or database handles to the classifier. It runs as the same operating
system user, so this process separation is not a filesystem security sandbox.
The integration disables model telemetry and discards worker diagnostics; it does
not write request/response caches. Local Ollama settings remain a separate boundary.
Known classifier context limits hand off to Qwen; runtime errors or invalid
responses stop the batch. There is no automatic model download or hosted fallback
during categorization. Category accuracy still requires checking with each
installation's labels and transaction descriptions.

Local AI categorization and statement imports save reusable, exact-description
rules for the same account as each completed group is processed. AI-created rules
remain lower priority than user rules and individual choices. Conflicting guesses,
unresolved groups, and transaction-specific transfer evidence are not generalized
into new rules. An interrupted run keeps rules from its completed groups.

Each model batch is validated before any result is saved. Unexpected fields,
missing or duplicate results, invalid categories, and malformed responses stop
the batch without applying its predictions. Transaction identifiers and account
scope come from the app, not the model.

Before saving, the app also checks whether transactions, category labels, or rules
changed during model processing. A changed batch is rebuilt from current inputs
and retried automatically up to twice. Manual choices and user rules remain
protected; records that become pending, excluded, deleted, or otherwise ineligible
are skipped. If changes continue, the run stops and preserves completed batches.
The results page shows automatic retries and skipped records. Statement-import
categorization uses the same checks and remains limited to the imported records.

New installations include **Other** with Money out treatment. Upgrades preserve
existing category labels and treatments, including a previously deleted Other.

Existing-category review saves supported AI rules automatically as each description
finishes processing; there is no acceptance step. Its results show saved rules and
skipped groups. User rules and individual choices retain their priority. Rule previews
still offer an optional **Replace matching individual category choices** checkbox
with a warning and affected counts. Use **Prefer this rule…** to do this for a saved
rule. Older choices with unknown origin remain protected.

Use the pencil icon on a transaction to edit it, including adding your own description.
It appears in the Transactions list and can be searched alongside the original bank text. Clear it
to restore the original label. The original merchant and description remain
available, and bank refreshes preserve your label. This edits only that transaction;
it does not change categories, remembered rules, or merchant-based reports.

Transactions show their category source and the matching **Category rule text**.
Missing merchant or description text is copied from the other field and marked
as copied. A later bank sync can replace copied text with supplied detail.

Use **Review** for a transaction or **Review details for selected transactions**
for bulk changes. Category Rules also supports bulk changes to text, match
style, category, account scope, and category treatment. Individual user choices
win over rules. Use **Only change this transaction** to preserve saved rules.

Exact description rules match the full description, ignoring case and outer
spaces. Contains rules match a phrase; merchant rules match the full merchant.
No match style automatically wins over another. Saved-rule edits preview their
impact across history. If rules overlap, choose **Replace old rules**, **Keep
old rules as fallback**, or **Cancel**. A fallback applies where its preferred
rule does not match; deleting the preferred rule makes the fallback available
again. Newly encountered unresolved conflicts are flagged for review.

**AI category review → Review existing categories** proposes changes against the current
category labels. Scanning does not change transactions. Accepted suggestions
remain AI-sourced and can be superseded by user rules; they do not create
permanent user overrides. A suggestion conflicting with a user rule directs you
to review that rule. Individual manual edits remain protected during automatic
categorization. AI processing uses the configured local Ollama service.

Upgrades retain an encrypted migration recovery copy. Existing rules with no
recorded origin are conservatively treated as user rules. The development
production mirror remains read-only.

## Spending and budget

Open **Budget** for a selected month's category spending and allowances. Category
patterns leads the page, with twelve monthly budget-use cells that open the matching
transactions. The accumulation start controls cumulative totals independently of
that twelve-month history. Other reporting windows remain available.

Open **Spending trends** for full-history total spending, the category breakdown,
and selectable category time series in dollars. Category charts include monthly
points and trailing 3- and 12-month averages. Averages require a full recorded
window, count known zero-spending months, include refunds, and leave gaps for
missing history. The current month's point and averages use month-to-date actuals.
Account and person filters apply to every chart and transaction link.

**Cash flow** includes income and expense percentage-change lines for the latest
12 completed months. The default compares trailing 12-month totals with totals
ending 12 months earlier. Set the total window and comparison distance separately
(1–60 months each); the controls are remembered for the browser session. Changes
are total percentage changes, not annualized rates. The chart uses included
cash-flow accounts and saved category treatments, excludes transfers and pending
or excluded transactions, and nets refunds against expenses. Missing months,
unclassified transactions, other currencies, and nonpositive earlier totals leave
gaps instead of invented percentages. Tooltips and calculation details show the
compared windows and dollar totals. These controls are independent of the page's
lookback in days, which still applies to its summary cards and recent activity.

In **Edit monthly plan**, new lines default to **% of income**; fixed dollars remain
available. Both carry forward until changed. All percentages use the same monthly
income base. Recorded income is the default. Optional planned income or trailing
3-month, 12-month, and full-history average income can provide a steadier base.
Averages use completed months before each budget month, never later income;
future budgets use history through the last completed month. Available USD
cash-flow months are averaged, including recorded zero-income months. Missing
months are excluded and counted in the preview and monthly details. With no prior
history, percentage allowances are marked unavailable; fixed-dollar lines still apply. The plan editor shows a
monthly timeline of saved percentages and dollar amounts. Review a proposed plan
before applying it from an effective month onward: later saved plans are replaced,
with affected intervals and old/new settings shown first. Modify or cancel the
draft without saving; earlier months retain their settings. The accumulation
filter changes reporting only, not the saved plan dates. Full-history
spending charts and a category selector show actual dollars independently of
budget amounts and the selected snapshot. The spending-mix pie uses the same full
history; refunds reduce category totals, and nonpositive totals have no pie slice.
The budget page includes cumulative utilization and expandable transaction details. Chart points and heatmap cells link to their transactions. Account and person filters show scoped spending without comparing
it to household allowances. Earnings trends remain available from this page.

Compare monthly spending and cumulative utilization, including refunds and
negative overspending. Accumulation does not reset in January. Each month uses
its applicable saved plan; a new effective month preserves earlier allowances.
Views cover up to ten years at a time. Changes to transaction categories or
recorded income update the report. The brokerage remainder is calculated income
minus spending, not an actual transfer or account balance; unused allowances are
not counted again as cash. Missing records and unsupported currencies are flagged.

Budget settings stay in the existing encrypted vault. Development snapshots
allow viewing but cannot save budget changes.

## Ask AI (preview)

Enable **Local AI assistance** in Settings, then open **Ask AI**. The preview
uses the locally installed `qwen3.8:27b` model through Ollama on this computer.
Ask about spending by category, income over time, matching transaction
descriptions, or the latest manually recorded savings balances and goal.
Follow-up questions retain the last three exchanges while the page is open.

The app calculates totals using its reporting rules before asking the model to
explain them. Expand **View supporting data** to check the selected dates,
category, description filter, counts, totals, and sample records. Questions use
all included accounts; account-specific analysis and historical savings
comparisons are not supported in this preview. Missing records do not prove
zero activity. Large requests ask for a narrower period rather than silently
calculating incomplete totals. AI explanations can still be inaccurate.

Questions and allowlisted financial context are sent only to the loopback
Ollama service, with proxies and redirects disabled. The assistant cannot run
SQL, modify records, or initiate bank operations. The app does not save chat
history or log prompt/answer text. Leaving the page or choosing **New chat**
clears the conversation. The existing local AI switch also disables chat.

### Review existing categories with local AI

After adding more specific category labels, open **Transactions → AI category review**
and choose **Update categories with AI**. The scan compares all posted, categorized
transactions with the current labels, including manual choices and excluded records.
It automatically saves reusable exact-description rules for the same account and
shows the results as it runs. You can edit saved rules in Category Rules or use the
pencil icon on a transaction. Manual category choices and user rules retain their
priority. Uncertain, conflicting, transfer-specific, or stale results are skipped.
An interrupted run keeps rules from completed descriptions; it does not require
accepting or dismissing suggestions before another run.
The read-only development mirror does not permit scans or data edits.


## Lifetime planning (development preview)

**Plan** offers live calculations, a monthly lifestyle view, investment charts,
and annual details. Name a plan and use **Save**, **Save a copy**, or **Discard
edits** to keep or reject changes. Drafts and pinned comparisons stay in the
page until explicitly saved; calculation never changes financial records.

Saved plans retain their inputs, starting year, and recorded spending snapshot.
**Use latest Savings, people & spending** explicitly refreshes connected sources
while preserving fields edited as manual assumptions. Comparisons use the active
plan's spending snapshot and starting year; current ages must match. Saved
assumptions are retained, but calculations use the current app version.

Recorded spending uses the last 12 complete calendar months and Spending Trends
treatment. Refunds reduce spending; transfers, pending and excluded records are
omitted. Missing history and mixed currencies remain unknown. Enter an explicit
monthly lifestyle assumption to calculate without usable history, and set the
tax-payment adjustment to zero in that case. With history, remove only income
taxes already counted in spending. A direct lifestyle assumption replaces the
adjusted spending figure without subtracting taxes again.

Add named expenses for one year or several years, in today's dollars. They must
fit within the projection horizon. Expenses use available taxable investments
after that year's growth and regular cash flow, including any surplus; they do
not trigger extra retirement withdrawals. Unfunded expenses and recurring
lifestyle gaps are reported separately. Savings goal progress does not reserve
money: exclude amounts held outside the plan, and avoid subtracting a planned
expense from starting investments a second time.

The model covers two married adults with independent income, saving rates,
pre-tax/Roth balances, retirement ages, and Washington/Oregon residence and work
locations. Wages and contributions stop at each retirement age. One household
withdrawal rate applies to combined opening investments, beginning at the first
or both retirements. Withdrawals split equally between retirement and brokerage,
with fallback when a pool is insufficient, capped after growth. Pre-tax
withdrawals are taxable; Roth withdrawals are assumed qualified. Regular spending
gaps never trigger additional withdrawals, even when assets remain.

Balances grow at a fixed rate; cash flows occur at year end. Spending and expenses
increase with inflation. The monthly view uses annual averages and defaults to
the first year both adults are retired. Today's-dollar cash flows use their own
year's inflation factor. The horizon ends when the younger adult reaches 95;
both remain alive in the model, with an age difference of at most 25 years.
Social Security, pensions, debt funding, brokerage gains taxes, and variable
market returns are excluded. See [tax coverage and official sources](docs/planning-taxes.md)
for federal/WA/OR estimates, separate-return allocation, contribution limits, and
other exclusions. These are illustrations, not a sustainable spending guarantee,
tax return, or probability of success.

Plans are encrypted in the application vault. In the read-only development
mirror, plans and feedback use a separate encrypted companion file that survives
mirror refreshes without changing the financial snapshot. It uses the current
unlock key: rotating the source vault key can require recovery with the earlier
key. An unreadable companion file is preserved. Earlier per-person withdrawal
plans are translated in memory; differing old rates require a shared rate before
saving.

## Feedback and operational records

**Send feedback** saves the submitted text and time to this installation's
encrypted inbox. Messages can be viewed and deleted in **Feedback**. Nothing is
attached automatically, and message contents are excluded from operational logs.

The app sends no usage telemetry. Its local request records contain only time,
HTTP method, a known endpoint identifier, status, and duration. Application errors
use generic event labels; request bodies, URL queries, exception details, and
financial content are omitted. The ordinary Werkzeug access log is disabled.
This describes the app's logging; a future host or proxy needs its own logging
configuration.

## Developer setup

Use Python 3.11 or newer.

Development never clears categories or rules on unlock. A configured production
mirror uses a fresh, separately encrypted snapshot on every server launch;
production is read only as a source of encrypted files. Schema migrations apply
only to the copy, after which database writes and data-changing requests are
blocked. The snapshot banner shows when the data was copied. Relaunch development
to load newer production changes. Categorization and other financial edits must
be made in production. Chat and session-only AI/display preferences still work.

The original independent development vault is preserved when switching to mirror
mode. Mirror startup uses separate snapshot directories instead of overwriting
that vault. Without mirror mode, development keeps its independent working copy
across restarts; code updates never implicitly discard its edits.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open <http://127.0.0.1:4242>, create an app password, and enter credentials from
your own Plaid developer account on the Settings page. Real institution data may
require Plaid Production access and institution-specific approval.

Use **Connect a bank** after Plaid setup. The app requests up to 730 days of
transaction history when Plaid makes it available. Additional household members
and their display names are added in the interface.

## Tests

```sh
python -m unittest discover -s tests -v
```

The app uses Flask, SQLite, AES-256-GCM, Argon2id, Plaid, and a pinned local copy
of Chart.js 4.5.1 with its MIT license.

This project deliberately contains no container, public hosting, or
remote-network setup. Do not change the Flask host to `0.0.0.0` without first
adding and reviewing appropriate network security controls.

## Public fixture policy

Tests, examples, documentation, commit messages, and pull-request text must use
obviously fictional data. Never copy transaction descriptions, merchants,
employers, financial institutions, amounts, dates, account details, or other
personal-life clues supplied by a user or taken from a real financial record.
Use clear placeholders such as `EXAMPLE EMPLOYER`, `SAMPLE CAFE`, and invented
amounts and dates that cannot reasonably be mistaken for a person's data.
