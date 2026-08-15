# HSC queue monitor

Detects **available appointment dates/time slots** in the Ukrainian HSC electronic queue
(<https://eqn.hsc.gov.ua/cabinet/queue>) and notifies you when availability appears.

It does **not** book anything, does **not** forge cookies and does **not** attempt to bypass
CAPTCHA, WAF, Akamai or any other protection. It drives a real Chromium instance with a
persistent profile; the browser owns authentication, cookies, session rotation and anti-bot
state, and all API calls are plain same-origin `fetch()` calls executed inside that
authenticated page.

```
real Chromium → normal website session → browser-managed cookies → same-origin fetch
              → availability parser → change detection → notification
```

## Status

| Piece | State |
| --- | --- |
| `login` | ✅ works |
| `departments --service-id 47` | ✅ works (`/api/v2/equeue/departments`) |
| `inspect` | ✅ works (records `/api/v2/equeue/` traffic) |
| `monitor` | ⚠️ runs, but stops with a clear message until the date/slot endpoints are discovered |

The endpoints for *available dates* and *available time slots* have **not** been observed yet,
so `HscApiClient.get_available_dates()` / `get_available_slots()` raise
`EndpointNotDiscoveredError` instead of guessing URLs. See
[Network discovery](#network-discovery) below — that is the next step.

## Installation

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
playwright install chromium
```

For development (tests, lint, types):

```bash
pip install -e ".[dev]"
```

Optionally copy the sample configuration:

```bash
cp .env.example .env
```

## First authentication

```bash
python -m hsc_queue_monitor.cli login
```

A visible Chromium window opens on the queue page.

1. **Sign in yourself** (Diia / id.gov.ua / BankID — whatever the site offers).
2. **Complete any browser verification yourself** if the site shows one.
3. Leave the window open until the CLI prints `Authenticated session detected`.

Nothing about your credentials is automated, stored or transmitted by this project. The session
lives only inside the Playwright profile at `data/browser-profile/`, and later runs reuse it.
Add `--keep-open` if you want to keep browsing after the session is detected.

Session detection is deliberately shallow: it checks only whether a session **cookie name**
(e.g. `__Secure-auth.access-token`) exists and that the page is not a login screen. Cookie
values are never read.

## Departments

```bash
python -m hsc_queue_monitor.cli departments --service-id 47
python -m hsc_queue_monitor.cli departments --service-id 47 --json
```

Calls `GET /api/v2/equeue/departments?serviceId=47` from inside the authenticated page and
prints `id`, name, region/city/street/building/office and `allowOnlineCount` when present.
The service id is configurable everywhere (`--service-id`, `HSC_SERVICE_ID`); `47` is only the
development default.

## Network discovery

```bash
python -m hsc_queue_monitor.cli inspect
```

Then, in the open window, walk the booking flow **by hand**:

1. select the service;
2. select the department;
3. open the date selection;
4. look at the available dates;
5. open the time slots.

Do **not** confirm a booking — only navigate. Every request whose URL contains
`/api/v2/equeue/` is appended to:

```
data/network-events.jsonl
```

One JSON object per line:

```json
{"ts":"2026-08-11T20:40:03.114+00:00","type":"response","method":"GET",
 "url":"https://eqn.hsc.gov.ua/api/v2/equeue/departments?serviceId=47",
 "path":"/api/v2/equeue/departments","query":{"serviceId":"47"},"status":200,
 "response_content_type":"application/json","response_body":{"...":"..."}}
```

On exit the command prints a summary of every distinct `METHOD /path` it saw.

### Handing the capture over

Skim `data/network-events.jsonl` first and confirm it contains no personal data (see
[Security](#security)), then share it. With that file, the two placeholder methods in
`hsc_queue_monitor/api.py` can be implemented:

- `get_available_dates(department_id, service_id=..., date_from=..., date_to=...)`
- `get_available_slots(department_id, date, service_id=...)`

Both already have parsing helpers ready (`AvailableDate.from_api`, `AvailableSlot.from_api`,
`unwrap_list`) which tolerate unknown/renamed fields. Endpoint names alone are not enough —
the recorded request/response *shapes* are what the implementation is based on.

## Monitoring

```bash
python -m hsc_queue_monitor.cli monitor
python -m hsc_queue_monitor.cli monitor --department-ids 8041,8042 --interval 90
python -m hsc_queue_monitor.cli monitor --once          # a single cycle
```

Until the availability endpoints are implemented this command authenticates, loads the
departments and then stops with:

```
get_available_dates() is not implemented yet: the dates endpoint under /api/v2/equeue/ has not
been observed. Run `python -m hsc_queue_monitor.cli inspect`, ...
```

Once implemented, the loop:

- polls every `HSC_POLL_INTERVAL_SECONDS` (default **60 s**) with **±10 s random jitter**;
- never polls faster than `HSC_MIN_POLL_INTERVAL_SECONDS` (default **30 s**);
- runs **one** loop per browser session and queries departments **sequentially** with a small
  delay between requests — no request fan-out;
- backs off exponentially on `429`, and on `401/403` logs that the browser session needs to be
  re-authenticated instead of hammering the API;
- notifies only about **new** slots (`A,B` → `A,B,C` notifies about `C` only) and about dates
  that flip from unavailable to available.

Add `--inspect-network` to also record API traffic while monitoring.

### Notifications

`ConsoleNotifier` is always on:

```
NEW HSC APPOINTMENT AVAILABLE

Service: 47
Department: [8041] ТСЦ 8041 — м. Київ, Київ, вул. Набережно-Хрещатицька, 27
Date: 2026-08-20
Time: 10:40
```

Telegram is optional and enabled only when **both** variables are set:

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

The bot token is never logged, printed or included in error messages.

## Configuration

All settings come from the environment (`.env` supported) and can be overridden per run; see
[`.env.example`](.env.example) for the full list.

| Variable | Default | Meaning |
| --- | --- | --- |
| `HSC_SERVICE_ID` | `47` | Service to watch |
| `HSC_DEPARTMENT_IDS` | *(empty = all)* | Comma-separated department ids |
| `HSC_POLL_INTERVAL_SECONDS` | `60` | Polling interval |
| `HSC_POLL_JITTER_SECONDS` | `10` | Random ± jitter |
| `HSC_MIN_POLL_INTERVAL_SECONDS` | `30` | Hard floor for the interval |
| `HSC_DATE_FROM` / `HSC_DATE_TO` | *(empty)* | Inclusive date-range filter |
| `HSC_HEADLESS` | `false` | Headed by default; `--headless` per command |
| `HSC_PROFILE_DIR` | `data/browser-profile` | Playwright persistent profile |
| `HSC_AUTH_TIMEOUT_SECONDS` | `600` | How long to wait for manual login |
| `HSC_LOG_LEVEL` | `INFO` | Logging level |

`login`, `departments` and `inspect` default to **headed** mode, `monitor` to headless (it can
only work headless once a session already exists in the profile — run `login` first).

## State

Non-sensitive monitoring state is persisted in `data/state.json`:

```json
{
  "version": 1,
  "last_check_at": "2026-08-11T20:41:04+00:00",
  "seen_slots": ["47|8041|2026-08-20|10:40"],
  "date_availability": {"47|8041|2026-08-20": true}
}
```

No cookies and no tokens are ever written there. Entries for past dates are pruned
automatically. Ctrl+C (SIGINT/SIGTERM) stops the loop, saves the state and closes Playwright
cleanly, leaving the browser profile intact.

## Security

- `data/browser-profile/` contains a **live authenticated session**. Treat it like a password:
  never commit it, never share it, never copy it to another machine.
- HAR files and raw browser exports contain **access tokens and cookies**
  (`__Secure-auth.access-token`, `__Host-next.equeue-session`, `bm_*`, `_abck`, …). Never
  commit them; `*.har` is gitignored.
- The network logger redacts sensitive headers (`Cookie`, `Set-Cookie`, `Authorization`,
  `X-CSRF-Token`, …) and any JSON/query key matching token/session/csrf/secret/credential and
  similar patterns before writing to disk. Still, review a capture before sharing it — payloads
  may include personal data (name, phone, document numbers).
- Logs contain method, path, status and a short body snippet — never cookies or tokens.
- Nothing in this project forges, generates or copies cookies, and nothing tries to defeat
  anti-bot protection. If the API answers `401/403`, the fix is to run `login` and sign in
  again by hand.
- Delete stale captures when you no longer need them: `rm data/network-events.jsonl`.
- ⚠️ The pre-existing `main.py` in this directory hardcodes a real
  `__Secure-auth.access-token`. It is unused by this package — delete it (`rm main.py`) and
  treat that token as compromised; it is excluded from lint only so `ruff check .` stays quiet.

## Development

```bash
pytest                       # tests never touch the real HSC website
ruff check . && ruff format --check .
mypy hsc_queue_monitor
```

Tests cover department JSON parsing, date/slot models, new-slot detection, duplicate
suppression, state persistence, HTTP response handling (200/401/403/429/5xx/non-JSON) and
redaction of sensitive headers, bodies and query parameters — all against fakes.

### Layout

```
hsc_queue_monitor/
  config.py          Settings (env + CLI overrides)
  models.py          Department, AvailableDate, AvailableSlot, MonitorState
  browser.py         persistent Chromium context, auth detection/waiting
  api.py             HscApiClient — fetch() inside the page, retries, backoff
  network_logger.py  /api/v2/equeue/ capture with redaction
  monitor.py         QueueMonitor polling loop + change detection
  notifier.py        Notifier, ConsoleNotifier, optional TelegramNotifier
  cli.py             login / departments / inspect / monitor
```

## Assumptions

- Authentication is considered present when a session cookie *name* exists and the current URL
  is not a login/verification page; the values are never inspected.
- Department payload field names may vary between deployments, so parsing checks several known
  aliases (`name`/`title`, `city`/`settlement`, …) and keeps the raw record in `Department.raw`.
- `monitor` treats a date as worth expanding into slots only when the date payload marks it
  available (or reports a non-zero free count).
- Any endpoint not directly observed in real traffic is left unimplemented on purpose.
