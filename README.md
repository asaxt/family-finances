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

**Plan** accepts eight inputs: annual income, tax-advantaged saving percentage,
retirement withdrawal percentage, retirement age, current age, starting
investments, annual inflation, and annual investment growth. The latest active
Savings balances provide the starting total and retirement/taxable proportions.
Editing the total preserves that split; without recorded balances, an entered
starting amount is treated as retirement investments.

Yearly spending is inferred from the last 12 completed calendar months using
Spending Trends treatment. Refunds reduce spending; transfers, pending records,
and excluded transactions are omitted. Missing months and mixed currencies
prevent calculation. Records in all 12 months do not establish complete account
coverage; the page shows monthly totals and classification warnings for review.

Before retirement, tax-advantaged contributions equal income times the saving
percentage. Income minus contributions minus spending goes to taxable brokerage
investments; a negative remainder draws from that balance. Income and
contributions stop at retirement. Each year's withdrawal is the selected
percentage of the opening retirement balance, capped at funds available after
growth. Withdrawals fund spending, with taxable investments covering the gap
or receiving any excess. Both balances earn the chosen growth rate. Income and
spending increase with inflation, with cash flows applied at year end.

The chart and annual table distinguish retirement and taxable investments and
can show today's or future dollars through age 95. Keep a baseline in the page,
edit the same eight inputs, and calculate again to compare. Calculations do not
write records, including in the development mirror. Plans are cleared on exit.
Taxes, contribution limits, penalties, benefits, and market volatility are not
modeled; these are illustrations, not success probabilities.

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
