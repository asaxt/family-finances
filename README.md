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
- Full-history transaction cleanup with bulk exclusion and restoration
- Multiple named Plaid connections with combined household totals
- Cached account balances refreshed during Plaid syncs
- Manual, dated savings tracking and update reminders
- User-created accounts classified as pre-tax, post-tax, or taxable
- An editable savings goal, initially `$10,000`, with per-account eligibility
- A user-editable app name

## Developer setup

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
