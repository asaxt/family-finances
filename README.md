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

## Lifetime planning (development preview)

**Plan** models two spouses with independent income, saving rates, pre-tax/Roth
balances, retirement ages, and Washington/Oregon residence
and physical work locations. Choose married filing jointly or separately.
Separate returns require an explicit income-allocation assumption for community
property. The household shares brokerage investments, inflation, growth, one withdrawal
percentage, and a withdrawal start choice (first spouse retired or both retired).

Yearly spending uses the last 12 complete calendar months and Spending Trends
classification. Refunds reduce spending; transfers, pending and excluded records
are omitted. Missing months and mixed currencies prevent calculation. Review
coverage and subtract only income-tax payments already counted in spending.

Each person's wages and new retirement contributions stop at their retirement
age. Contributions use the indexed 2026 employee-plan base limit; catch-ups,
IRA contributions, and employer matches are not included. The shared withdrawal rate applies to the combined opening investment balance.
Half the dollar amount comes from retirement and half from brokerage; when one
pool cannot supply its half, the other covers the remainder, capped after growth.
Retirement withdrawals are allocated proportionally across all owners and tax
types, including a spouse still working.
Pre-tax withdrawals are taxable; Roth withdrawals are assumed qualified.
After modeled taxes and spending, surplus is reinvested in brokerage. Deficits
are funding gaps under the elected rate, even with assets remaining. There are
no automatic extra withdrawals. Both balances grow at
the selected rate, with cash flows at year end.

Federal income/payroll taxes and Oregon/Washington income-tax estimates are
shown separately. The model includes Oregon work sourcing and proportional
interstate credits, and Washington's enacted ordinary-income tax from 2028.
See [tax coverage and official sources](docs/planning-taxes.md) for assumptions,
indexing, community-property choices, and exclusions. This is a planning
illustration, not a tax return or a probability of success.

Charts and annual tables run until the younger spouse reaches 95 (both remain
alive in the model; supported age gap is at most 25 years). Comparisons are
page-only. Explicitly saving household profiles and assumptions writes to the
existing encrypted vault. Calculation never writes records. In the read-only
development mirror, saving is disabled and all edits remain temporary.

The monthly spending view compares the recorded 12-month average with the
inflation-adjusted cost of the same lifestyle and after-tax income/withdrawals
in any projection year. It defaults to the first year both spouses are retired.
Combined withdrawals already include brokerage. Monthly surplus and unfunded
gaps are shown without extra top-ups; the view does not claim a sustainable
maximum spending level. Monthly amounts are annual averages,
converted to today's purchasing power using the cash-flow year's inflation
factor. The current comparable average removes tax payments already in spending.

Saved plans from the earlier per-person-withdrawal preview are translated in
memory without rewriting the vault. Matching old rates become the shared rate;
differing rates require a new selection. The page asks the user to review the
changed withdrawal behavior before explicitly saving.

## Developer setup

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
