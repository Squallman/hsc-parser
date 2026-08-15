# Authentication

How the local file-based ID.GOV.UA electronic-signature journey works, how it
recovers, and how its selectors were discovered. Operational setup is in the
[README](../README.md#configuration); this is the detail behind it.

---

The HSC session expires quickly. When it does, the site redirects
`https://eqn.hsc.gov.ua/cabinet` to `https://eqn.hsc.gov.ua/` — and a command
that just carried on would fail with a confusing "`queue.start_registration`
matched 0 elements".

Instead, **every command that works inside the cabinet recovers the session by
itself**. There is no login command to remember to run first:

```bash
python -m hsc_queue_monitor.cli check-center 3242
```

```
INFO  Authentication session is not active
INFO  Starting ID.GOV.UA authentication
INFO  ID.GOV.UA authentication completed
INFO  HSC authenticated session established
… check-center continues normally
```

A session is considered authenticated only when **both** are true: the URL is
under `/cabinet`, *and* `login.authenticated_marker` is visible. When it already
is, the guard costs one marker check and does not touch a single login control.

The journey it walks when it is not:

```
HSC, signed out
  → tick "Я ознайомлений та погоджуюсь з умовами надання послуги"   login.terms
  → id.gov.ua                                                       login.idgov
ID.GOV.UA
  → "Електронного підпису"                          login.electronic_signature
  → "Файловий"                                                   login.file_tab
  → choose the КНЕДП (authentication.key_provider)               login.provider
  → click the visible "choose a file" control            login.key_file_trigger
  → choose IDGOV_SIGNING_KEY_PATH in the OS Open dialog        (NativeFileSelector)
  → wait until the key is accepted                             login.key_loaded
  → type IDGOV_SIGNING_KEY_PASSWORD                                        login.password
  → wait until "Продовжити" is visible and enabled                 login.submit
  → submit                                                         login.submit
  → wait out the key-reading overlay                          login.processing
  → confirm "Перевірте дані"                            login.user_data_accept
HSC callback
  → /cabinet  →  login.authenticated_marker
```

Each arrow is a separate, configurable selector — login is never one opaque
click. Every wait is a condition (a locator, an enabled state, a host change),
never a fixed `sleep`.

**The key provider comes before the upload.** ID.GOV.UA defaults its КНЕДП
dropdown to «КНЕДП ДПС» and reads the `.dat` according to whatever is selected,
so the provider that issued your key must be chosen first. Which provider that
is lives in `config/flow.yaml` — it is configuration, not a secret, so it is
deliberately not an environment variable:

```yaml
authentication:
  key_provider: 'КНЕДП "MASTERKEY" ТОВ "АРТ-МАСТЕР"'
```

It is matched against the option text ID.GOV.UA shows. A value the dropdown does
not offer stops the journey before the upload and lists what it does offer.

**The key must arrive through a real OS file selection.** Five A/B runs against
the live site, each changing one variable:

| Run | Upload mechanism | Result |
|---|---|---|
| 1 | `set_input_files()` on `#PKeyFileInput` | key read → form resets |
| 2 | visible control + Playwright `FileChooser` | key read → form resets |
| 3 | manual provider selection, rest automated | same reset — provider ruled out |
| 4 | manual password entry, rest automated | same reset — password ruled out |
| 5 | visible control + **real macOS Open panel** | proceeds to signer information |

So production clicks `login.key_file_trigger` ("оберіть його на своєму носієві")
and then drives the operating system's own Open dialog. Whatever the signing
code depends on lives in the browser's native file-picking path; every shortcut
around it lets the site read the key and then throw the attempt away.

`page.expect_file_chooser()` is **not** used on this path — arming that listener
is what makes Playwright intercept the chooser and suppress the native panel, so
the two mechanisms are mutually exclusive. A test asserts the native path never
arms it.

The OS is driven from `browser/native_files.py` alone, behind a
`NativeFileSelector` protocol; `LoginPage` contains no AppleScript and no
`sys.platform`. On macOS: wait for the Open panel → ⌘⇧G → poll until the
"Go to the folder" path control *exists* → write the path into its accessibility
value → read it back and verify → Return. Every step is a condition poll, with
no `delay` and no fixed sleeps.

Two things that look like details and are not:

- **The path is written, not typed.** Typing lost it: the leading `/` opens the
  Go to Folder sheet, and everything sent while that sheet is still appearing
  goes nowhere — seen live as a field containing exactly `/` and a panel that
  then sat open until the timeout. The value is read back before Return, so a
  half-filled field fails with what it actually contained instead of hanging.
- **The control is found by role, never by focus.** Chrome for Testing reports
  `AXFocusedUIElement` as nothing at all while the field is visibly active, so
  waiting for focus waits forever. Sheets are searched deepest-first (the Go to
  Folder sheet sits on top of the panel, which has a search field of its own)
  and children are matched on `AXTextField` / `AXComboBox` / `AXSearchField`
  rather than by position.

It needs Accessibility permission for your terminal
(System Settings → Privacy & Security → Accessibility); a missing grant is
reported as exactly that.

```yaml
authentication:
  file_selection: native      # or: chooser, to repeat the A/B comparison
  browser_process: Chromium   # "Google Chrome" if you launch a channel build
```

`native` never degrades into `chooser`. On a platform with no native
implementation the run stops with a configuration error naming the platform,
because the chooser path is *known* not to authenticate — silently substituting
it would turn "not supported here" into a full journey that resets at the key
form for no visible reason. Linux/CI support is not built yet.

Two safety rules, both enforced by tests:

- **In `chooser` mode the chooser is verified before it is answered.** The
  screen carries a second file input (`#ChoosePKCertsInput`, for certificates);
  the chooser's element must match `login.key_file` (`#PKeyFileInput`) or the
  run fails without uploading. `input[type="file"]` is ambiguous there — the id
  distinguishes them, never an `nth:`.
- **There is no in-page fallback.** If the OS dialog cannot be driven, it saves
  diagnostics and raises. Falling back to `set_input_files()` would silently
  restore the known-broken behaviour, which fails three screens later where
  nothing explains it.

**Handing over the file is not the same as the key being read.** ID.GOV.UA
parses the `.dat` asynchronously, and typing into a form that is still bound to
no key produces a submit that silently does nothing. So each step waits for the
state the previous one should have produced — all condition polls, no `sleep`:

| After | Wait for |
|---|---|
| `set_input_files` | `login.key_loaded` ("Завантажити інший файл") becomes visible |
| `fill` password | `login.submit` is visible *and* enabled |
| click submit | the callback, the «Перевірте дані» screen, a rejection, or the timeout |

If the key is never accepted, the run stops there — before the password is
typed — and saves a screenshot plus a sanitized element dump, naming both paths
in the error.

**Nothing is classified while the key is being read.** ID.GOV.UA covers the
file-key form with a dimmer ("Зчитування особистого ключа") and leaves the form
*mounted underneath it* — so mid-processing, `login.password` is present and
`login.submit` reports itself enabled. Neither is an outcome. `login.processing`
is what tells work-in-progress from a result, and the screen is only judged once
that marker has gone:

```
submit clicked
  → login.processing visible   → keep waiting, classify nothing
  → login.processing gone      → now decide
```

| Outcome | Detected as | Result |
|---|---|---|
| A | host changes back to `eqn.hsc.gov.ua` | success (outranks everything, even mid-processing) |
| B | overlay gone, ID.GOV.UA on an *unknown* screen | `AuthIntermediateScreenReached` — stop, capture, click nothing |
| C | `login.auth_error` visible | `AuthenticationFailed` with what the page said |
| D | overlay gone, key form back and stable for 3 polls | `AuthenticationFailed` — the attempt was reset |
| E | overlay never clears | `AuthenticationProcessingTimeout` |
| F | `#btnAcceptUserDataAgreement` on screen | confirm it once, then keep waiting for the callback |

**F is a success, not an interruption.** A key ID.GOV.UA accepted lands on
«Перевірте дані», where it shows the name, tax number and address it read out of
the certificate and asks for them to be confirmed. Automation clicks
`login.user_data_accept` — that exact id, once — and goes back to waiting for
the callback. It used to be reported as B, which was true only in the sense that
this project had no step for it.

Three things about that click are deliberate:

- **The id, not the name.** Both this screen and the file-key form carry a
  «Продовжити», so `login.submit` is not reused and no generic button locator or
  `nth:` is involved. `login.user_data_screen` («Перевірте дані») is configured
  as a second opinion only: a heading is wording, an id is identity.
- **Never `#btnResetUserDataAgreement`.** «Відмовитись» sits next to it and
  abandons the authentication.
- **Once.** A screen still there after being confirmed is a callback that did
  not arrive; it fails on the normal authentication timeout with the full
  artifact set, saying what it got through rather than reading like a rejected
  key.

Confirming it is still not evidence of a session: success continues to require a
URL under `/cabinet` *and* `login.authenticated_marker`, checked afterwards by
`AuthManager` exactly as before.

The personal data on that screen is never logged — ordinary output names the
screen semantically ("the user-data confirmation screen") and nothing else. The
failure artifacts capture the page structurally, as they do for every other
screen.

D additionally requires that processing was *observed* first: a form that never
went busy is a submit that did nothing, and calling that a rejection is a false
positive. Without the evidence the run fails as a timeout with the screen saved
— a false negative costs one slow failure, a false diagnosis costs an afternoon.

`login.processing` is `optional:`, so an unset value degrades to the older,
weaker "something re-rendered" signal rather than breaking the journey.

`login.auth_error` is deliberately still `TODO` — no real rejection has been
captured, and a guessed wording would be a detector that never matches.

**No cause is inferred.** Wrong password, wrong provider, bad key and an expired
certificate all look identical from the DOM, so the failure says what happened
and hands over evidence rather than naming a suspect.

### What gets captured, and what never does

Throughout the ID.GOV.UA phase an observer records, per step (`provider`,
`upload`, `submit`), into `data/debug/auth/post-submit-<timestamp>-*`:

| File | Contents |
|---|---|
| `…-text.json` | distinct sanitized visible-text snapshots (bounded ring buffer) — catches messages that are only on screen for a moment |
| `…-console.json` | console errors/warnings and page errors |
| `…-network.json` | per id.gov.ua response: method, host, path, status, content-type — plus which step provoked it |
| `…-network.json` → `responses_by_phase` | zero-filled over every step that ran (`idgov`, `provider`, `upload`, `submit`, `processing`), so "no traffic" and "step never ran" are distinguishable |
| `…-elements.json` | the full sanitized interactive-element dump |
| `….png` | the screen |

Never recorded, at any verbosity: request/response bodies, headers, cookies,
query-string values (`code`, `state`, tokens — dropped whole from network
records and redacted in any URL written), page HTML, the `.dat`, or the
password. Everything goes through the redactor on the way to disk.

The per-step network breakdown is also what answers whether changing
`#CAsServersSelect` kicks off its own request. No wait was added for it — there
is no evidence yet that one is needed, and a wait invented on a hunch is
indistinguishable from a sleep. If `provider` shows up in `responses_by_phase`,
that is the grounds to add one.

**An interrupted attempt is recoverable.** A dead sign-in can leave the OIDC
hand-over in flight, so opening `/cabinet` redirects straight to
`id.gov.ua/?response_type=code&…`. That is treated as "authentication required",
not "unexpected page": the browser goes back to the HSC entry page and walks the
normal journey once. The stale authorization screen is never driven, and there
is no second restart.

**Recovery happens at most once per operation.** If authentication succeeds but
the cabinet still does not come up authenticated, the run fails with
`AuthenticationFailed` and saves diagnostics rather than looping.

Two diagnostic commands:

```bash
python -m hsc_queue_monitor.cli auth-status    # look only, never logs in
python -m hsc_queue_monitor.cli ensure-auth    # log in, stop at /cabinet
```

**Temporary A/B diagnostics** for the post-processing form reset. Each hands
exactly one step to a human and leaves everything else automated, so a run
differs from `ensure-auth` in one variable only. Neither affects production.

```bash
python -m hsc_queue_monitor.cli ensure-auth-debug-provider    # you pick the КНЕДП
python -m hsc_queue_monitor.cli ensure-auth-debug-password    # you type the password
python -m hsc_queue_monitor.cli ensure-auth-debug-native-ax   # dump the AX tree
```

`ensure-auth-debug-provider` — ID.GOV.UA wraps `#CAsServersSelect` in
jquery.nice-select, so the native element automation drives is not the control a
person touches. **Result: identical reset, so provider selection is ruled out.**

`ensure-auth-debug-password` — stops after `login.key_loaded` and waits for you
to type into «Пароль». `IDGOV_SIGNING_KEY_PASSWORD` is never filled and
`fill(secret=True)` is never called. The typed value is never read back: a
browser-side boolean (`value.length > 0`) is the only thing that crosses into
Python, so the process cannot leak what it does not hold. Everything after the
password — submit, processing, callback, diagnostics — is the production path.

`ensure-auth-debug-native-ax` — runs the journey to the key-file screen, opens
the real macOS Open dialog, sends ⌘⇧G and then dumps the browser process's
*actual* accessibility tree to `data/debug/native-ax-<timestamp>.json`. It
selects no file, types nothing, never presses Return, and leaves the dialog open
so the artifact matches what is on screen. Written because two heuristics have
now failed against this dialog — `AXFocusedUIElement` reports nothing, and the
roles we expected were not where we expected them — and the next move is to look
rather than guess again. The walk assumes no sheet index, no child index and no
role, is bounded by depth and element count, skips `AXWebArea` subtrees (page
content), never reads a secure field's value, and passes every string through
the redactor.

Delete all three once the questions are answered.

### scripts/mac_ax_inspector.py

A standalone local debug tool — nothing in `src/` imports it, and a test
enforces that. It inspects the macOS Accessibility hierarchy of a running GUI
process so we can stop guessing how the native Go to Folder field is
represented.

```bash
python scripts/mac_ax_inspector.py                          # processes, with PIDs
python scripts/mac_ax_inspector.py --pid 62868 --windows
python scripts/mac_ax_inspector.py --pid 62868 --tree --json data/debug/mac-ax.json
python scripts/mac_ax_inspector.py --pid 62868 --contains "Перейти"
python scripts/mac_ax_inspector.py --pid 62868 --editable-only
python scripts/mac_ax_inspector.py --pid 62868 --focused
python scripts/mac_ax_inspector.py --pid 62868 --ancestry
python scripts/mac_ax_inspector.py --pid 62868 --delay 5 --focused --detail
```

**A control that only exists while focused** won't appear in a static dump at
all: running the inspector from a terminal makes the terminal frontmost, which
moves the panel's focused element off the Go to Folder field. `--delay N` waits
before querying anything, so you can switch back, press ⌘⇧G and leave focus in
the path field. Every element also reports `AXIdentifier` and
`AXPlaceholderValue`, since role alone cannot separate the panel's search field
from its path field — both are `AXTextField`.

Prefer `--pid` over `--process`: several processes can share a name (this
machine runs two called `com.apple.appkit.xpc.openAndSavePanelService`), and
`tell process "<name>"` picks one without saying which — which is how a query
returns zero windows while the dialog is plainly on screen. PID mode goes
through `AXUIElementCreateApplication(pid)` and never resolves the id back to a
name. It needs `pip install -e '.[macos-debug]'` (PyObjC); every other mode
works without it.

**`AXWindows` can be empty even when the panel is right there.** Observed live:
a process with no `AXWindows` whose `AXFocusedUIElement` is an `AXOutline`
carrying `AXWindow` and `AXTopLevelUIElement`. So PID mode falls back —
`AXWindows` → `AXFocusedUIElement.AXWindow` →
`AXFocusedUIElement.AXTopLevelUIElement` — and records which route worked as
`root_source` in the JSON. `--ancestry` walks `AXParent` upward from the focused
element (bounded to 20, with cycle detection via `CFEqual`, since every read
returns a fresh Python object for the same element).

Open the dialog by hand first, press ⌘⇧G, leave it open, then run the inspector
from another terminal.

It never uses AppleScript's `entire contents` — that has already hung this
browser. Python drives a breadth-first walk, asking about one node and its
*direct* child count at a time, addressing nodes by index path (`2.1.3`). Each
subprocess has its own timeout, so a bad element costs one `<query timeout>`
row rather than the run. Bounded by `--max-depth` (12) and `--max-elements`
(500). It lists each element's available attributes and actions and whether
`AXValue` is settable, but **performs no actions** — inspection only.
`AXWebArea` subtrees are recorded and not entered unless `--include-web-area`
is passed, and `AXSecureTextField` values are never read.

```
HSC AUTH STATUS

Authenticated: YES
URL: https://eqn.hsc.gov.ua/cabinet
```

### The ID.GOV.UA signing component

ID.GOV.UA may require its own web-signature browser component. Nothing about
that requirement is emulated or bypassed. If you set `login.signature_unavailable`
to the text the page shows when the component is missing, authentication stops
with `SignatureExtensionUnavailable`, saves a screenshot and a sanitized element
dump, and tells you to install the official component in the persistent profile.
It never quietly falls back to another authentication method.

### Discovering the ID.GOV.UA selectors

The login screens only exist while you are signed **out**, so plain `inspect`
(which starts inside the cabinet) cannot reach them:

```bash
python -m hsc_queue_monitor.cli inspect-auth
```

It opens the public home page, waits, and on ENTER writes a **uniquely
numbered** screenshot + element dump under `data/debug/auth/` — type a label
first to name the capture:

```
inspect> idgov-method
Elements:   data/debug/auth/002-idgov-method-elements.json
Screenshot: data/debug/auth/002-idgov-method.png
```

Nothing is overwritten, so all the screens of one journey survive. Individual
authentication selectors can then be validated the usual way — their chains in
`flow.yaml` start on the public page and are deliberately *not* guarded by the
login recovery, which would sign you in and take the screen away:

```bash
python -m hsc_queue_monitor.cli test-step login.password
```

---
