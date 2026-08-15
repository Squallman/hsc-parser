# Configuring the HSC UI flow

The development loop for discovering selectors and teaching the browser path a
new screen. Nothing here is needed to *run* the monitor — see the
[README](../README.md#main-commands) for that.

---

This is the intended development loop. Work **one screen at a time** — do not
try to fill in the whole file up front.

### 1. Install the project

See [First run](../README.md#first-run) in the README.

### 2. Launch inspect mode

```bash
python -m hsc_queue_monitor.cli inspect
```

A headed Chromium opens with the persistent profile from
`data/browser-profile/`. The session survives between runs, so you normally log
in only once.

### 3. Navigate manually to the screen you care about

Click through the site yourself in the browser window. The terminal waits.

### 4. Inspect the visible interactive elements

Press ENTER in the terminal. Every visible interactive element is written to
`data/debug/page-elements.json` and the first 40 are printed:

```json
[
  {
    "tag": "button",
    "role": "button",
    "text": "Практичний іспит",
    "aria_label": null
  }
]
```

Password field values and file input contents are never collected.

### 5. Determine a stable locator

Prefer, in this order:

1. **role + accessible name** — `get_by_role("button", name="Практичний іспит")`
2. **label** — for form fields
3. **test id** — `data-testid`, when the site provides one
4. **stable visible text** — user-facing wording that is unlikely to change
5. **stable CSS selector** — a semantic class or attribute, as a last resort

Avoid: generated CSS classes (`css-1x7fj2n`), long DOM paths, XPath,
`nth-child`, and dynamically generated ids. If you truly cannot avoid an
ambiguous selector, set `nth:` explicitly rather than hoping the first match is
right — the code will never silently pick one for you.

### 6. Put it into `config/selectors.yaml`

```yaml
exam:
  practical_exam:
    strategy: role
    role: button
    name: "Практичний іспит"
```

### 7. Test it

```bash
python -m hsc_queue_monitor.cli test-step exam.practical_exam
```

`test-step` **drives itself to the right screen first**. It reads the target's
entry under `steps:` in `flow.yaml`, opens the configured `start_url`, clicks
each prerequisite in order, then stops — the target itself is never touched:

```
PREPARE 1/1: queue.start_registration
  Locator: get_by_role("link", name="Записатись у чергу")
  Result: OK
  URL: https://eqn.hsc.gov.ua/cabinet/queue/…

Selector: exam.practical_exam
Locator:  get_by_role("button", name="Практичний іспит")
URL:      https://eqn.hsc.gov.ua/cabinet/queue/…

PASS: selector matched exactly one visible element
      [0] <button> role='button' text='Практичний іспит'
```

Prerequisites go through the same page-object click path as the real flow, so a
broken one fails immediately with a screenshot in `data/debug/errors/` and tells
you which link in the chain to fix:

```
PREPARE 1/2: queue.start_registration
  Locator: get_by_role("link", name="Записатись у чергу")
  Result: FAILED
  The target was never reached. Fix this prerequisite first —
  `test-step queue.start_registration` validates it on its own.
```

To navigate by hand instead — useful for a screen you have not mapped yet:

```bash
python -m hsc_queue_monitor.cli test-step exam.practical_exam --manual-prepare
```

Defining the chain in `config/flow.yaml`, keyed by **selector** key:

```yaml
steps:
  exam.practical_exam:
    start_url: "https://eqn.hsc.gov.ua/cabinet"
    prerequisites:
      - queue.start_registration

  category.category_a:
    prerequisites:
      - queue.start_registration
      - exam.practical_exam        # full chain, listed explicitly
```

The list is executed verbatim and in order — it is not expanded transitively,
so each entry spells out its own ancestors. A DYNAMIC prerequisite (the service
centre card) takes its value from `--service-center "<name>"`.

or

```
FAIL: selector matched 4 elements
      [0] <div> text='Практичний іспит'
      [1] <button> role='button' text='Практичний іспит'
      ...
```

Nothing is clicked unless you pass `--click`:

```bash
python -m hsc_queue_monitor.cli test-step exam.practical_exam --click
```

### 8. Run the flow interactively

```bash
python -m hsc_queue_monitor.cli flow
```

Before each step you get a preview and a prompt:

```
STEP 3/5: select practical exam
  Selector: exam.practical_exam
  Locator:  get_by_role("button", name="Практичний іспит")
  Press ENTER to execute or type "s" to skip:
```

After each step the URL is printed and a screenshot is saved to
`data/debug/003-practical_exam.png`.

To debug a later part of the flow without redoing the earlier screens:

```bash
python -m hsc_queue_monitor.cli flow --from category.category_a
python -m hsc_queue_monitor.cli flow --no-login
```

### 9. Repeat for the next screen

Each screen: inspect → write selector → `test-step` → `flow`.

### 9a. Check one service centre

The service-centre screen has its own command, because "is this centre
bookable right now?" is the question the whole project is built around:

```bash
python -m hsc_queue_monitor.cli check-center 3242
```

It runs the prerequisite chain configured for `department.search`
(`start_registration → practical_exam → service_center_vehicle → category_a`),
waits for the service-centre screen to actually arrive, types the **ID** into
the search box, finds the one button that identifies that ID and reports it:

```
SERVICE CENTER CHECK

ID:         3242
Name:       ТСЦ МВС № 3242
Found:      yes
Disabled:   false
Available:  YES

Full text:
ТСЦ МВС № 3242 м. Біла Церква, вул. Сухоярська 20
```

Exit codes: `0` the centre was found (available **or not**), `1` navigation or
search failed, `2` the ID is not in `config/service_centers.yaml`. Availability
is an observation, never a process failure.

Identity is the **ID**, not the address: `3242` is matched with digit
boundaries, so it never resolves to `13242`, and the site may reword the
address freely. Zero matches and several matches are both hard errors — the
code never falls back to `.first()`.

**The screen after category A is waited for, not assumed.** Clicking
«категорія А» returns while HSC is still showing its spinner, with the category
buttons left mounted underneath it — so for a moment the service-centre search
box genuinely is not in the DOM. Both paths that go through that transition (the
`category_a` step, which `flow` and `monitor` run, and `check-center` with or
without `--click`) therefore wait for the destination state: `department.search`
visible, polled, with `timeouts.navigation` (30s) as the budget rather than the
per-locator 15s. The selector comes from the registry — the placeholder text is
not repeated anywhere in flow code.

If that screen never arrives, the failure says so:

```
FlowError:
Timed out waiting for the service-centre screen after selecting category A (30s).
department.search never became visible, and the browser is on …
This is a slow or stalled transition, not a wrong selector — …
Saved for inspection:
  elements:   data/debug/007-department-screen-timeout-elements.json
  screenshot: data/debug/007-department-screen-timeout.png
```

It used to surface as `LocatorNotFound: department.search matched 0 visible
elements`, which sent you off to re-check a selector that had been right all
along. Nothing is typed or clicked on the way to this error.

`--click` selects the centre (only when its button is enabled), then stops:

```bash
python -m hsc_queue_monitor.cli check-center 3242 --click
```

```
Clicking service center 3242...

URL after click:
https://eqn.hsc.gov.ua/cabinet/queue

Screenshot:
data/debug/005-check-center-3242-after-click.png

Elements:
data/debug/check-center-3242-after-click-elements.json

Stopped after service-center selection.
No date or time was selected.
```

The URL usually does not change — this is an SPA, so read the screenshot and
the element dump instead. Both are uniquely named, so they survive the next
`inspect` run and are what you use to write the calendar selectors.

### 9b. Scan for real availability

`check-center` answers "can this centre be opened". The question the project
exists for is "is there a free time", and those are not the same thing:

```bash
python -m hsc_queue_monitor.cli check-availability
python -m hsc_queue_monitor.cli check-availability --center 3242 --center 4641
```

For each centre it opens the wizard, reads every enabled day, opens each of them
in turn and reads the free times:

```
AVAILABILITY

ТСЦ МВС № 3242
  2026-08-21
    09:20
    10:40

  2026-08-26
    14:00

ТСЦ МВС № 4641
  no available dates

1 of 2 centre(s) have at least one free time.

Nothing was booked: no time was selected and no form was submitted.
```

**It reads and it stops.** The scan walks as far as the «Час» step, writes down
the times and goes back. It never selects a time, never continues to
«Контакти», never fills in a phone number and never submits anything —
`pages/time_page.py` has no method that could, and tests assert both that the
API has none and that the scanner's own source contains no such call. Anything
that would actually book an appointment is a person's decision, made in the
browser window.

**Availability means a free time, not an enabled card.** The rule the project
uses is: *at least one enabled date carrying at least one enabled time*. A
centre's card being clickable is only an optimisation — a disabled card is never
opened — and `check-center` says so in its own output. The report distinguishes
all four ways of having nothing:

| Report line | Meaning |
|---|---|
| `not on the service-centre screen` | the search did not turn the centre up at all |
| `unavailable — the centre's button is disabled` | on screen, not openable; not clicked |
| `no available dates` | opened, and every day in both months is disabled |
| `no available times` (under a date) | the day was open, every time on it is taken |

**1 to 5 centres, from configuration.** The scan covers the enabled entries of
`config/service_centers.yaml`; `--center` (repeatable) replaces that list for
one run and still resolves IDs through the same file. No centre ID appears
anywhere in the source. Zero centres and more than five are both refused before
the browser opens — five is where "check what is free" turns into sustained
traffic, which this project is not.

**Two months, parsed independently.** The calendar renders «Серпень 2026» and
«Вересень 2026» side by side and their day numbers overlap, so a day is only
ever addressed inside its own month container: the container's caption gives the
year and month, the day button gives the day, and the full `YYYY-MM-DD` is built
from both. Nothing counts containers or assumes which one comes first, and
`select_date()` resolves the month before it looks for the number — a page-wide
lookup for "1" would match twice.

Between dates the scan steps back exactly one screen with the site's own Back
control (`wizard.back`), waits for the calendar to be ready again and continues.
That selector is still `TODO`, so until it is filled in from a real element dump
the scan re-walks the wizard from the cabinet instead — slower, always correct,
and it never re-authenticates: the session guard runs once per scan and is
idempotent.

`calendar.month` is the one selector this feature cannot work without and the
one that needs a live dump — it must match a block containing a month's caption
*and* its day buttons. `calendar.day` and `time.slot` are deliberately the
generic `button`, filtered in Python by what the site itself renders: a label
that is exactly a day number or exactly a time, and the control's enabled state.

### 10. Dry-run the monitor

Once the flow reaches the calendar, list the centres you want in
`config/service_centers.yaml` — `id` is the identity, `name`/`full_name` are
display text — then:

```bash
python -m hsc_queue_monitor.cli monitor --dry-run
```

This navigates and detects slots normally, prints what *would* be sent, and
never contacts Telegram or writes state.

### 11. Configure Telegram

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_USERS` in `.env`, then drop the flag:

```bash
python -m hsc_queue_monitor.cli monitor
```

---
