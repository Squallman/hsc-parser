# HSC Parser

A lightweight monitor for HSC (ГСЦ МВС) electronic queue availability —
practical driving exam, category A, at the service centres you choose.

The project authenticates locally through the official HSC / ID.GOV.UA browser
flow, persists the resulting HTTP session securely, and lets a headless monitor
check service-centre availability from GitHub Actions.

It does not book appointments automatically.

---

## Code generation

The implementation of this project was fully generated with OpenAI Codex.

The architecture, requirements, security boundaries, runtime behaviour and live
validation scenarios were defined iteratively, while Codex generated and
refactored the implementation and its automated test suite.

Correctness is held to the checks the project runs on itself:

```bash
pytest
ruff check .
mypy --strict src
```

---

## Architecture

Two runtimes, one persisted hand-off between them.

### 1. Local authentication

```
Local machine
    |
    v
Chromium / Playwright
    |
    v
HSC + ID.GOV.UA authentication
    |
    v
/cabinet/queue bootstrap
    |
    v
HTTP cookie jar
    |
    v
Encrypted MongoDB session
```

```bash
python -m hsc_queue_monitor.cli refresh-session
```

Authentication happens only here. The electronic-signature private key and its
password stay on the local machine, as does the browser profile. Once the
browser has signed in and `/cabinet/queue` has minted the queue-session cookie,
the cookies are handed to a plain `requests` session, encrypted and written to
MongoDB — after which Chromium is closed. Routine monitoring never needs it
again.

### 2. Headless monitoring

```
GitHub Actions
    |
    v
MongoDB
    |
    +--> encrypted HSC session
    +--> monitor state
    +--> availability snapshot
    |
    v
HSC read-only API
    |
    v
availability diff / monitor transition
    |
    +--> slot added
    +--> slot removed
    +--> authentication required
    |
    v
Telegram
```

```bash
python -m hsc_queue_monitor.cli monitor-once
```

No Chromium. No Playwright authentication. No private signing key. No booking.
The HSC API path is read-only — every request is a GET, and tests enforce it.

---

## Authentication support

This project supports:

* **ID.GOV.UA electronic-signature authentication** with a file-based signing key
* **КНЕДП provider:** КНЕДП "MASTERKEY" ТОВ "АРТ-МАСТЕР"
* **macOS native file picker** for selecting the signing key file

Not currently supported:

* Дія.Підпис (Diia electronic signature)
* BankID or other authentication methods
* Hardware or token-based signing devices
* Other КНЕДП providers without explicit validation
* Unattended authentication in headless environments (GitHub Actions)

---

## Why this architecture

**A. Authentication stays local.** ID.GOV.UA requires an electronic signature
with a file-based private key, which means sensitive local material. GitHub
Actions therefore never receives the key file, the key password,
`IDGOV_SIGNING_KEY_PATH`, `IDGOV_SIGNING_KEY_PASSWORD` or the browser profile
— only the secrets headless monitoring actually needs.

**B. The browser is temporary.** Chromium exists to establish and refresh the
authenticated HSC session, nothing more. Afterwards a `requests.Session` is
sufficient, so the browser closes and the monitored path stays small,
deterministic and cheap to schedule.

**C. MongoDB bridges the two runtimes.** Three documents, deliberately separate
because they have three different lifetimes:

| Document | Holds |
|---|---|
| `hsc-api-session` | the encrypted HSC HTTP cookie jar |
| `hsc-monitor-state` | operational state: `READY`, `AUTH_REQUIRED`, `RATE_LIMITED`, `SERVICE_UNAVAILABLE` |
| `hsc-availability-snapshot` | the last *complete* availability scan, used for diffing |

**D. `AUTH_REQUIRED` is sticky.** If the persisted session is expired or refused,
the monitor state becomes `AUTH_REQUIRED` and later scheduled runs stop before
making any HSC request. Monitoring resumes only after a local `refresh-session`
succeeds — a bad session is never used to knock repeatedly.

**E. Availability is snapshot-based.** Only a complete scan replaces the
snapshot. Added and removed slots are computed against the previous complete
snapshot, an unchanged scan notifies nobody, and a partial or failed scan can
never be mistaken for "everything disappeared".

**F. Telegram is outbound-only.** The bot sends. There is no `getUpdates`, no
webhook, no commands and no incoming message handling. Three things get a
message: a slot appeared, a slot disappeared, or authentication is required.

---

## Main commands

```bash
# Refresh the authenticated HSC session locally (opens Chromium)
python -m hsc_queue_monitor.cli refresh-session

# Discover service centres and update config/service_centers.yaml
python -m hsc_queue_monitor.cli init-config

# Run one headless availability scan (what GitHub Actions runs)
python -m hsc_queue_monitor.cli monitor-once

# Send one test message to every Telegram recipient
python -m hsc_queue_monitor.cli telegram-test
```

The full command list, including the selector-discovery and debugging tools, is
in [Commands](#commands).

---

## Runtime boundaries

**Local only — never given to GitHub Actions:**

```
IDGOV_SIGNING_KEY_PATH
IDGOV_SIGNING_KEY_PASSWORD
```

...along with the file-based signing key itself and `data/browser-profile/`.

**GitHub Environment `production` secrets:**

```
HSC_MONGODB_URI
HSC_SESSION_ENCRYPTION_KEY
TELEGRAM_BOT_TOKEN
TELEGRAM_USERS
```

**Version-controlled operational configuration:**

```
config/app.yaml              timeouts, pacing, retries, database names
config/service_centers.yaml  which centres to watch
```

How to set each of these up is in [Configuration](#configuration); this section
is only the boundary itself.

---

## Safety scope

This is an availability monitor. It does **not**:

* book appointments automatically;
* submit, modify or cancel an appointment;
* bypass CAPTCHA, WAF or anti-bot protection;
* forge or synthesise authentication cookies or tokens;
* automate electronic-signature authentication outside the normal, visible
  browser flow.

It reads which dates and times are free and stops there. Reserving an
appointment is yours to do, by hand, in the browser. It also never polls faster
than 30 seconds, and a CAPTCHA pauses the run so a person can answer it.

---

## Why every selector lives in YAML

The exact DOM of the HSC cabinet is not known up front, and it changes. So **no
page object contains a selector string**. Every element is described in
`config/selectors.yaml` and resolved at runtime:

```yaml
category:
  category_a:
    strategy: text
    value: "Категорія A"
```

Unknown selectors ship as `TODO`. Trying to use one fails loudly instead of
clicking something random:

```
SelectorNotConfigured:
calendar.available_slot has not been configured.
Discover it with:  python -m hsc_queue_monitor.cli inspect
```

The config files split responsibilities:

| File | Answers |
|---|---|
| `config/selectors.yaml` | **Where** elements are |
| `config/flow.yaml` | **What** steps exist, in what order, and how to reach each screen |
| `config/service_centers.yaml` | **Which** service centres to watch |
| `config/app.yaml` | **How** it runs: timeouts, pacing, retries, database names |

`.env` answers only **who you are**: six secrets, and nothing else. See
[Configuration](#configuration).

`flow.yaml` holds two independent lists, which are easy to confuse:
`flow.queue.steps` is the order the **monitor** walks the booking journey, while
the top-level `steps:` map tells **`test-step`** how to reach the screen that
holds a given selector.

---

## Configuration

Two kinds of configuration, kept apart on purpose:

* **`config/app.yaml`** — every operational setting. Committed, reviewed and
  diffed. A secret written into it is refused at load time, not ignored.
* **`.env`** — six secrets, and nothing else. Never committed.

Nothing crosses the line: a credential in YAML is an error, and a timeout in an
environment variable is not read at all.

### A. Application configuration — `config/app.yaml`

| Section | What it sets |
|---|---|
| `mongodb:` | `database`, `session_collection` — names only, never the URI |
| `api:` | `monitor_interval_seconds`, `connect_timeout_seconds`, `read_timeout_seconds`, `slot_request_interval_seconds` |
| `api.retry:` | `max_attempts`, `initial_backoff_seconds`, `max_backoff_seconds`, `multiplier`, `max_retry_after_seconds` |
| `telegram:` | `enabled` — *whether* to notify (**who** is a secret) |
| `browser:` | `headless` — overridden per run by `--headed` / `--headless` |
| `browser_monitor:` | pacing for the older, local browser `monitor` command |

The shipped values are measured, not guessed: a 60s read budget because `/slots`
timed out at 30s, 3s between `/slots` requests because HSC answered 429 to two
of them one second apart, and a 300s scan interval against a 900s queue-session
lifetime. Every value is range-checked when the file loads.

The runner reads this file straight from the checkout, so a setting is never
configured twice — there are no operational GitHub variables.

### B. Local authentication secrets — `.env`

```bash
cp .env.example .env
```

| Variable | Needed by | What it is |
|---|---|---|
| `IDGOV_SIGNING_KEY_PATH` | `refresh-session` | Path to your file-based ID.GOV.UA signing key (keep it outside the repo) |
| `IDGOV_SIGNING_KEY_PASSWORD` | `refresh-session` | Its password |
| `HSC_MONGODB_URI` | `refresh-session`, `monitor-once`, `init-config` | Connection string, credentials included |
| `HSC_SESSION_ENCRYPTION_KEY` | the same three | Fernet key the stored session is encrypted with |
| `TELEGRAM_BOT_TOKEN` | `telegram-test`, notifications | The bot itself |
| `TELEGRAM_USERS` | the same | Comma-separated numeric recipient ids |

No command needs all six:

| Command | Secrets |
|---|---|
| `refresh-session` | `IDGOV_SIGNING_KEY_PATH` `IDGOV_SIGNING_KEY_PASSWORD` `HSC_MONGODB_URI` `HSC_SESSION_ENCRYPTION_KEY` |
| `init-config` | `HSC_MONGODB_URI` `HSC_SESSION_ENCRYPTION_KEY` |
| `monitor-once` | `HSC_MONGODB_URI` `HSC_SESSION_ENCRYPTION_KEY` (+ `TELEGRAM_*` to notify) |
| `telegram-test` | `TELEGRAM_BOT_TOKEN` `TELEGRAM_USERS` |
| `selectors`, `steps` | none |

**The first two are local only.** They authenticate to ID.GOV.UA via electronic
signature, and GitHub Actions is never given them — tests assert that
`monitor-once`, `init-config` and `telegram-test` never so much as ask for them.

Generate the encryption key once, locally:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

`TELEGRAM_USERS` is a **secret**, not a public setting: those numbers identify
people. It is redacted from logs alongside the token, and delivery lines show a
masked id (`***6789`).

### C. Telegram, locally

Set the two Telegram secrets in `.env` and prove the transport before relying on
it:

```bash
python -m hsc_queue_monitor.cli telegram-test
```

It sends one message to each recipient and reports per-recipient delivery. It
opens no database, reads no session, calls no HSC endpoint and starts no
browser — when it fails, the thing that failed is Telegram. Each recipient must
open the bot and press Start once first; a bot cannot begin a conversation.

### D. GitHub Actions — production monitoring

The scheduled workflow runs in a GitHub **Environment** named `production`, and
every secret it uses is defined there rather than at repository level.

**Settings → Environments → New environment → `production`**, then add four
*environment secrets*:

| Secret | What it is |
|---|---|
| `HSC_MONGODB_URI` | The MongoDB connection string, credentials included |
| `HSC_SESSION_ENCRYPTION_KEY` | The Fernet key that decrypts the stored session |
| `TELEGRAM_BOT_TOKEN` | The notification bot |
| `TELEGRAM_USERS` | Comma-separated recipient ids — a secret, **not** `vars.` |

No repository *variables* are used at all. Everything non-sensitive comes from
`config/app.yaml` in the checkout.

**Never give GitHub Actions:** `IDGOV_SIGNING_KEY_PATH`, `IDGOV_SIGNING_KEY_PASSWORD`,
the signing key file, the browser profile, or HSC cookies. The runner gets the
encrypted store, the key to read it and a bot to speak through — nothing that could
authenticate as you. It never runs `playwright install`, so no browser binary is
ever downloaded. Tests assert all of this against the workflow file.

---

## First run

```bash
python3.12 -m venv .venv          # any Python ≥ 3.12
source .venv/bin/activate
pip install -e .
playwright install chromium
cp .env.example .env
```

Edit `.env` and set `IDGOV_SIGNING_KEY_PATH` to your file-based ID.GOV.UA signing
key. **Keep that file outside this repository.** Telegram is not needed yet — leave
those two variables empty. Everything that is not a secret is already configured in
`config/app.yaml`, which is committed; see [Configuration](#configuration).

Then check what still needs configuring:

```bash
python -m hsc_queue_monitor.cli selectors
```

Open the site with the persistent profile and start discovering selectors:

```bash
python -m hsc_queue_monitor.cli inspect
```

Validate one selector at a time:

```bash
python -m hsc_queue_monitor.cli test-step login.key_file
```

Walk the flow interactively, one step per ENTER:

```bash
python -m hsc_queue_monitor.cli flow
```

Once every step works, run it without prompts:

```bash
python -m hsc_queue_monitor.cli flow --auto
```

And finally monitor without touching Telegram:

```bash
python -m hsc_queue_monitor.cli monitor --dry-run
```

---

## Setup guides

Longer setup and troubleshooting guides live beside the code:

* [docs/mongodb-atlas.md](docs/mongodb-atlas.md) — practical MongoDB Atlas
  setup for session storage
* [docs/authentication.md](docs/authentication.md) — how automatic ID.GOV.UA
  electronic-signature authentication works, what happens when a session
  expires, and how to discover its selectors
* [docs/ui-flow.md](docs/ui-flow.md) — the one-screen-at-a-time loop for
  discovering selectors with `inspect` and validating them with `test-step`

---

## Commands

| Command | Purpose |
|---|---|
| `selectors` | List configured vs. TODO selectors (no browser) |
| `steps` | List available flow steps and the configured order (no browser) |
| `auth-status` | Report whether the profile still has a live session. Diagnostic only — never logs in |
| `ensure-auth` | Run only the authentication guard, then stop at `/cabinet` |
| `inspect` | Open the site, dump visible interactive elements on ENTER |
| `inspect-auth` | Same, for the signed-out / ID.GOV.UA screens; every capture uniquely numbered under `data/debug/auth/` |
| `screenshot` | Save a screenshot + URL + sanitized element dump |
| `test-step <key>` | Run the prerequisite chain, then resolve/count/highlight the target; `--click` to interact, `--manual-prepare` to navigate by hand |
| `check-center <id>` | Reach the service-centre screen, search one centre by ID and report whether its card is enabled; `--click` to select it and stop on the next screen |
| `check-availability` | Scan 1–5 centres for free **dates and times**; `--center <id>` (repeatable) overrides the configured list. Reads only — books nothing |
| `flow` | Run the configured flow step by step; `--auto`, `--from`, `--no-login` |
| `monitor` | Poll for slots through the browser; `--dry-run`, `--once` |
| `api-probe` | DIAGNOSTIC: one direct GET against the HSC JSON API with the browser's cookies |
| `api-observe` | DIAGNOSTIC: log the `/api/` calls the page makes while you click |
| `api-availability` | DIAGNOSTIC: read one centre's dates and times through the API; `--open-queue`, `--max-dates`, `--slot-interval` |
| `init-config` | Discover HSC's service centres from one departments call and write `config/service_centers.yaml`; `--dry-run`, `--force`, `--output`. New centres arrive `enabled: false` |
| **`refresh-session`** | **LOCAL**: authenticate, mint the queue session, store it encrypted in MongoDB, close the browser |
| **`monitor-once`** | **HEADLESS**: one availability scan from the stored session. No browser. This is what GitHub Actions runs |
| `api-monitor` | Local/debug: the same headless reads, in a long-running loop; `--interval`, `--once` |

Global flags: `-v/--verbose`, `--headed/--headless`, `--pwdebug`,
`--config-dir`, `--data-dir`.

### Playwright Inspector

```bash
python -m hsc_queue_monitor.cli flow --pwdebug
# equivalently
PWDEBUG=1 python -m hsc_queue_monitor.cli flow
```

---

## How the flow is assembled

`config/flow.yaml` lists step names; `src/hsc_queue_monitor/flow/steps.py` maps
each name to a page-object call. Run `python -m hsc_queue_monitor.cli steps` to
see them all. Adding a screen means adding a page object plus one registry
entry — the engine, CLI and monitor do not change.

The monitor loop, once selectors are configured:

```
loop:
    for each enabled service center:      # sequential, one browser context
        ensure authenticated              # opens /cabinet, logs in if expired
        start registration → practical exam → category A
        wait for the service-centre screen   # destination state, not a sleep
        select service center → open calendar
        read available slots
        notify about slots not seen before
    sleep poll interval ± jitter
```

### `/cabinet/queue` is never opened by URL

Only `site.cabinet_url` is ever navigated to. The registration screen is reached
by clicking `queue.start_registration`, the way a person gets there — jumping
straight to the URL would skip whatever state the site sets up on the way.
`site.queue_url` is kept in `flow.yaml` for reference only; nothing navigates to
it, and prerequisite chains never fall back to it.

---

## Debug artifacts

| Path | Contents |
|---|---|
| `data/debug/NNN-<step>.png` | Screenshot after each step |
| `data/debug/page-elements.json` | Sanitized interactive element dump |
| `data/debug/auth/NNN-<label>.png/.json` | `inspect-auth` captures — numbered, never overwritten |
| `data/debug/events.jsonl` | One line per action: step, URLs, selector, result, duration |
| `data/debug/errors/<ts>-<step>.png/.json` | Automatic capture when an action fails |

Full HTML is **not** saved by default — it can contain personal data from the
authenticated cabinet. Cookies, tokens and headers are never written anywhere.

---

## State

`data/state.json` records which slots have already been reported:

```json
{
  "version": 1,
  "updated_at": "2026-08-12T09:00:00+00:00",
  "seen_slots": {
    "ТСЦ 8041|2026-08-20|10:40": "2026-08-12T09:00:00+00:00"
  }
}
```

Identities and timestamps only — no cookies, tokens, MasterKey information or
passwords. A slot that disappears and comes back is reported again after
`browser_monitor.notify_cooldown_seconds` in `config/app.yaml` (default 6
hours). Entries unseen for 30 days are pruned.

---

## Operating the two paths

The split is sketched under [Architecture](#architecture); this section is how
it behaves in practice.

`refresh-session` is the only command that needs the signing key, and the only
one that can open a browser. `monitor-once` cannot authenticate at all: the
module it runs (`api/headless_monitor.py`) cannot even *reach* Playwright, and a
test walks the whole import graph to keep it that way.

`api-monitor` is the same headless reads in a local loop, kept for endurance
testing and debugging. GitHub Actions must use `monitor-once`.

### Exit codes for `monitor-once`

`monitor-once` is designed for scheduled execution in GitHub Actions. Operational
failures are reported through persisted monitor state, logs and Telegram
notifications rather than a non-zero exit code, so transient HSC problems do not
fail the scheduled workflow.

| Code | Meaning |
|---|---|
| 0 | Monitor invocation completed — see logs and monitor state for operational result |
| 2 | Configuration error — e.g. MongoDB is not configured (before any operations run) |

**A green GitHub Action means the monitor ran, not that HSC was available.**

All operational outcomes return 0 and are signaled through:

* **Persisted monitor state** (`READY`, `AUTH_REQUIRED`, `RATE_LIMITED`, `SERVICE_UNAVAILABLE`)
* **Logs** (info level for normal operation, error level for problems)
* **Telegram notifications** (if configured) — sent on state transitions, not per-run

Examples:

| Outcome | Exit code | State persisted | Telegram |
|---------|-----------|-----------------|----------|
| Scan OK, availability unchanged | 0 | `READY` | silence |
| Scan OK, new slots appeared | 0 | `READY` | 🟢 notification |
| Session expired/refused | 0 | `AUTH_REQUIRED` | 🔐 notification (once) |
| Rate limited (HTTP 429) | 0 | `RATE_LIMITED` | 🟠 notification (once) |
| Service unavailable (5xx/timeout) | 0 | `SERVICE_UNAVAILABLE` | 🟡 notification (once) |
| Telegram delivery failed | 0 | persisted normally | logged, not retried |
| Database connection failed | 0 | not persisted, logged | best-effort 🔴 error message |
| Unexpected runtime exception | 0 | not persisted, logged | best-effort 🔴 error message |

### Monitor state

A second MongoDB document (`_id: "hsc-monitor-state"`, never inside the
encrypted session) remembers what the last run found, so the next one does not
repeat its mistake:

* `READY` — normal. Set after a scan in which *every* centre was read.
* `AUTH_REQUIRED` — **sticky**. Later runs stop at the gate: no request is sent
  and the session is not even decrypted. Only a successful local
  `refresh-session` clears it.
* `RATE_LIMITED` — carries `retry_after_at`; runs before that moment skip the
  API entirely.
* `SERVICE_UNAVAILABLE` — temporary, never sticky.

### Availability changes

A third document (`_id: "hsc-availability-snapshot"`) remembers what was free
after the last **complete** scan, so `monitor-once` prints only what changed:

```
HSC AVAILABILITY CHANGED

New slots:
  3242
    2026-08-26
      + 09:18-09:44

Removed slots:
  3242
    2026-08-27
      - 08:26-08:52
```

A first run, an unchanged run and a newly enabled centre all print **nothing** —
silence is the design, not an omission. The full availability list is no longer
printed on every run.

Slot identity is `(centre number, date, start time)`; the end time is metadata,
and the internal department id plays no part. Only a complete scan may replace
the snapshot: a partial read is an unknown, not an empty result, so a timeout can
never be reported as "everything disappeared". The snapshot is written *before*
the change is announced — at-most-once, deliberately, because a missed change
shows up in the next run's diff while a repeated one never stops.

The rule that shapes all of it: **exhausted retries are never an authentication
problem.** An outage is not an expired login, and only a 401 or 403 — HSC
refusing *this session* — can produce `AUTH_REQUIRED`.

Transient failures (429, 500, 502, 503, 504, timeouts, dropped connections) are
retried by one policy in one place (`HscApiClient._get`), three attempts with a
deterministic 2s/4s backoff, honouring `Retry-After` up to a cap. A 401, 403 or
malformed body is an *answer* and is never retried. Tunable under `api.retry:`
in `config/app.yaml`.

### Telegram notifications (optional)

Outbound only: the bot **sends**. There is no polling, no webhook, no command
handling and no way for it to read anything — asserted by tests over the whole
`notifications/` package.

Three things get a message, and nothing else does:

| Event | Message |
|---|---|
| New slots appeared | 🟢 З'явилися нові слоти |
| Slots disappeared | 🔴 Слоти більше недоступні |
| Session needs re-authentication | 🔐 Потрібна повторна авторизація |

Additions and removals from the same scan are **one** combined message. A first
run, an unchanged run and a repeated `AUTH_REQUIRED` send nothing at all.

Setup:

1. Open **@BotFather** in Telegram.
2. Create a bot with `/newbot`.
3. Copy the bot token into `TELEGRAM_BOT_TOKEN`.
4. **Each recipient opens the bot and presses Start once.** A bot cannot begin a
   conversation with somebody who has never written to it — this project never
   reads that message, it just needs the conversation to exist.
5. Get each recipient's numeric Telegram id and put them, comma-separated, in
   `TELEGRAM_USERS`:

   ```
   TELEGRAM_USERS=123456789,987654321
   ```

6. Verify the transport: `python -m hsc_queue_monitor.cli telegram-test`.

Both are secrets: `.env` locally, and environment secrets of the `production`
GitHub Environment in CI — see [D](#d-github-actions--production-monitoring).
With neither set, `monitor-once` behaves exactly as it did before notifications
existed; with only one of them it refuses to start rather than run half a
feature. Notifications can also be switched off wholesale with
`telegram.enabled: false` in `config/app.yaml`, without removing the secrets.

### When a scheduled run says AUTH REQUIRED

The runner cannot fix this, by design. On your own machine:

```bash
python -m hsc_queue_monitor.cli refresh-session
```

That opens the browser, signs in with the MasterKey, mints a fresh queue
session, writes it to MongoDB and closes the browser. The next scheduled run
picks it up on its own — nothing needs restarting.

### GitHub Actions setup

`.github/workflows/hsc-monitor.yml` runs `monitor-once` every five minutes
(`workflow_dispatch` for a manual run), with `concurrency` so two runners never
share the persisted session, and a 15-minute job timeout.

The job declares `environment: production`, so its four secrets are scoped to
that environment and granted to nothing else in the repository. Configure them
as described in [B](#b-local-authentication-secrets--env) locally and
[D](#d-github-actions--production-monitoring) in CI — that section is the single
place the CI setup is written down.

No repository variables are involved: database and collection names, timeouts,
pacing and the retry policy all come from `config/app.yaml` in the checkout, so
the runner behaves like the machine it was tested on.

---

## Privacy and signing identity

**Important:** This project authenticates using your real ID.GOV.UA electronic
signature. An authenticated HSC session carries your identity and may be
associated with personal information available to your HSC account. Therefore:

* This project persists an authenticated HSC session in MongoDB for unattended
  monitoring.
* The session is encrypted with a Fernet key before storage.
* The encrypted session and the decryption key are both necessary to access your
  HSC account through this project's automation.
* If MongoDB, the encryption key, GitHub Actions environment variables, or the
  persisted session document are compromised, an attacker could read your HSC
  account data (subject to HSC's own access controls).

**Assess whether this risk is acceptable before using this project.**

---

## Security

* `.env`, `data/browser-profile/`, `data/state.json`, `data/debug/`, `*.har`
  and all key material (`*.dat`, `*.key`, `*.jks`, `*.p12`, `*.pfx`) are
  gitignored.
* The file-based signing key is read from `IDGOV_SIGNING_KEY_PATH` and never
  copied into the repository.
* `IDGOV_SIGNING_KEY_PASSWORD`, key file contents, the Telegram token, cookies,
  authorization headers and session tokens are never logged. A redaction filter
  on the root logger scrubs both known secret values and credential-shaped
  patterns, and every debug artifact passes through it.
* `IDGOV_SIGNING_KEY_PATH` and `IDGOV_SIGNING_KEY_PASSWORD` are validated
  before the automatic login opens ID.GOV.UA, and the key file is handed to
  Playwright's `set_input_files()` — the native macOS file picker is never
  automated.
* Automatic authentication reads and writes nothing but the browser profile.
  No cookie, token or CAPTCHA is ever manipulated, and the ID.GOV.UA signing
  component is never emulated.
* The persisted HSC session is encrypted with Fernet (authenticated encryption)
  before it reaches MongoDB. Plaintext cookie values never touch the database,
  and a document written with a different key — or edited — is refused rather
  than decrypted.
* `HSC_SESSION_ENCRYPTION_KEY` and `HSC_MONGODB_URI` are registered as secrets
  with the redaction filter, so neither the key nor the database credentials can
  appear in a log line. Connection logging shows `mongodb+srv://***@host/`.
* Every HSC API call is a GET. There is no `post`/`put`/`patch`/`delete` anywhere
  in the API package, and tests enforce it — nothing in this project can book,
  reserve or cancel an appointment.
* The environment carries secrets and nothing else: six variables, listed in
  `.env.example`. A committed YAML file that mentions a credential-shaped key is
  **refused at load time** rather than quietly ignored, so a secret cannot drift
  into version control as a setting that appears to do nothing.
* `TELEGRAM_USERS` is treated as sensitive — Telegram ids identify people. It is
  a GitHub Environment secret, never a repository variable, and it is registered
  with the redaction filter; delivery logs show only a masked id (`***6789`) and
  the `sendMessage` URL, which carries the token in its path, is never logged.

---

## Project layout

```
config/          app.yaml, selectors.yaml, flow.yaml, service_centers.yaml
docs/            authentication and selector-discovery guides
src/hsc_queue_monitor/
  cli.py         commands
  config.py      secrets (env) + YAML loading and validation, kept apart
  models.py      LocatorSpec, ServiceCenter, availability results, exceptions
  logging_config.py  redaction filter, armed from the configured secrets
  api/           read-only HSC client, retry policy, headless monitor,
                 encrypted session store, monitor state, availability snapshot
  notifications/ outbound-only Telegram, Ukrainian templates, dispatcher
  browser/       persistent context, screenshots, element dumps, events
  pages/         page objects (no selector strings); ui_text.py parses UI strings
  flow/          auth guard, step registry, interactive runner, availability scan
  monitor/       local browser polling loop + seen-slot state
  notification/  the local browser monitor's console/Telegram notifiers
tests/
scripts/         macOS Accessibility helpers for the native file dialog
data/            browser profile, debug artifacts, state (all gitignored)
legacy/          previous API-based prototype, kept for reference
```

The two Telegram packages are not a duplication to tidy away: `notifications/`
is the headless, outbound-only path with its own boundary tests, while
`notification/` belongs to the older local browser `monitor` command.

## Validation

```bash
pip install -e ".[dev]"

pytest
ruff check .
mypy --strict src
```

No browser is required: the tests use duck-typed Playwright stand-ins, and the
suite makes no network request of any kind.

---

## `legacy/`

An earlier prototype that called the site's JSON API directly. It is kept only
for reference, is excluded from linting and type checking, and is not importable
as part of this package.
