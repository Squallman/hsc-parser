"""Automatic authentication recovery.

Nothing here touches the real HSC or ID.GOV.UA. :class:`AuthJourney` is a
scripted stand-in for the whole signed-out journey that reproduces the two
behaviours the feature depends on:

* opening ``/cabinet`` without a session redirects to ``https://eqn.hsc.gov.ua/``;
* Playwright's text / accessible-name matching is a substring match unless
  ``exact: true`` is configured.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import (
    FakeConsoleMessage,
    FakeElement,
    FakeFileChooser,
    FakeLocator,
    FakeNativeFileSelector,
    FakePage,
    FakeResponse,
)

import hsc_queue_monitor
from hsc_queue_monitor.browser.diagnostics import Diagnostics
from hsc_queue_monitor.cli import (
    run_check_center,
    run_ensure_auth_debug_native_ax,
    run_ensure_auth_debug_native_file_only,
    run_ensure_auth_debug_password,
    run_ensure_auth_debug_provider,
)
from hsc_queue_monitor.config import (
    AppConfig,
    AppSettings,
    FlowConfig,
    Paths,
    SecretSettings,
    SelectorRegistry,
    load_secrets,
)
from hsc_queue_monitor.flow.steps import FlowContext
from hsc_queue_monitor.models import (
    AuthenticationFailed,
    AuthenticationProcessingTimeout,
    AuthIntermediateScreenReached,
    ConfigError,
    FlowError,
    LocatorAmbiguous,
    SelectorNotConfigured,
    ServiceCenter,
    SignatureExtensionUnavailable,
)
from hsc_queue_monitor.pages.base_page import BasePage
from hsc_queue_monitor.pages.login_page import LoginPage

CABINET = "https://eqn.hsc.gov.ua/cabinet"
SIGNED_OUT = "https://eqn.hsc.gov.ua/"
IDGOV = "https://id.gov.ua/auth?target=hsc"

PASSWORD = "sup3r-s3cret-key-pass"

TERMS_TEXT = "Я ознайомлений та погоджуюсь з умовами надання послуги"
MARKER_TEXT = "Записатись у чергу"
PASSWORD_LABEL = "Пароль захисту ключа"
NO_LIBRARY_TEXT = "Не вдалося встановити зв'язок з бібліотекою підпису"

#: The visible control a person clicks to pick the key file. Clicking it is
#: what makes the page open a file chooser.
KEY_FILE_TRIGGER_TEXT = "оберіть його на своєму носієві"
#: Shown by ID.GOV.UA only once the .dat has actually been read.
KEY_LOADED_TEXT = "Завантажити інший файл"
#: The dimmer ID.GOV.UA shows while it reads the key. Confirmed from the live
#: DOM — and critically, the file-key form stays mounted underneath it.
PROCESSING_TEXT = "Зчитування особистого ключа"
PROCESSING_WAIT_TEXT = "Зачекайте будь ласка"
#: The screen a *successful* signature is supposed to reach.
SIGNER_TEXT = "Інформація про підписувача"

#: The intermediate screen a successful signature really reaches: ID.GOV.UA
#: shows what it read out of the certificate and asks for it to be confirmed.
USER_DATA_TEXT = "Перевірте дані"
#: Measured on the live screen. Both buttons carry ids; only one of them
#: continues the authentication.
USER_DATA_ACCEPT_CSS = "#btnAcceptUserDataAgreement"
USER_DATA_RESET_CSS = "#btnResetUserDataAgreement"
#: Invented stand-ins for the personal data that screen displays. They exist to
#: be asserted *absent* from the logs.
PERSONAL_NAME = "ШЕВЧЕНКО ТАРАС ГРИГОРОВИЧ"
PERSONAL_TAX_ID = "1234567890"
PERSONAL_ADDRESS = "м. Київ, вул. Хрещатик, 1"
#: A rejection message. Not a guess at the real wording — it exists so the
#: not-yet-known `login.auth_error` selector can be exercised at all.
REJECTED_TEXT = "Помилка автентифікації"

#: Where an interrupted attempt leaves the browser: the cabinet redirects
#: straight into an OIDC authorization request instead of the HSC login page.
IDGOV_AUTHORIZE = (
    "https://id.gov.ua/?response_type=code&client_id=hsc"
    "&redirect_uri=https%3A%2F%2Feqn.hsc.gov.ua%2Fcallback"
)

#: The КНЕДП dropdown. ID.GOV.UA pre-selects the tax service's provider, so the
#: default is deliberately *not* the one this key needs.
DEFAULT_PROVIDER = "КНЕДП ДПС"
MASTERKEY_PROVIDER = 'КНЕДП "MASTERKEY" ТОВ "АРТ-МАСТЕР"'
PROVIDER_OPTIONS = [DEFAULT_PROVIDER, MASTERKEY_PROVIDER, "КНЕДП Приватбанк"]

CENTER_3242 = ServiceCenter(
    name="ТСЦ МВС № 3242",
    id="3242",
    full_name="ТСЦ МВС № 3242 м. Біла Церква, вул. Сухоярська 20",
    enabled=True,
)

#: The order the journey must execute in, as recorded by the fake. The provider
#: is chosen before the upload because it decides how the .dat is interpreted,
#: and the file chooser is armed before the control that opens it is clicked.
JOURNEY = [
    "click:terms",
    "click:idgov",
    "click:electronic_signature",
    "click:file_tab",
    "select:provider",
    "click:key_file_trigger",
    # The operating system's Open dialog — the only mechanism ID.GOV.UA has
    # been observed to accept.
    "native:select",
    "upload:key_file",
    "fill:password",
    "click:submit",
]

#: The same journey with Playwright's intercepted chooser instead of the OS
#: dialog. Kept because that mechanism is still selectable for A/B comparison.
CHOOSER_JOURNEY = [
    "click:terms",
    "click:idgov",
    "click:electronic_signature",
    "click:file_tab",
    "select:provider",
    "arm:file_chooser",
    "click:key_file_trigger",
    "upload:key_file",
    "fill:password",
    "click:submit",
]

SELECTORS = f"""
login:
  terms:
    strategy: text
    value: "{TERMS_TEXT}"
    exact: true
  idgov:
    strategy: role
    role: button
    name: "id.gov.ua"
    exact: false
  electronic_signature:
    strategy: text
    value: "Електронного підпису"
  file_tab:
    strategy: text
    value: "Файловий"
  provider:
    strategy: css
    value: '#CAsServersSelect'
  key_file_trigger:
    strategy: text
    value: "{KEY_FILE_TRIGGER_TEXT}"
    exact: true
  key_file:
    strategy: css
    value: '#PKeyFileInput'
    visible: false
  key_loaded:
    strategy: text
    value: "{KEY_LOADED_TEXT}"
    exact: true
  processing:
    strategy: text
    value: "{PROCESSING_TEXT}"
    exact: true
    optional: true
  password:
    strategy: label
    value: "{PASSWORD_LABEL}"
  submit:
    strategy: role
    role: button
    name: "Продовжити"
    exact: true
  user_data_accept:
    strategy: css
    value: '{USER_DATA_ACCEPT_CSS}'
  user_data_screen:
    strategy: text
    value: "{USER_DATA_TEXT}"
    exact: true
    optional: true
  signature_unavailable:
    strategy: text
    value: "TODO"
    optional: true
  auth_error:
    strategy: text
    value: "TODO"
    optional: true
  authenticated_marker:
    strategy: role
    role: link
    name: "{MARKER_TEXT}"
    exact: true
queue:
  start_registration:
    strategy: role
    role: link
    name: "{MARKER_TEXT}"
    exact: true
exam:
  practical_exam:
    strategy: role
    role: button
    name: "Практичний іспит"
    exact: true
  service_center_vehicle:
    strategy: role
    role: button
    name: "Практичний іспит на транспортному засобі Сервісного центру МВС"
    exact: true
category:
  category_a:
    strategy: role
    role: button
    name: "категорія А (механична КПП)"
    exact: true
department:
  search:
    strategy: placeholder
    value: "Пошук сервісного центру МВС"
    exact: true
  department_card:
    strategy: role
    role: button
    name: DYNAMIC
    exact: false
    multiple: true
"""

FLOW = f"""
{{authentication}}
site:
  base_url: "https://eqn.hsc.gov.ua"
  cabinet_url: "{CABINET}"
timeouts:
  default_locator: 200
  navigation: 200
  manual_challenge: 200
  authentication: {{authentication_timeout_ms}}
debug:
  screenshots: false
steps:
  department.search:
    start_url: "{CABINET}"
    prerequisites:
      - queue.start_registration
      - exam.practical_exam
      - exam.service_center_vehicle
      - category.category_a
"""

#: The cabinet controls `check-center` walks through, by accessible name.
CABINET_PREREQUISITES = (
    "Практичний іспит",
    "Практичний іспит на транспортному засобі Сервісного центру МВС",
    "категорія А (механична КПП)",
)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


def element(marker: str, **attrs: Any) -> FakeElement:
    return FakeElement(marker=marker, **attrs)


class ScriptedLocator(FakeLocator):
    """A locator whose interactions drive the page's state machine.

    It also remembers *which* query produced it, so a test can prove not just
    that the right element was clicked but that it was addressed the right way
    — by its id rather than by a "Продовжити" that two screens share.
    """

    def __init__(
        self,
        elements: list[FakeElement],
        page: AuthJourney,
        index: int | None = None,
        query: tuple[str, str | None] | None = None,
    ) -> None:
        super().__init__(elements, index)
        self._page = page
        self._query = query

    def nth(self, index: int) -> ScriptedLocator:
        return ScriptedLocator(self._elements, self._page, index, self._query)

    async def click(self) -> None:
        await super().click()
        marker = str(self._element.attrs.get("marker"))
        self._page.record(f"click:{marker}")
        self._page.clicked_via[marker] = self._query
        self._page.advance(marker)

    async def fill(self, value: str) -> None:
        await super().fill(value)
        self._page.record(f"fill:{self._element.attrs.get('marker')}")

    async def set_input_files(self, path: str) -> None:
        await super().set_input_files(path)
        self._page.record(f"upload:{self._element.attrs.get('marker')}")
        self._page.uploaded.append(path)
        self._page.key_uploaded()

    async def is_disabled(self) -> bool:
        """A control the site enables a moment after rendering it."""
        remaining = int(self._element.attrs.get("disabled_checks", 0) or 0)
        if remaining:
            self._element.attrs["disabled_checks"] = remaining - 1
            self._page.record(f"disabled:{self._element.attrs.get('marker')}")
            return True
        return await super().is_disabled()

    async def select_option(self, *, label: str) -> list[str]:
        values = await super().select_option(label=label)
        self._page.record(f"select:{self._element.attrs.get('marker')}")
        self._page.selected.append(label)
        return values


class AuthJourney(FakePage):
    """HSC + ID.GOV.UA as a small state machine.

    Screens hold plain elements; queries match them the way Playwright does
    (substring unless ``exact=True``), so a selector that would be ambiguous on
    the real site is ambiguous here too.
    """

    def __init__(
        self,
        *,
        authenticated: bool = False,
        signing_succeeds: bool = True,
        callback_returns: bool = True,
        library_missing: bool = False,
        provider_options: list[str] | None = None,
        provider_tag: str = "select",
        key_loads: bool = True,
        submit_disabled_checks: int = 0,
        submit_never_enables: bool = False,
        rejection_text: str | None = None,
        opens_file_chooser: bool = True,
        chooser_input_id: str = "PKeyFileInput",
        oidc_redirects: int = 0,
        resets_to_key_form: bool = False,
        advances_to_new_screen: bool = False,
        advances_to_user_data: bool = False,
        user_data_callback_returns: bool = True,
        callback_after_processing: bool = False,
        processing_never_ends: bool = False,
        processing_polls: int = 2,
        page_error: str | None = None,
        cabinet_extras: list[FakeElement] | None = None,
    ) -> None:
        super().__init__(matches={})
        self.authenticated = authenticated
        self.signing_succeeds = signing_succeeds
        self.callback_returns = callback_returns
        #: Whether ID.GOV.UA finishes reading the uploaded .dat.
        self.key_loads = key_loads
        #: How many HSC navigations a stale OIDC flow bounces to id.gov.ua.
        self.oidc_redirects = oidc_redirects
        self.rejection_text = rejection_text
        #: Whether the visible control makes the page ask for a file, and which
        #: input it asks for. The screen holds two.
        self.opens_file_chooser = opens_file_chooser
        self.chooser_input_id = chooser_input_id
        #: Whether Playwright is intercepting file choosers right now.
        self.chooser_armed = False
        #: The live symptom: read the key, then put the same form back.
        self.resets_to_key_form = resets_to_key_form
        self.advances_to_new_screen = advances_to_new_screen
        #: The measured behaviour: a good signature lands on «Перевірте дані».
        self.advances_to_user_data = advances_to_user_data
        #: Whether confirming that screen is what finally returns the browser.
        self.user_data_callback_returns = user_data_callback_returns
        self.user_data_confirmed = False
        self.callback_after_processing = callback_after_processing
        self.processing_never_ends = processing_never_ends
        self.processing_polls = processing_polls
        self.page_error = page_error
        #: Polls served since the click, which is what drives the script below.
        self.post_submit_polls = 0
        self.submitted = False
        self.actions: list[str] = []
        #: marker -> the query that produced the locator that was clicked.
        self.clicked_via: dict[str, tuple[str, str | None] | None] = {}
        self.uploaded: list[str] = []
        self.selected: list[str] = []
        self.journeys = 0

        idgov = element("idgov", tag="button", text=".cls-1id.gov.ua", disabled=True)
        method_screen = [
            element("electronic_signature", text="Вхід за допомогою Електронного підпису")
        ]
        if library_missing:
            method_screen.append(element("no_library", text=NO_LIBRARY_TEXT))

        self.screens: dict[str, list[FakeElement]] = {
            "signed_out": [
                element("terms", tag="label", text=TERMS_TEXT),
                idgov,
            ],
            "idgov_method": method_screen,
            "idgov_signature": [element("file_tab", text="Файловий носій")],
            # The real screen carries TWO file inputs, told apart only by their
            # ids, so `input[type="file"]` matches both of them here as well.
            "idgov_file": [
                element(
                    "provider",
                    tag=provider_tag,
                    css="#CAsServersSelect",
                    options=list(PROVIDER_OPTIONS if provider_options is None
                                 else provider_options),
                    selected=DEFAULT_PROVIDER,
                ),
                element("key_file_trigger", tag="span", text=KEY_FILE_TRIGGER_TEXT),
                element(
                    "key_file",
                    tag="input",
                    id="PKeyFileInput",
                    css=["#PKeyFileInput", 'input[type="file"]'],
                ),
                element(
                    "certificates",
                    tag="input",
                    id="ChoosePKCertsInput",
                    css=["#ChoosePKCertsInput", 'input[type="file"]'],
                ),
                element("password", tag="input", label=PASSWORD_LABEL),
                element(
                    "submit",
                    tag="button",
                    text="Продовжити",
                    # The real button is rendered before it is usable.
                    disabled=submit_never_enables,
                    disabled_checks=submit_disabled_checks,
                ),
            ],
            # Somewhere past the key form that this project has no steps for.
            "idgov_signer": [
                element("signer_info", tag="div", text=SIGNER_TEXT),
                element("signer_continue", tag="button", text="Далі"),
            ],
            # «Перевірте дані»: the key was accepted, and ID.GOV.UA wants the
            # data it read out of the certificate confirmed before it hands the
            # browser back. Its «Продовжити» is a *different* button from the
            # key form's, which is the whole reason it needs its own selector,
            # and «Відмовитись» next to it abandons the authentication.
            "idgov_user_data": [
                element("user_data_heading", tag="h2", text=USER_DATA_TEXT),
                element(
                    "user_data_fields",
                    tag="div",
                    text=f"{PERSONAL_NAME} {PERSONAL_TAX_ID} {PERSONAL_ADDRESS}",
                ),
                element(
                    "user_data_accept",
                    tag="button",
                    id="btnAcceptUserDataAgreement",
                    css=USER_DATA_ACCEPT_CSS,
                    text="Продовжити",
                ),
                element(
                    "user_data_reset",
                    tag="button",
                    id="btnResetUserDataAgreement",
                    css=USER_DATA_RESET_CSS,
                    text="Відмовитись",
                ),
            ],
            "cabinet": [
                element("authenticated_marker", tag="a", text=MARKER_TEXT),
                *(cabinet_extras or []),
            ],
        }
        self.screen = "cabinet" if authenticated else "signed_out"
        self.url = CABINET if authenticated else SIGNED_OUT

    # -- state machine ----------------------------------------------------

    def record(self, action: str) -> None:
        self.actions.append(action)

    def on_file_chooser_armed(self) -> None:
        """Playwright's listener going up. Recorded to prove it precedes the click."""
        self.chooser_armed = True
        self.record("arm:file_chooser")

    def _open_file_chooser(self) -> None:
        """What clicking the visible control does.

        With a listener armed, Playwright intercepts and the page's chooser
        event is what fires. Without one the browser opens its own native
        dialog instead — which is exactly why the two mechanisms are mutually
        exclusive, and why the native path must never arm the listener.
        """
        if not self.opens_file_chooser or not self.chooser_armed:
            return
        self.pending_file_chooser = FakeFileChooser(
            self.chooser_input_id, on_set=self._file_chosen
        )

    def native_file_selected(self, path: str) -> None:
        """The OS dialog being answered: the file lands on the input for real."""
        self._file_chosen(path)

    def _file_chosen(self, path: str) -> None:
        """The chooser being answered — the equivalent of a person picking a file."""
        self.record("upload:key_file")
        self.uploaded.append(path)
        self._element("key_file").attrs["files"] = path
        self.key_uploaded()

    def key_uploaded(self) -> None:
        """Reading the .dat is what reveals "Завантажити інший файл".

        Until then the screen looks exactly as it did before the upload, which
        is the state the live site was getting stuck in.
        """
        if not self.key_loads:
            return
        self.screens["idgov_file"].append(
            element("key_loaded", tag="button", text=KEY_LOADED_TEXT)
        )

    def advance(self, marker: str) -> None:
        match marker:
            case "terms":
                # Ticking the box is what enables the hand-over button.
                self._element("idgov").attrs["disabled"] = False
            case "idgov":
                self.journeys += 1
                self.url = IDGOV
                self.screen = "idgov_method"
            case "electronic_signature":
                self.screen = "idgov_signature"
            case "file_tab":
                self.screen = "idgov_file"
            case "key_file_trigger":
                self._open_file_chooser()
            case "user_data_accept":
                self.user_data_confirmed = True
                if not self.user_data_callback_returns:
                    # Confirmed, and then nothing came back.
                    return
                self.authenticated = True
                self.url = SIGNED_OUT
                self.screen = "signed_out"
            case "user_data_reset":  # pragma: no cover - nothing may click it
                raise AssertionError("«Відмовитись» must never be clicked")
            case "submit":
                self.submitted = True
                if not self.callback_returns:
                    # The browser never comes back from ID.GOV.UA. It may or
                    # may not say why.
                    if self.rejection_text is not None:
                        self.screens["idgov_file"].append(
                            element("auth_error", tag="div", text=self.rejection_text)
                        )
                    return
                self.authenticated = self.signing_succeeds
                self.url = SIGNED_OUT
                self.screen = "signed_out"

    @property
    def post_submit_script(self) -> bool:
        """Whether this page has anything scripted to do after the click."""
        return (
            self.resets_to_key_form
            or self.advances_to_new_screen
            or self.advances_to_user_data
            or self.callback_after_processing
            or self.processing_never_ends
        )

    @property
    def body_text(self) -> str:
        """Visible text of the current screen, as innerText would render it.

        Reading it is also what advances the post-submit script: the watcher
        captures the text once per poll, so one read is one observation.
        """
        if self.submitted:
            self._advance_post_submit()
        return " ".join(
            str(e.attrs.get("text", "")) for e in self.screens[self.screen]
        ).strip()

    def _show_overlay(self) -> None:
        """Cover the form with the dimmer — without unmounting anything.

        This is the behaviour that broke the first detector: every control of
        the key form is still present and «Продовжити» still reports itself as
        enabled while ID.GOV.UA is reading the key.
        """
        if any(e.attrs.get("marker") == "processing" for e in self.screens["idgov_file"]):
            return
        self.screens["idgov_file"].extend(
            [
                element("processing", tag="label", text=PROCESSING_TEXT),
                element("processing_wait", tag="label", text=PROCESSING_WAIT_TEXT),
            ]
        )

    def _hide_overlay(self) -> None:
        self.screens["idgov_file"] = [
            e
            for e in self.screens["idgov_file"]
            if not str(e.attrs.get("marker", "")).startswith("processing")
        ]

    def _advance_post_submit(self) -> None:
        """Reproduce the live sequence: the dimmer, then a destination."""
        if not self.post_submit_script:
            return

        self.post_submit_polls += 1
        if self.post_submit_polls == 1:
            # Whatever the site does with the submission, it does it here.
            self.emit(
                "console",
                FakeConsoleMessage(type="error", text="euid: sign failed (0x8009000b)"),
            )
            self.emit("console", FakeConsoleMessage(type="log", text="routine chatter"))
            self.emit(
                "response",
                FakeResponse(
                    f"{IDGOV_AUTHORIZE}&code=SECRET-CODE-VALUE",
                    status=400,
                    method="POST",
                    content_type="application/json; charset=utf-8",
                ),
            )
            self.emit("response", FakeResponse("https://cdn.example.test/x.js", status=200))
            if self.page_error is not None:
                self.emit("pageerror", ValueError(self.page_error))

        if self.processing_never_ends or self.post_submit_polls <= self.processing_polls:
            self._show_overlay()
            return

        self._hide_overlay()
        if self.advances_to_new_screen:
            self.screen = "idgov_signer"
        elif self.advances_to_user_data and not self.user_data_confirmed:
            # It stays there until it is confirmed — one click, one transition.
            self.screen = "idgov_user_data"
        elif self.callback_after_processing:
            self.authenticated = True
            self.url, self.screen = SIGNED_OUT, "signed_out"
        # Otherwise the form is simply back, exactly as it was.

    def _element(self, marker: str) -> FakeElement:
        for elements in self.screens.values():
            for candidate in elements:
                if candidate.attrs.get("marker") == marker:
                    return candidate
        raise AssertionError(f"no element marked {marker!r}")

    async def goto(self, url: str, **_k: Any) -> None:
        self.calls.append(("goto", (url,), {}))
        on_hsc = url.startswith(SIGNED_OUT.rstrip("/"))

        # A half-finished OIDC hand-over sends HSC straight to id.gov.ua.
        if on_hsc and not self.authenticated and self.oidc_redirects > 0:
            self.oidc_redirects -= 1
            self.url, self.screen = IDGOV_AUTHORIZE, "idgov_method"
            return

        # The real site bounces an expired session out of the cabinet.
        if url.startswith(CABINET) and not self.authenticated:
            self.url, self.screen = SIGNED_OUT, "signed_out"
            return

        self.url = url
        if url.startswith(CABINET):
            self.screen = "cabinet"
        elif on_hsc:
            self.screen = "signed_out"

    # -- queries ----------------------------------------------------------

    def _match(
        self,
        attribute: str,
        wanted: str | None,
        exact: bool | None,
        query: tuple[str, str | None] | None = None,
    ) -> ScriptedLocator:
        found = []
        for candidate in self.screens[self.screen]:
            value = candidate.attrs.get(attribute)
            if value is None or wanted is None:
                continue
            # A list means "this element is matched by all of these selectors",
            # which is how one <input> answers both #PKeyFileInput and
            # input[type="file"].
            if isinstance(value, list):
                matched = wanted in value
            else:
                matched = str(value) == wanted if exact else wanted in str(value)
            if matched:
                found.append(candidate)
        return ScriptedLocator(found, self, query=query)

    def get_by_role(self, role: str, **kwargs: Any) -> ScriptedLocator:
        self.calls.append(("get_by_role", (role,), kwargs))
        name = kwargs.get("name")
        return self._match("text", name, kwargs.get("exact"), ("get_by_role", name))

    def get_by_text(self, value: str, **kwargs: Any) -> ScriptedLocator:
        self.calls.append(("get_by_text", (value,), kwargs))
        return self._match("text", value, kwargs.get("exact"), ("get_by_text", value))

    def get_by_label(self, value: str, **kwargs: Any) -> ScriptedLocator:
        self.calls.append(("get_by_label", (value,), kwargs))
        return self._match("label", value, kwargs.get("exact"), ("get_by_label", value))

    def get_by_placeholder(self, value: str, **kwargs: Any) -> ScriptedLocator:
        self.calls.append(("get_by_placeholder", (value,), kwargs))
        return self._match(
            "placeholder", value, kwargs.get("exact"), ("get_by_placeholder", value)
        )

    def locator(self, value: str) -> ScriptedLocator:
        self.calls.append(("locator", (value,), {}))
        return self._match("css", value, True, ("locator", value))


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


def flow_yaml(
    key_provider: str | None = MASTERKEY_PROVIDER,
    authentication_timeout_ms: int = 400,
    file_selection: str = "native",
) -> str:
    """flow.yaml text, with or without the authentication section."""
    section = ""
    if key_provider is not None:
        section = (
            "authentication:\n"
            f"  key_provider: '{key_provider}'\n"
            f"  file_selection: {file_selection}"
        )
    return FLOW.format(
        authentication=section, authentication_timeout_ms=authentication_timeout_ms
    )


def build_config(
    tmp_path: Path,
    *,
    key_path: Path | None = None,
    password: str | None = PASSWORD,
    selectors_yaml: str = SELECTORS,
    key_provider: str | None = MASTERKEY_PROVIDER,
    authentication_timeout_ms: int = 400,
    file_selection: str = "native",
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> AppConfig:
    """Config whose MasterKey settings come from the environment, as in production.

    The КНЕДП is not a secret, so it comes from flow.yaml rather than the
    environment — the same split the real configuration uses.
    """
    if monkeypatch is not None:
        if key_path is None:
            key_path = tmp_path / "masterkey.dat"
            key_path.write_bytes(b"not-a-real-key")
        monkeypatch.setenv("IDGOV_SIGNING_KEY_PATH", str(key_path))
        monkeypatch.setenv("IDGOV_SIGNING_KEY_PASSWORD", password or "")

    return AppConfig(
        secrets=load_secrets(env_file=tmp_path / "absent.env"),
        app=AppSettings(),
        paths=Paths(data_dir=tmp_path),
        selectors=SelectorRegistry.from_dict(yaml.safe_load(selectors_yaml)),
        flow=FlowConfig.from_dict(
            yaml.safe_load(
                flow_yaml(key_provider, authentication_timeout_ms, file_selection)
            )
        ),
        service_centers=[CENTER_3242],
    )


def build_context(
    config: AppConfig,
    page: FakePage,
    tmp_path: Path | None = None,
    *,
    file_selector: Any | None = None,
) -> FlowContext:
    """A context whose OS file dialog is a fake — there is no real one in tests.

    In ``chooser`` mode none is injected, so the Playwright path runs exactly
    as it does in production.
    """
    diagnostics = (
        Diagnostics(tmp_path / "debug", enabled=True) if tmp_path is not None else None
    )
    if file_selector is None and config.flow.authentication.file_selection == "native":
        file_selector = FakeNativeFileSelector(page)
    return FlowContext(
        config=config,
        page=page,
        diagnostics=diagnostics,
        file_selector=file_selector,
    )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The developer's own .env must not leak into these tests."""
    monkeypatch.delenv("IDGOV_SIGNING_KEY_PATH", raising=False)
    monkeypatch.delenv("IDGOV_SIGNING_KEY_PASSWORD", raising=False)


# --------------------------------------------------------------------------- #
# Already authenticated
# --------------------------------------------------------------------------- #


async def test_live_session_performs_no_login_actions(tmp_path):
    """Neither the key file nor any login control may be touched."""
    page = AuthJourney(authenticated=True)
    # No IDGOV_SIGNING_KEY_PATH / IDGOV_SIGNING_KEY_PASSWORD at all: any attempt to authenticate
    # would raise ConfigError, which is exactly what must not happen.
    ctx = build_context(build_config(tmp_path), page)

    await ctx.auth.ensure_authenticated()

    assert page.actions == []
    assert page.uploaded == []
    assert page.journeys == 0
    assert page.url == CABINET


async def test_ensure_authenticated_is_idempotent(tmp_path):
    page = AuthJourney(authenticated=True)
    ctx = build_context(build_config(tmp_path), page)

    await ctx.auth.ensure_authenticated()
    await ctx.auth.ensure_authenticated()

    assert page.actions == []


async def test_is_authenticated_needs_the_url_and_the_marker(tmp_path):
    page = AuthJourney(authenticated=True)
    ctx = build_context(build_config(tmp_path), page)
    assert await ctx.auth.is_authenticated() is True

    # A cabinet URL alone is not enough: drop the marker and it is a "no".
    page.screens["cabinet"] = []
    assert await ctx.auth.is_authenticated() is False


async def test_is_authenticated_returns_false_off_the_cabinet(tmp_path):
    """An absent marker is an answer, never an exception."""
    page = AuthJourney()
    ctx = build_context(build_config(tmp_path), page)

    assert await ctx.auth.is_authenticated() is False
    assert page.actions == []


# --------------------------------------------------------------------------- #
# Expired session
# --------------------------------------------------------------------------- #


async def test_expired_session_is_detected_and_recovered(tmp_path, monkeypatch, caplog):
    page = AuthJourney()
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx = build_context(config, page)

    with caplog.at_level(logging.INFO):
        await ctx.auth.ensure_authenticated()

    assert page.url == CABINET
    assert "Authentication session is not active" in caplog.text
    assert "Starting ID.GOV.UA authentication" in caplog.text
    assert "ID.GOV.UA authentication completed" in caplog.text
    assert "HSC authenticated session established" in caplog.text


async def test_the_full_journey_runs_in_order(tmp_path, monkeypatch):
    page = AuthJourney()
    ctx = build_context(build_config(tmp_path, monkeypatch=monkeypatch), page)

    await ctx.auth.ensure_authenticated()

    assert page.actions == JOURNEY


async def test_the_key_file_from_the_environment_is_uploaded(tmp_path, monkeypatch):
    key = tmp_path / "keys" / "Key-6.dat"
    key.parent.mkdir()
    key.write_bytes(b"not-a-real-key")
    page = AuthJourney()
    ctx = build_context(build_config(tmp_path, key_path=key, monkeypatch=monkeypatch), page)

    await ctx.auth.ensure_authenticated()

    assert page.uploaded == [str(key)]


async def test_the_password_is_filled_from_the_environment(tmp_path, monkeypatch):
    page = AuthJourney()
    ctx = build_context(build_config(tmp_path, monkeypatch=monkeypatch), page)

    await ctx.auth.ensure_authenticated()

    assert page._element("password").attrs["value"] == PASSWORD


async def test_the_password_never_reaches_a_log_or_an_artifact(
    tmp_path, monkeypatch, caplog
):
    page = AuthJourney()
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx = build_context(config, page, tmp_path)

    with caplog.at_level(logging.DEBUG):
        await ctx.auth.ensure_authenticated()

    assert PASSWORD not in caplog.text
    assert "<redacted>" in caplog.text

    written = list((tmp_path / "debug").rglob("*"))
    assert written, "the journey should have produced at least the event journal"
    for path in written:
        if path.is_file():
            assert PASSWORD not in path.read_text(encoding="utf-8", errors="ignore")


async def test_the_terms_box_is_not_toggled_back_off(tmp_path, monkeypatch):
    """Consent already given: clicking again would withdraw it."""
    page = AuthJourney()
    page._element("idgov").attrs["disabled"] = False
    ctx = build_context(build_config(tmp_path, monkeypatch=monkeypatch), page)

    await ctx.auth.ensure_authenticated()

    assert "click:terms" not in page.actions
    assert page.actions[0] == "click:idgov"


# --------------------------------------------------------------------------- #
# Key provider (КНЕДП)
# --------------------------------------------------------------------------- #


async def test_the_configured_provider_is_selected_by_its_visible_label(
    tmp_path, monkeypatch, caplog
):
    page = AuthJourney()
    ctx = build_context(build_config(tmp_path, monkeypatch=monkeypatch), page)

    with caplog.at_level(logging.INFO):
        await ctx.auth.ensure_authenticated()

    assert page.selected == [MASTERKEY_PROVIDER]
    # The page ends up on the requested provider, not on the ДПС default.
    assert page._element("provider").attrs["selected"] == MASTERKEY_PROVIDER
    assert f"Selecting key provider: {MASTERKEY_PROVIDER}" in caplog.text


async def test_the_journey_is_narrated_in_order(tmp_path, monkeypatch, caplog):
    """What `ensure-auth` prints, in the order the screens are actually visited."""
    page = AuthJourney()
    ctx = build_context(build_config(tmp_path, monkeypatch=monkeypatch), page)

    with caplog.at_level(logging.INFO):
        await ctx.auth.ensure_authenticated()

    expected = [
        "Authentication session is not active",
        "Starting ID.GOV.UA authentication",
        "Accepting the service terms",
        "Continuing to ID.GOV.UA",
        "ID.GOV.UA login page reached",
        "Selecting the electronic-signature authentication method",
        "Selecting the file-based key medium",
        f"Selecting key provider: {MASTERKEY_PROVIDER}",
        "Opening ID.GOV.UA key file chooser",
        "Selecting key file (masterkey.dat)",
        "MasterKey file loaded",
        "Filling login.password with <redacted>",
        "Submitting authentication",
        "Clicking login.submit",
        "login.submit click completed",
        "Waiting for the ID.GOV.UA callback",
        "ID.GOV.UA authentication completed",
    ]
    positions = [caplog.text.find(line) for line in expected]
    assert all(position >= 0 for position in positions), [
        line for line, position in zip(expected, positions, strict=True) if position < 0
    ]
    assert positions == sorted(positions)


async def test_the_provider_is_selected_before_the_key_is_uploaded(tmp_path, monkeypatch):
    """The provider decides how the .dat is read, so it cannot come after it."""
    page = AuthJourney()
    ctx = build_context(build_config(tmp_path, monkeypatch=monkeypatch), page)

    await ctx.auth.ensure_authenticated()

    assert page.actions.index("select:provider") < page.actions.index("upload:key_file")
    assert page.actions.index("select:provider") > page.actions.index("click:file_tab")


async def test_the_provider_travels_from_flow_yaml_through_the_auth_manager(
    tmp_path, monkeypatch
):
    """Whatever flow.yaml says is what reaches the dropdown — nothing is hardcoded."""
    other = "КНЕДП Приватбанк"
    page = AuthJourney()
    config = build_config(tmp_path, key_provider=other, monkeypatch=monkeypatch)
    assert config.flow.authentication.key_provider == other

    await build_context(config, page).auth.ensure_authenticated()

    assert page.selected == [other]


async def test_a_missing_provider_config_fails_before_the_journey_starts(
    tmp_path, monkeypatch
):
    page = AuthJourney()
    config = build_config(tmp_path, key_provider=None, monkeypatch=monkeypatch)
    ctx = build_context(config, page)

    with pytest.raises(ConfigError) as exc:
        await ctx.auth.ensure_authenticated()

    message = str(exc.value)
    assert "authentication.key_provider is not set" in message
    # It says where to put it, and it is not asked for in .env.
    assert "config/flow.yaml" in message
    assert page.actions == []
    assert page.uploaded == []
    assert page.selected == []


async def test_a_blank_provider_config_is_rejected(tmp_path, monkeypatch):
    page = AuthJourney()
    ctx = build_context(build_config(tmp_path, key_provider="   ", monkeypatch=monkeypatch),
                        page)

    with pytest.raises(ConfigError, match="authentication.key_provider is not set"):
        await ctx.auth.ensure_authenticated()

    assert page.actions == []


async def test_an_unknown_provider_stops_before_the_upload(tmp_path, monkeypatch):
    """A provider the page does not offer is a configuration error, not a guess."""
    page = AuthJourney()
    config = build_config(
        tmp_path, key_provider="КНЕДП НЕІСНУЄ", monkeypatch=monkeypatch
    )
    ctx = build_context(config, page)

    with pytest.raises(FlowError) as exc:
        await ctx.auth.ensure_authenticated()

    message = str(exc.value)
    assert "КНЕДП НЕІСНУЄ" in message
    # The error names what the dropdown really offers and where to fix it.
    assert MASTERKEY_PROVIDER in message
    assert "authentication.key_provider" in message

    assert page.selected == []
    assert page.uploaded == []
    assert page.actions == ["click:terms", "click:idgov",
                            "click:electronic_signature", "click:file_tab"]


async def test_a_provider_selector_pointing_at_the_wrong_element_is_reported(
    tmp_path, monkeypatch
):
    """It has to be the <select> itself: select_option() cannot use a wrapper."""
    page = AuthJourney(provider_tag="div")
    ctx = build_context(build_config(tmp_path, monkeypatch=monkeypatch), page)

    with pytest.raises(FlowError, match="not a <select>"):
        await ctx.auth.ensure_authenticated()

    assert page.selected == []
    assert page.uploaded == []


async def test_a_live_session_selects_no_provider(tmp_path):
    """Nothing on the login screens is touched when the session already works."""
    page = AuthJourney(authenticated=True)
    ctx = build_context(build_config(tmp_path), page)

    await ctx.auth.ensure_authenticated()

    assert page.selected == []
    assert page._element("provider").attrs["selected"] == DEFAULT_PROVIDER


# --------------------------------------------------------------------------- #
# Upload readiness: the key must be *accepted*, not merely handed over
# --------------------------------------------------------------------------- #


async def test_the_password_is_not_filled_until_the_key_is_loaded(tmp_path, monkeypatch):
    """The bug this exists for: typing into a form bound to no key.

    ID.GOV.UA reads the .dat asynchronously. Until it says so, nothing may be
    typed and nothing may be submitted.
    """
    page = AuthJourney(key_loads=False)
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx = build_context(config, page, tmp_path)

    with pytest.raises(FlowError):
        await ctx.auth.ensure_authenticated()

    assert page.uploaded, "the file was handed to the input"
    assert "fill:password" not in page.actions
    assert "value" not in page._element("password").attrs
    assert "click:submit" not in page.actions
    # It stopped at the upload, with nothing after it.
    assert page.actions[-1] == "upload:key_file"


async def test_a_key_that_is_never_accepted_fails_with_the_screen_saved(
    tmp_path, monkeypatch, caplog
):
    page = AuthJourney(key_loads=False)
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx = build_context(config, page, tmp_path)

    with caplog.at_level(logging.INFO), pytest.raises(FlowError) as exc:
        await ctx.auth.ensure_authenticated()

    message = str(exc.value)
    assert "login.key_loaded never appeared" in message
    assert "the password was NOT typed" in message
    # The artifacts are named in the error, not left to be hunted for.
    dumps = list((tmp_path / "debug").glob("*auth-key-not-loaded-elements.json"))
    shots = list((tmp_path / "debug").glob("*auth-key-not-loaded.png"))
    assert dumps and shots
    assert str(dumps[0]) in message
    assert str(shots[0]) in message
    assert "MasterKey file loaded" not in caplog.text


async def test_the_key_loaded_marker_is_what_releases_the_password(tmp_path, monkeypatch):
    """Ordering, positively: loaded -> password -> submit."""
    page = AuthJourney()
    ctx = build_context(build_config(tmp_path, monkeypatch=monkeypatch), page)

    await ctx.auth.ensure_authenticated()

    assert page.actions.index("upload:key_file") < page.actions.index("fill:password")
    assert page.actions.index("fill:password") < page.actions.index("click:submit")


# --------------------------------------------------------------------------- #
# Submit readiness and click instrumentation
# --------------------------------------------------------------------------- #


async def test_submit_waits_for_the_button_to_become_enabled(tmp_path, monkeypatch):
    """A disabled "Продовжити" is waited out, never force-clicked."""
    page = AuthJourney(submit_disabled_checks=1)
    ctx = build_context(build_config(tmp_path, monkeypatch=monkeypatch), page)

    await ctx.auth.ensure_authenticated()

    assert "disabled:submit" in page.actions
    assert page.actions.index("disabled:submit") < page.actions.index("click:submit")
    assert page.actions.count("click:submit") == 1


async def test_a_submit_that_never_enables_stops_the_journey(tmp_path, monkeypatch):
    page = AuthJourney(submit_never_enables=True)
    ctx = build_context(build_config(tmp_path, monkeypatch=monkeypatch), page)

    with pytest.raises(FlowError, match="login.submit is still disabled"):
        await ctx.auth.ensure_authenticated()

    assert "click:submit" not in page.actions


async def test_the_click_is_reported_as_completed_before_the_callback_wait(
    tmp_path, monkeypatch, caplog
):
    """The click returning is evidence about the click, and it is logged as such."""
    page = AuthJourney()
    ctx = build_context(build_config(tmp_path, monkeypatch=monkeypatch), page)

    with caplog.at_level(logging.INFO):
        await ctx.auth.ensure_authenticated()

    completed = caplog.text.find("login.submit click completed")
    waiting = caplog.text.find("Waiting for the ID.GOV.UA callback")
    assert completed >= 0 and waiting > completed
    # Before/after state is recorded, so a click that changed nothing is visible.
    assert "url: " in caplog.text
    assert PASSWORD not in caplog.text


# --------------------------------------------------------------------------- #
# What happens after submit
# --------------------------------------------------------------------------- #


async def test_the_callback_returns_the_browser_to_hsc(tmp_path, monkeypatch, caplog):
    page = AuthJourney()
    ctx = build_context(build_config(tmp_path, monkeypatch=monkeypatch), page)

    with caplog.at_level(logging.INFO):
        await ctx.auth.ensure_authenticated()

    assert page.url == CABINET
    assert "ID.GOV.UA authentication completed" in caplog.text


def auth_artifacts(tmp_path: Path) -> dict[str, Path]:
    """The post-submit artifact set, keyed by kind."""
    found = {}
    for path in sorted((tmp_path / "debug" / "auth").glob("post-submit-*")):
        kind = path.stem.split("-")[-1] if path.suffix == ".json" else "screenshot"
        found[kind] = path
    return found


async def test_a_callback_timeout_saves_the_screen_and_names_the_files(
    tmp_path, monkeypatch
):
    """The old failure gave only a URL after 120s. Now it hands over evidence."""
    page = AuthJourney(callback_returns=False)
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx = build_context(config, page, tmp_path)

    with pytest.raises(FlowError) as exc:
        await ctx.auth.ensure_authenticated()

    message = str(exc.value)
    assert "authentication callback" in message
    assert page.url in message

    artifacts = auth_artifacts(tmp_path)
    assert set(artifacts) == {"screenshot", "elements", "text", "console", "network"}
    for path in artifacts.values():
        assert str(path) in message, f"{path.name} must be named in the error"

    payload = json.loads(artifacts["elements"].read_text(encoding="utf-8"))
    assert payload["url"] == IDGOV
    assert payload["outcome"] == "callback-timeout"
    assert PASSWORD not in artifacts["elements"].read_text(encoding="utf-8")


async def test_a_rejection_fails_immediately_instead_of_waiting_out_the_callback(
    tmp_path, monkeypatch
):
    """Outcome B. Inert until login.auth_error is filled in from a real capture."""
    selectors = SELECTORS.replace(
        '  auth_error:\n    strategy: text\n    value: "TODO"',
        f'  auth_error:\n    strategy: text\n    value: "{REJECTED_TEXT}"',
    )
    page = AuthJourney(callback_returns=False, rejection_text=REJECTED_TEXT)
    config = build_config(tmp_path, selectors_yaml=selectors, monkeypatch=monkeypatch)
    ctx = build_context(config, page, tmp_path)

    with pytest.raises(AuthenticationFailed) as exc:
        await ctx.auth.ensure_authenticated()

    message = str(exc.value)
    assert REJECTED_TEXT in message
    assert "refusal, not a slow response" in message

    artifacts = auth_artifacts(tmp_path)
    assert json.loads(artifacts["elements"].read_text(encoding="utf-8"))[
        "outcome"
    ] == "rejected"
    # One attempt, and it did not sit through the callback timeout first.
    assert page.journeys == 1


async def test_an_unconfigured_error_selector_leaves_the_timeout_path_intact(
    tmp_path, monkeypatch
):
    """login.auth_error is TODO on purpose — it must not break the timeout path."""
    page = AuthJourney(callback_returns=False)
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx = build_context(config, page, tmp_path)

    with pytest.raises(FlowError, match="authentication callback"):
        await ctx.auth.ensure_authenticated()

    assert auth_artifacts(tmp_path)["text"].exists()


# --------------------------------------------------------------------------- #
# The provider A/B diagnostic (ensure-auth-debug-provider)
# --------------------------------------------------------------------------- #


class Operator:
    """Stands in for the person at the terminal. Records what they were asked."""

    def __init__(self, *, selects: str | None = MASTERKEY_PROVIDER) -> None:
        self.prompts: list[str] = []
        self.actions_when_asked: list[str] = []
        self._selects = selects
        self.page: AuthJourney | None = None

    async def __call__(self, message: str) -> str:
        self.prompts.append(message)
        assert self.page is not None
        # Whatever the automation had done by this point is frozen here.
        self.actions_when_asked = list(self.page.actions)
        if self._selects is not None:
            # A human working the visible dropdown; no select_option() involved.
            self.page._element("provider").attrs["selected"] = self._selects
        return ""


def operator_for(page: AuthJourney, **kwargs: Any) -> Operator:
    operator = Operator(**kwargs)
    operator.page = page
    return operator


async def test_debug_provider_pauses_before_the_provider_is_selected(
    tmp_path, monkeypatch
):
    page = AuthJourney()
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx = build_context(config, page)
    operator = operator_for(page)

    code = await run_ensure_auth_debug_provider(config, ctx, prompt=operator)

    assert code == 0
    assert operator.prompts, "the operator was never asked"
    # It stopped exactly after the file-key medium, before touching the dropdown.
    assert operator.actions_when_asked == [
        "click:terms",
        "click:idgov",
        "click:electronic_signature",
        "click:file_tab",
    ]


async def test_debug_provider_never_calls_select_option(tmp_path, monkeypatch):
    """The whole experiment is that select_key_provider() is not used."""
    page = AuthJourney()
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx = build_context(config, page)

    def fail(*_a: Any, **_k: Any) -> None:
        raise AssertionError("select_key_provider() must not run in debug mode")

    monkeypatch.setattr(LoginPage, "select_key_provider", fail)

    code = await run_ensure_auth_debug_provider(config, ctx, prompt=operator_for(page))

    assert code == 0
    assert "select:provider" not in page.actions
    assert page.selected == [], "no select_option() call was made"


async def test_debug_provider_verifies_the_native_select_afterwards(
    tmp_path, monkeypatch, caplog
):
    page = AuthJourney()
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx = build_context(config, page)

    with caplog.at_level(logging.INFO):
        code = await run_ensure_auth_debug_provider(
            config, ctx, prompt=operator_for(page)
        )

    assert code == 0
    assert page._element("provider").attrs["selected"] == MASTERKEY_PROVIDER
    assert f"Key provider confirmed from the page: {MASTERKEY_PROVIDER}" in caplog.text
    assert MASTERKEY_PROVIDER in caplog.text


async def test_debug_provider_stops_when_the_manual_selection_did_not_take(
    tmp_path, monkeypatch, capsys
):
    """ENTER pressed without choosing anything must not run the key anyway."""
    page = AuthJourney()
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx = build_context(config, page)

    code = await run_ensure_auth_debug_provider(
        config, ctx, prompt=operator_for(page, selects=None)
    )

    assert code == 1
    assert "FlowError" in capsys.readouterr().err
    # It never went on to use the key.
    assert page.uploaded == []
    assert "click:submit" not in page.actions


async def test_debug_provider_continues_the_rest_of_the_journey_automatically(
    tmp_path, monkeypatch
):
    page = AuthJourney()
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx = build_context(config, page)

    code = await run_ensure_auth_debug_provider(config, ctx, prompt=operator_for(page))

    assert code == 0
    # Everything except the provider step ran exactly as in production.
    assert page.actions == [action for action in JOURNEY if action != "select:provider"]
    assert page.uploaded, "the key was still uploaded automatically"
    assert page._element("password").attrs["value"] == PASSWORD
    assert page.url == CABINET


async def test_debug_provider_keeps_the_password_out_of_the_terminal(
    tmp_path, monkeypatch, caplog, capsys
):
    page = AuthJourney()
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx = build_context(config, page, tmp_path)

    with caplog.at_level(logging.DEBUG):
        await run_ensure_auth_debug_provider(config, ctx, prompt=operator_for(page))

    captured = capsys.readouterr()
    assert PASSWORD not in caplog.text
    assert PASSWORD not in captured.out + captured.err
    assert "<redacted>" in caplog.text


async def test_normal_ensure_auth_still_selects_the_provider_itself(
    tmp_path, monkeypatch
):
    """The production path must be untouched by any of the above."""
    page = AuthJourney()
    ctx = build_context(build_config(tmp_path, monkeypatch=monkeypatch), page)

    await ctx.auth.ensure_authenticated()

    assert page.actions == JOURNEY
    assert page.selected == [MASTERKEY_PROVIDER]


# --------------------------------------------------------------------------- #
# The password A/B diagnostic (ensure-auth-debug-password)
# --------------------------------------------------------------------------- #

#: What the operator types in the browser. Never IDGOV_SIGNING_KEY_PASSWORD, so a test can
#: tell the two apart, and it must be as absent from the output as PASSWORD is.
TYPED_PASSWORD = "typed-by-hand-9f2a"


class Typist:
    """Stands in for the person typing into the browser's password field."""

    def __init__(self, *, types: str | None = TYPED_PASSWORD) -> None:
        self.prompts: list[str] = []
        self.actions_when_asked: list[str] = []
        self._types = types
        self.page: AuthJourney | None = None

    async def __call__(self, message: str) -> str:
        self.prompts.append(message)
        assert self.page is not None
        self.actions_when_asked = list(self.page.actions)
        if self._types is not None:
            # Straight into the element, the way a keyboard would: no fill().
            self.page._element("password").attrs["value"] = self._types
        return ""


def typist_for(page: AuthJourney, **kwargs: Any) -> Typist:
    typist = Typist(**kwargs)
    typist.page = page
    return typist


def forbid_secret_fill(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any secret fill() an immediate, obvious failure."""
    original = BasePage.fill

    async def guarded(self: BasePage, *args: Any, **kwargs: Any) -> None:
        if kwargs.get("secret"):
            raise AssertionError("BasePage.fill(secret=True) must not run in this mode")
        await original(self, *args, **kwargs)

    monkeypatch.setattr(BasePage, "fill", guarded)


async def test_debug_password_pauses_after_the_key_is_loaded(tmp_path, monkeypatch):
    page = AuthJourney()
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx = build_context(config, page)
    typist = typist_for(page)

    code = await run_ensure_auth_debug_password(config, ctx, prompt=typist)

    assert code == 0
    assert typist.prompts
    # The key is in and accepted; nothing has been typed or submitted yet.
    assert typist.actions_when_asked == [
        "click:terms",
        "click:idgov",
        "click:electronic_signature",
        "click:file_tab",
        "select:provider",
        "click:key_file_trigger",
        "native:select",
        "upload:key_file",
    ]


async def test_debug_password_never_fills_the_configured_password(
    tmp_path, monkeypatch
):
    page = AuthJourney()
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx = build_context(config, page)
    forbid_secret_fill(monkeypatch)

    code = await run_ensure_auth_debug_password(config, ctx, prompt=typist_for(page))

    assert code == 0
    assert "fill:password" not in page.actions
    assert "fills" not in page._element("password").attrs
    # The field holds what the operator typed, not IDGOV_SIGNING_KEY_PASSWORD.
    assert page._element("password").attrs["value"] == TYPED_PASSWORD


async def test_debug_password_only_asks_whether_the_field_is_empty(
    tmp_path, monkeypatch
):
    """The value must never cross into this process — not even to be measured."""
    page = AuthJourney()
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx = build_context(config, page)

    await run_ensure_auth_debug_password(config, ctx, prompt=typist_for(page))

    scripts = page._element("password").attrs["evaluated"]
    assert scripts, "the field was never inspected at all"
    for script in scripts:
        assert ".length > 0" in script, script

    # What such a script returns is a boolean, and nothing else — running it
    # again on the field that still holds the typed value proves it.
    page.screen = "idgov_file"
    answer = await page.get_by_label(PASSWORD_LABEL).nth(0).evaluate(scripts[0])
    assert answer is True
    assert TYPED_PASSWORD not in str(answer)


async def test_debug_password_submits_only_after_the_operator_confirms(
    tmp_path, monkeypatch
):
    page = AuthJourney()
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx = build_context(config, page)
    typist = typist_for(page)

    code = await run_ensure_auth_debug_password(config, ctx, prompt=typist)

    assert code == 0
    assert "click:submit" not in typist.actions_when_asked
    assert page.actions[-1] == "click:submit"
    assert page.url == CABINET


async def test_debug_password_stops_when_nothing_was_typed(
    tmp_path, monkeypatch, capsys
):
    page = AuthJourney()
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx = build_context(config, page)

    code = await run_ensure_auth_debug_password(
        config, ctx, prompt=typist_for(page, types=None)
    )

    assert code == 1
    assert "login.password is still empty" in capsys.readouterr().err
    assert "click:submit" not in page.actions


async def test_debug_password_runs_the_normal_processing_observer(
    tmp_path, monkeypatch, caplog
):
    """Everything after the password is production code, diagnostics included."""
    page = AuthJourney(callback_returns=False, resets_to_key_form=True)
    config = build_config(
        tmp_path, authentication_timeout_ms=LONG_CALLBACK_MS, monkeypatch=monkeypatch
    )
    ctx = build_context(config, page, tmp_path)

    with caplog.at_level(logging.INFO):
        code = await run_ensure_auth_debug_password(
            config, ctx, prompt=typist_for(page)
        )

    assert code == 1
    assert "ID.GOV.UA is reading the private key" in caplog.text
    assert "ID.GOV.UA key processing completed" in caplog.text

    artifacts = auth_artifacts(tmp_path)
    assert set(artifacts) == {"screenshot", "elements", "text", "console", "network"}
    assert json.loads(artifacts["elements"].read_text(encoding="utf-8"))[
        "outcome"
    ] == "form-reset"


async def test_debug_password_keeps_both_passwords_out_of_everything(
    tmp_path, monkeypatch, caplog, capsys
):
    """Neither the configured secret nor the typed one may be recorded."""
    page = AuthJourney(callback_returns=False, resets_to_key_form=True)
    config = build_config(
        tmp_path, authentication_timeout_ms=LONG_CALLBACK_MS, monkeypatch=monkeypatch
    )
    ctx = build_context(config, page, tmp_path)

    with caplog.at_level(logging.DEBUG):
        await run_ensure_auth_debug_password(config, ctx, prompt=typist_for(page))

    captured = capsys.readouterr()
    for secret in (PASSWORD, TYPED_PASSWORD):
        assert secret not in caplog.text
        assert secret not in captured.out + captured.err
        for path in (tmp_path / "debug").rglob("*"):
            if path.is_file():
                content = path.read_text(encoding="utf-8", errors="ignore")
                assert secret not in content, f"{secret!r} leaked into {path}"


async def test_normal_ensure_auth_still_fills_the_password_itself(
    tmp_path, monkeypatch
):
    """The production path must be untouched by the password experiment."""
    page = AuthJourney()
    ctx = build_context(build_config(tmp_path, monkeypatch=monkeypatch), page)

    await ctx.auth.ensure_authenticated()

    assert page.actions == JOURNEY
    assert "fill:password" in page.actions
    assert page._element("password").attrs["value"] == PASSWORD


# --------------------------------------------------------------------------- #
# Native file selection alone (ensure-auth-debug-native-file-only)
# --------------------------------------------------------------------------- #
#
# The site now shows the key as loaded and only then resets, so the remaining
# question is whether the native file selection or the password/submit that
# follows is responsible. This command does the first without the second.


class Waiter:
    """Stands in for the operator, who keeps the browser open meanwhile."""

    def __init__(self, page: AuthJourney) -> None:
        self.page = page
        self.prompts: list[str] = []
        self.actions_when_asked: list[str] = []

    async def __call__(self, message: str) -> str:
        self.prompts.append(message)
        self.actions_when_asked = list(self.page.actions)
        return ""


async def test_the_native_file_only_run_stops_at_the_loaded_key(
    tmp_path, monkeypatch, capsys
):
    page = AuthJourney()
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    selector = FakeNativeFileSelector(page)
    ctx = build_context(config, page, tmp_path, file_selector=selector)
    waiter = Waiter(page)

    code = await run_ensure_auth_debug_native_file_only(config, ctx, prompt=waiter)

    assert code == 0
    # The production journey, up to and including the key being loaded.
    assert page.actions == [
        "click:terms",
        "click:idgov",
        "click:electronic_signature",
        "click:file_tab",
        "select:provider",
        "click:key_file_trigger",
        "native:select",
        "upload:key_file",
    ]
    assert selector.selected, "the production native selector did the selecting"

    out = capsys.readouterr().out
    assert "Native key selection completed." in out
    assert "Key loaded in ID.GOV.UA: masterkey.dat" in out
    assert 'click "Продовжити"' in out


async def test_the_native_file_only_run_never_touches_the_password(
    tmp_path, monkeypatch
):
    """Reading IDGOV_SIGNING_KEY_PASSWORD at all would defeat the experiment."""
    page = AuthJourney()
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx = build_context(config, page, tmp_path, file_selector=FakeNativeFileSelector(page))

    def forbidden(_self: Any) -> str:
        raise AssertionError("the password must not be read by this command")

    monkeypatch.setattr(SecretSettings, "require_key_password", forbidden)
    forbid_secret_fill(monkeypatch)

    code = await run_ensure_auth_debug_native_file_only(config, ctx, prompt=Waiter(page))

    assert code == 0
    assert "fill:password" not in page.actions
    assert "value" not in page._element("password").attrs
    assert "click:submit" not in page.actions


async def test_the_native_file_only_run_requires_the_key_loaded_marker(
    tmp_path, monkeypatch, capsys
):
    page = AuthJourney(key_loads=False)
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx = build_context(config, page, tmp_path, file_selector=FakeNativeFileSelector(page))
    waiter = Waiter(page)

    code = await run_ensure_auth_debug_native_file_only(config, ctx, prompt=waiter)

    assert code == 1
    assert "login.key_loaded never appeared" in capsys.readouterr().err
    assert waiter.prompts == [], "it did not hand over a browser it could not set up"


async def test_the_native_file_only_run_holds_the_browser_open(tmp_path, monkeypatch):
    """Returning would close it, so it waits for the operator instead."""
    page = AuthJourney()
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx = build_context(config, page, tmp_path, file_selector=FakeNativeFileSelector(page))
    waiter = Waiter(page)

    await run_ensure_auth_debug_native_file_only(config, ctx, prompt=waiter)

    assert waiter.prompts, "the operator was asked to finish first"
    assert "ENTER" in waiter.prompts[0]
    # Nothing happened after the key loaded and before the hand-over.
    assert waiter.actions_when_asked[-1] == "upload:key_file"


async def test_the_native_file_only_run_logs_only_the_basename(
    tmp_path, monkeypatch, caplog, capsys
):
    key = tmp_path / "keys" / "Key-6.dat"
    key.parent.mkdir()
    key.write_bytes(b"not-a-real-key")
    page = AuthJourney()
    config = build_config(tmp_path, key_path=key, monkeypatch=monkeypatch)
    ctx = build_context(config, page, tmp_path, file_selector=FakeNativeFileSelector(page))

    with caplog.at_level(logging.DEBUG):
        await run_ensure_auth_debug_native_file_only(config, ctx, prompt=Waiter(page))

    written = caplog.text + capsys.readouterr().out
    assert "Key-6.dat" in written
    assert str(key.parent) not in written
    assert PASSWORD not in written
    assert "not-a-real-key" not in written


async def test_the_native_file_only_run_stops_when_already_authenticated(
    tmp_path, monkeypatch, capsys
):
    page = AuthJourney(authenticated=True)
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx = build_context(config, page, tmp_path, file_selector=FakeNativeFileSelector(page))

    code = await run_ensure_auth_debug_native_file_only(config, ctx, prompt=Waiter(page))

    assert code == 1
    assert "already authenticated" in capsys.readouterr().err
    assert page.actions == []


async def test_normal_ensure_auth_still_completes_the_whole_journey(
    tmp_path, monkeypatch
):
    """The diagnostic must not have changed the production path."""
    page = AuthJourney()
    ctx = build_context(build_config(tmp_path, monkeypatch=monkeypatch), page)

    await ctx.auth.ensure_authenticated()

    assert page.actions == JOURNEY
    assert "fill:password" in page.actions
    assert "click:submit" in page.actions


# --------------------------------------------------------------------------- #
# The accessibility diagnostic (ensure-auth-debug-native-ax)
# --------------------------------------------------------------------------- #


class FakeInspector:
    """A macOS whose accessibility tree is whatever the test says it is.

    Records every action so a test can prove what the diagnostic did *not* do:
    no file selection, no typing, no Return.
    """

    def __init__(
        self, page: AuthJourney, elements: list[dict[str, Any]] | None = None
    ) -> None:
        self.page = page
        self.actions: list[str] = []
        self.elements = elements if elements is not None else [
            {"depth": 0, "role": "AXWindow", "subrole": "", "title": "Open",
             "description": "", "value": "", "enabled": "true", "focused": "false",
             "parent_role": "AXApplication"},
            {"depth": 1, "role": "AXSheet", "subrole": "", "title": "",
             "description": "", "value": "", "enabled": "true", "focused": "false",
             "parent_role": "AXWindow"},
            {"depth": 2, "role": "AXStaticText", "subrole": "",
             "title": "Перейти до:", "description": "", "value": "/",
             "enabled": "true", "focused": "false", "parent_role": "AXSheet"},
        ]

    async def select_file(self, path: Path) -> None:  # pragma: no cover - guard
        raise AssertionError("the diagnostic must never select a file")

    async def wait_for_dialog(self) -> None:
        self.actions.append("wait_for_dialog")

    async def send_goto_shortcut(self) -> None:
        self.actions.append("goto")

    async def describe_hierarchy(
        self, *, max_depth: int = 12, max_elements: int = 400
    ) -> list[dict[str, Any]]:
        self.actions.append(f"describe:{max_depth}:{max_elements}")
        return list(self.elements)


async def test_the_ax_diagnostic_opens_the_dialog_and_dumps_the_tree(
    tmp_path, monkeypatch, capsys
):
    page = AuthJourney()
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    inspector = FakeInspector(page)
    ctx = build_context(config, page, tmp_path, file_selector=inspector)

    code = await run_ensure_auth_debug_native_ax(config, ctx)

    assert code == 0
    assert inspector.actions[:2] == ["wait_for_dialog", "goto"]
    assert inspector.actions[2].startswith("describe:")

    out = capsys.readouterr().out
    assert "windows: 1" in out
    assert "elements inspected: 3" in out
    assert "AXSheet: 1" in out


async def test_the_ax_diagnostic_selects_no_file_and_presses_nothing(
    tmp_path, monkeypatch
):
    """It stops with the dialog open: that is the state being inspected."""
    page = AuthJourney()
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    inspector = FakeInspector(page)
    ctx = build_context(config, page, tmp_path, file_selector=inspector)

    await run_ensure_auth_debug_native_ax(config, ctx)

    # FakeInspector.select_file would have raised; nothing typed, no Return.
    assert page.uploaded == []
    assert "upload:key_file" not in page.actions
    assert "fill:password" not in page.actions
    assert "click:submit" not in page.actions
    assert "return" not in inspector.actions


async def test_the_ax_diagnostic_runs_the_journey_up_to_the_trigger(
    tmp_path, monkeypatch
):
    page = AuthJourney()
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx = build_context(config, page, tmp_path, file_selector=FakeInspector(page))

    await run_ensure_auth_debug_native_ax(config, ctx)

    assert page.actions == [
        "click:terms",
        "click:idgov",
        "click:electronic_signature",
        "click:file_tab",
        "select:provider",
        "click:key_file_trigger",
    ]


async def test_the_ax_diagnostic_writes_a_sanitized_artifact(tmp_path, monkeypatch):
    page = AuthJourney()
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx = build_context(config, page, tmp_path, file_selector=FakeInspector(page))

    await run_ensure_auth_debug_native_ax(config, ctx)

    written = list((tmp_path / "debug").glob("native-ax-*.json"))
    assert len(written) == 1
    payload = json.loads(written[0].read_text(encoding="utf-8"))

    assert payload["summary"]["windows"] == 1
    assert payload["summary"]["roles"]["AXSheet"] == 1
    assert payload["elements"][2]["title"] == "Перейти до:"
    # Harmless UI metadata is kept — that is the point of the dump.
    assert payload["elements"][2]["value"] == "/"


async def test_no_secret_can_reach_the_ax_artifact(tmp_path, monkeypatch):
    """Even a value that somehow contained the password is redacted on the way out."""
    from hsc_queue_monitor.logging_config import setup_logging

    setup_logging(secrets=(PASSWORD,))
    page = AuthJourney()
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    inspector = FakeInspector(page)
    ctx = build_context(config, page, tmp_path, file_selector=inspector)

    await run_ensure_auth_debug_native_ax(config, ctx)

    for path in (tmp_path / "debug").rglob("*"):
        if path.is_file():
            assert PASSWORD not in path.read_text(encoding="utf-8", errors="ignore")


async def test_the_ax_diagnostic_needs_a_native_selector(tmp_path, monkeypatch, capsys):
    """In chooser mode there is no OS dialog to inspect."""
    page = AuthJourney()
    config = chooser_config(tmp_path, monkeypatch=monkeypatch)
    ctx = build_context(config, page, tmp_path)

    code = await run_ensure_auth_debug_native_ax(config, ctx)

    assert code == 2
    assert "needs the native macOS file selector" in capsys.readouterr().err
    assert page.actions == []


async def test_the_ax_diagnostic_stops_when_already_authenticated(
    tmp_path, monkeypatch, capsys
):
    """The key-file screen only exists while signed out."""
    page = AuthJourney(authenticated=True)
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx = build_context(config, page, tmp_path, file_selector=FakeInspector(page))

    code = await run_ensure_auth_debug_native_ax(config, ctx)

    assert code == 1
    assert "already authenticated" in capsys.readouterr().err
    assert page.actions == []


# --------------------------------------------------------------------------- #
# The live symptom: processed, then back to the same key form
# --------------------------------------------------------------------------- #

#: Long enough that waiting it out would be obvious in the test's runtime.
LONG_CALLBACK_MS = 30_000


def reset_journey(**kwargs: Any) -> AuthJourney:
    """The observed behaviour: click, spinner, then the key form returns."""
    return AuthJourney(callback_returns=False, resets_to_key_form=True, **kwargs)


async def test_the_form_under_the_processing_overlay_is_not_a_reset(
    tmp_path, monkeypatch, caplog
):
    """The bug this replaces: classifying work-in-progress as a rejection.

    While «Зчитування особистого ключа» is up, the whole key form is still
    mounted underneath it and «Продовжити» still reports itself enabled. None
    of that is an outcome, and none of it may be treated as one.
    """
    page = AuthJourney(callback_returns=False, processing_never_ends=True)
    config = build_config(
        tmp_path, authentication_timeout_ms=1_500, monkeypatch=monkeypatch
    )
    ctx = build_context(config, page, tmp_path)

    with caplog.at_level(logging.INFO), pytest.raises(
        AuthenticationProcessingTimeout
    ) as exc:
        await ctx.auth.ensure_authenticated()

    # It timed out waiting, and never once called this a form reset.
    assert "still reading the private key" in str(exc.value)
    assert "returned to the file-key form" not in str(exc.value)
    assert "Key form returned after processing" not in caplog.text
    assert "ID.GOV.UA key processing completed" not in caplog.text

    # The form really was present and clickable the whole time.
    assert page._element("submit").attrs.get("disabled") is False
    assert any(e.attrs.get("marker") == "key_loaded" for e in page.screens["idgov_file"])
    assert json.loads(
        auth_artifacts(tmp_path)["elements"].read_text(encoding="utf-8")
    )["outcome"] == "processing-timeout"


async def test_a_long_processing_state_is_waited_out(tmp_path, monkeypatch):
    """Many polls of "busy" must not accumulate into a verdict."""
    page = AuthJourney(
        callback_returns=False, resets_to_key_form=True, processing_polls=8
    )
    config = build_config(
        tmp_path, authentication_timeout_ms=LONG_CALLBACK_MS, monkeypatch=monkeypatch
    )
    ctx = build_context(config, page, tmp_path)

    with pytest.raises(AuthenticationFailed) as exc:
        await ctx.auth.ensure_authenticated()

    # It waited through every processing poll before judging anything.
    assert page.post_submit_polls > 8
    assert "returned to the file-key form" in str(exc.value)


async def test_processing_then_callback_is_a_success(tmp_path, monkeypatch, caplog):
    page = AuthJourney(callback_returns=False, callback_after_processing=True)
    config = build_config(
        tmp_path, authentication_timeout_ms=LONG_CALLBACK_MS, monkeypatch=monkeypatch
    )
    ctx = build_context(config, page, tmp_path)

    with caplog.at_level(logging.INFO):
        await ctx.auth.ensure_authenticated()

    assert "ID.GOV.UA is reading the private key" in caplog.text
    assert "ID.GOV.UA key processing completed" in caplog.text
    assert "Authentication callback received" in caplog.text
    assert page.url == CABINET
    # Success writes no failure artifacts.
    assert not (tmp_path / "debug" / "auth").exists()


async def test_processing_then_a_new_screen_is_intermediate(tmp_path, monkeypatch, caplog):
    page = AuthJourney(callback_returns=False, advances_to_new_screen=True)
    config = build_config(
        tmp_path, authentication_timeout_ms=LONG_CALLBACK_MS, monkeypatch=monkeypatch
    )
    ctx = build_context(config, page, tmp_path)

    with caplog.at_level(logging.INFO), pytest.raises(AuthIntermediateScreenReached):
        await ctx.auth.ensure_authenticated()

    assert "ID.GOV.UA key processing completed" in caplog.text
    assert "advanced to an intermediate authentication screen" in caplog.text


async def test_the_reset_verdict_requires_the_overlay_to_have_been_seen(
    tmp_path, monkeypatch
):
    """A submit that never went busy is not a rejection either.

    Without an observed processing state there is no evidence the site did
    anything, so the honest outcome is the timeout — with the screen saved —
    rather than a verdict inferred from a re-render.
    """
    page = AuthJourney(callback_returns=False, rejection_text="Щось сталося")
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx = build_context(config, page, tmp_path)

    with pytest.raises(FlowError, match="authentication callback"):
        await ctx.auth.ensure_authenticated()

    assert json.loads(
        auth_artifacts(tmp_path)["elements"].read_text(encoding="utf-8")
    )["outcome"] == "callback-timeout"


async def test_the_password_stays_redacted_through_a_processing_timeout(
    tmp_path, monkeypatch, caplog
):
    page = AuthJourney(callback_returns=False, processing_never_ends=True)
    config = build_config(
        tmp_path, authentication_timeout_ms=1_500, monkeypatch=monkeypatch
    )
    ctx = build_context(config, page, tmp_path)

    with caplog.at_level(logging.DEBUG), pytest.raises(AuthenticationProcessingTimeout):
        await ctx.auth.ensure_authenticated()

    assert PASSWORD not in caplog.text
    assert "<redacted>" in caplog.text
    for path in (tmp_path / "debug").rglob("*"):
        if path.is_file():
            assert PASSWORD not in path.read_text(encoding="utf-8", errors="ignore")


async def test_a_reset_to_the_key_form_fails_immediately(tmp_path, monkeypatch):
    page = reset_journey()
    config = build_config(
        tmp_path, authentication_timeout_ms=LONG_CALLBACK_MS, monkeypatch=monkeypatch
    )
    ctx = build_context(config, page, tmp_path)

    with pytest.raises(AuthenticationFailed) as exc:
        await ctx.auth.ensure_authenticated()

    message = str(exc.value)
    assert "returned to the file-key form" in message
    assert "instead of proceeding to signer information" in message
    # No cause is asserted — that is the point of this whole change.
    for guess in ("wrong password", "invalid key", "expired", "wrong provider"):
        assert guess not in message.lower()


async def test_a_reset_does_not_wait_out_the_callback_timeout(tmp_path, monkeypatch):
    """The 120s wait produced no information; this must not reintroduce it."""
    page = reset_journey()
    config = build_config(
        tmp_path, authentication_timeout_ms=LONG_CALLBACK_MS, monkeypatch=monkeypatch
    )
    ctx = build_context(config, page, tmp_path)

    started = time.monotonic()
    with pytest.raises(AuthenticationFailed):
        await ctx.auth.ensure_authenticated()
    elapsed = time.monotonic() - started

    assert elapsed < LONG_CALLBACK_MS / 1000 / 4, f"took {elapsed:.1f}s"
    assert page.journeys == 1


async def test_a_reset_saves_the_full_evidence_set(tmp_path, monkeypatch):
    page = reset_journey()
    config = build_config(
        tmp_path, authentication_timeout_ms=LONG_CALLBACK_MS, monkeypatch=monkeypatch
    )
    ctx = build_context(config, page, tmp_path)

    with pytest.raises(AuthenticationFailed) as exc:
        await ctx.auth.ensure_authenticated()

    artifacts = auth_artifacts(tmp_path)
    assert set(artifacts) == {"screenshot", "elements", "text", "console", "network"}
    for path in artifacts.values():
        assert str(path) in str(exc.value)
    assert json.loads(artifacts["elements"].read_text(encoding="utf-8"))[
        "outcome"
    ] == "form-reset"


async def test_the_transient_processing_text_is_captured(tmp_path, monkeypatch):
    """The point of the text observer: catch what is only on screen briefly."""
    page = reset_journey()
    config = build_config(
        tmp_path, authentication_timeout_ms=LONG_CALLBACK_MS, monkeypatch=monkeypatch
    )
    ctx = build_context(config, page, tmp_path)

    with pytest.raises(AuthenticationFailed):
        await ctx.auth.ensure_authenticated()

    payload = json.loads(auth_artifacts(tmp_path)["text"].read_text(encoding="utf-8"))
    states = [state["text"] for state in payload["states"]]

    # The form before the click, the processing state, and the form again.
    assert any(KEY_LOADED_TEXT in state for state in states)
    assert any(PROCESSING_TEXT in state for state in states), states
    assert states[-1] != states[-2], "distinct states only"
    assert len(states) <= 12, "the buffer is bounded"
    assert {state["phase"] for state in payload["states"]} <= {
        "idgov", "provider", "upload", "submit", "processing"
    }
    # The form is captured *under* the overlay, which is what proves the
    # controls stay mounted while the key is being read.
    overlay = next(state for state in states if PROCESSING_TEXT in state)
    assert KEY_LOADED_TEXT in overlay


async def test_console_and_page_errors_are_captured(tmp_path, monkeypatch):
    page = reset_journey()
    config = build_config(
        tmp_path, authentication_timeout_ms=LONG_CALLBACK_MS, monkeypatch=monkeypatch
    )
    ctx = build_context(config, page, tmp_path)

    with pytest.raises(AuthenticationFailed):
        await ctx.auth.ensure_authenticated()

    payload = json.loads(auth_artifacts(tmp_path)["console"].read_text(encoding="utf-8"))
    kinds = {message["kind"] for message in payload["messages"]}
    texts = " ".join(message["text"] for message in payload["messages"])

    assert "console.error" in kinds
    assert "0x8009000b" in texts
    # Routine chatter is not worth keeping.
    assert "routine chatter" not in texts


async def test_page_errors_are_captured(tmp_path, monkeypatch):
    page = reset_journey(page_error="boom in the page")
    config = build_config(
        tmp_path, authentication_timeout_ms=LONG_CALLBACK_MS, monkeypatch=monkeypatch
    )
    ctx = build_context(config, page, tmp_path)

    with pytest.raises(AuthenticationFailed):
        await ctx.auth.ensure_authenticated()

    payload = json.loads(auth_artifacts(tmp_path)["console"].read_text(encoding="utf-8"))
    assert any(message["kind"] == "pageerror" for message in payload["messages"])
    assert "boom in the page" in " ".join(m["text"] for m in payload["messages"])


async def test_safe_network_metadata_is_captured(tmp_path, monkeypatch):
    """Enough to see a failing XHR; nothing that could carry the session."""
    page = reset_journey()
    config = build_config(
        tmp_path, authentication_timeout_ms=LONG_CALLBACK_MS, monkeypatch=monkeypatch
    )
    ctx = build_context(config, page, tmp_path)

    with pytest.raises(AuthenticationFailed):
        await ctx.auth.ensure_authenticated()

    payload = json.loads(auth_artifacts(tmp_path)["network"].read_text(encoding="utf-8"))
    failed = payload["failed"]

    assert len(failed) == 1
    assert failed[0]["status"] == 400
    assert failed[0]["method"] == "POST"
    assert failed[0]["host"] == "id.gov.ua"
    assert failed[0]["content_type"] == "application/json"
    assert failed[0]["phase"] == "submit"
    # Only id.gov.ua is recorded, so third-party assets stay out of it.
    assert all(record["host"] == "id.gov.ua" for record in payload["responses"])
    # Which step provoked the traffic — this is what answers whether changing
    # the provider starts a request of its own. Every phase that ran is listed,
    # zero-filled: an absent key means the step never ran, not that nothing was
    # tagged. A bare {"submit": 1} could not tell those apart.
    assert payload["responses_by_phase"] == {
        "idgov": 0,
        "provider": 0,
        "upload": 0,
        "submit": 1,
        "processing": 0,
    }
    assert payload["phases_entered"] == [
        "idgov",
        "provider",
        "upload",
        "submit",
        "processing",
    ]


async def test_no_response_record_carries_a_url_query_string(tmp_path, monkeypatch):
    """The submit response URL carries an OIDC code. It must not be written."""
    page = reset_journey()
    config = build_config(
        tmp_path, authentication_timeout_ms=LONG_CALLBACK_MS, monkeypatch=monkeypatch
    )
    ctx = build_context(config, page, tmp_path)

    with pytest.raises(AuthenticationFailed):
        await ctx.auth.ensure_authenticated()

    written = "\n".join(
        path.read_text(encoding="utf-8")
        for path in auth_artifacts(tmp_path).values()
        if path.suffix == ".json"
    )
    assert "SECRET-CODE-VALUE" not in written
    assert "response_type" not in written
    assert "?" not in written or "code=" not in written
    # The path and the status survive — that is what a diagnostic needs.
    assert '"path"' in written and '"status"' in written


async def test_no_secret_reaches_any_post_submit_artifact(tmp_path, monkeypatch):
    page = reset_journey()
    config = build_config(
        tmp_path, authentication_timeout_ms=LONG_CALLBACK_MS, monkeypatch=monkeypatch
    )
    ctx = build_context(config, page, tmp_path)

    with pytest.raises(AuthenticationFailed):
        await ctx.auth.ensure_authenticated()

    for path in (tmp_path / "debug").rglob("*"):
        if path.is_file():
            content = path.read_text(encoding="utf-8", errors="ignore")
            assert PASSWORD not in content, path
            assert "not-a-real-key" not in content, path


async def test_the_observer_detaches_from_the_page(tmp_path, monkeypatch):
    """It listens for one journey, not for the life of the browser."""
    page = reset_journey()
    config = build_config(
        tmp_path, authentication_timeout_ms=LONG_CALLBACK_MS, monkeypatch=monkeypatch
    )
    ctx = build_context(config, page, tmp_path)

    with pytest.raises(AuthenticationFailed):
        await ctx.auth.ensure_authenticated()

    assert page.listener_count == 0


async def test_a_new_screen_after_submit_stops_with_its_own_status(
    tmp_path, monkeypatch
):
    """Outcome B: further than the key form, so it is not a rejection."""
    page = AuthJourney(callback_returns=False, advances_to_new_screen=True)
    config = build_config(
        tmp_path, authentication_timeout_ms=LONG_CALLBACK_MS, monkeypatch=monkeypatch
    )
    ctx = build_context(config, page, tmp_path)

    with pytest.raises(AuthIntermediateScreenReached) as exc:
        await ctx.auth.ensure_authenticated()

    message = str(exc.value)
    assert "moved on to a screen" in message
    assert "Nothing on the new screen was clicked" in message
    assert json.loads(
        auth_artifacts(tmp_path)["elements"].read_text(encoding="utf-8")
    )["outcome"] == "intermediate-screen"
    # It is still an authentication failure for every caller upstream.
    assert isinstance(exc.value, AuthenticationFailed)


async def test_an_unknown_screen_is_still_stopped_on_and_never_clicked(
    tmp_path, monkeypatch
):
    """Knowing «Перевірте дані» must not make every new screen "known"."""
    page = AuthJourney(callback_returns=False, advances_to_new_screen=True)
    config = build_config(
        tmp_path, authentication_timeout_ms=LONG_CALLBACK_MS, monkeypatch=monkeypatch
    )
    ctx = build_context(config, page, tmp_path)

    with pytest.raises(AuthIntermediateScreenReached):
        await ctx.auth.ensure_authenticated()

    assert "click:user_data_accept" not in page.actions
    assert "click:signer_continue" not in page.actions


# --------------------------------------------------------------------------- #
# «Перевірте дані» — the user-data confirmation screen
# --------------------------------------------------------------------------- #
#
# Measured on the live site: after the key is read and the password accepted,
# ID.GOV.UA shows what it extracted from the certificate and asks for it to be
# confirmed before it hands the browser back. It is a successful continuation,
# so it is confirmed and waited out — not a screen to stop on.


def user_data_journey(**kwargs: Any) -> AuthJourney:
    """Submit → overlay → «Перевірте дані» → (confirm) → callback."""
    return AuthJourney(callback_returns=False, advances_to_user_data=True, **kwargs)


async def test_the_user_data_screen_is_progress_and_authentication_completes(
    tmp_path, monkeypatch, caplog
):
    page = user_data_journey()
    config = build_config(
        tmp_path, authentication_timeout_ms=LONG_CALLBACK_MS, monkeypatch=monkeypatch
    )
    ctx = build_context(config, page, tmp_path)

    with caplog.at_level(logging.INFO):
        await ctx.auth.ensure_authenticated()

    assert page.url == CABINET
    assert "ID.GOV.UA key processing completed" in caplog.text
    assert (
        "ID.GOV.UA accepted the key and reached the user-data confirmation screen"
        in caplog.text
    )
    assert "Confirming ID.GOV.UA user data" in caplog.text
    assert "Authentication callback received" in caplog.text
    assert "HSC authenticated session established" in caplog.text
    # It is emphatically not the unknown-screen outcome any more.
    assert "advanced to an intermediate authentication screen" not in caplog.text
    # A success writes no failure artifacts.
    assert not (tmp_path / "debug" / "auth").exists()


async def test_the_confirmation_happens_between_the_submit_and_the_callback(
    tmp_path, monkeypatch, caplog
):
    page = user_data_journey()
    config = build_config(
        tmp_path, authentication_timeout_ms=LONG_CALLBACK_MS, monkeypatch=monkeypatch
    )
    ctx = build_context(config, page)

    with caplog.at_level(logging.INFO):
        await ctx.auth.ensure_authenticated()

    expected = [
        "Submitting authentication",
        "login.submit click completed",
        "Waiting for the ID.GOV.UA callback",
        "ID.GOV.UA accepted the key and reached the user-data confirmation screen",
        "Confirming ID.GOV.UA user data",
        "Authentication callback received",
        "ID.GOV.UA authentication completed",
    ]
    positions = [caplog.text.find(line) for line in expected]
    assert all(position >= 0 for position in positions), [
        line for line, position in zip(expected, positions, strict=True) if position < 0
    ]
    assert positions == sorted(positions)
    assert page.actions.index("click:submit") < page.actions.index(
        "click:user_data_accept"
    )


async def test_exactly_the_accept_button_is_clicked_and_by_its_id(
    tmp_path, monkeypatch
):
    """Not a generic «Продовжити», not the first button — that id and no other."""
    page = user_data_journey()
    config = build_config(
        tmp_path, authentication_timeout_ms=LONG_CALLBACK_MS, monkeypatch=monkeypatch
    )
    ctx = build_context(config, page)

    await ctx.auth.ensure_authenticated()

    assert page.actions.count("click:user_data_accept") == 1
    # How it was addressed, not merely what ended up being clicked: a CSS
    # lookup for the exact id, never a role/text query for "Продовжити".
    assert page.clicked_via["user_data_accept"] == ("locator", USER_DATA_ACCEPT_CSS)
    assert ("locator", (USER_DATA_ACCEPT_CSS,), {}) in page.calls


async def test_the_refuse_button_is_never_clicked_or_even_looked_up(
    tmp_path, monkeypatch
):
    """«Відмовитись» abandons the authentication. Nothing may go near it."""
    page = user_data_journey()
    config = build_config(
        tmp_path, authentication_timeout_ms=LONG_CALLBACK_MS, monkeypatch=monkeypatch
    )
    ctx = build_context(config, page)

    await ctx.auth.ensure_authenticated()

    assert "click:user_data_reset" not in page.actions
    assert "user_data_reset" not in page.clicked_via
    assert page._element("user_data_reset").attrs.get("clicked") is None
    assert USER_DATA_RESET_CSS not in [
        args[0] for api, args, _ in page.calls if api == "locator"
    ]


async def test_the_file_key_submit_selector_is_not_reused(tmp_path, monkeypatch):
    """Both screens carry «Продовжити», so the name identifies neither."""
    page = user_data_journey()
    config = build_config(
        tmp_path, authentication_timeout_ms=LONG_CALLBACK_MS, monkeypatch=monkeypatch
    )
    ctx = build_context(config, page)

    await ctx.auth.ensure_authenticated()

    submit = config.selectors.require("login.submit")
    accept = config.selectors.require("login.user_data_accept")
    assert (submit.strategy, submit.name) == ("role", "Продовжити")
    assert (accept.strategy, accept.value) == ("css", USER_DATA_ACCEPT_CSS)
    assert accept.value != USER_DATA_RESET_CSS
    # login.submit was used once, on the key form, and never again.
    assert page.actions.count("click:submit") == 1
    assert page.clicked_via["submit"] == ("get_by_role", "Продовжити")


async def test_a_confirmed_screen_that_never_calls_back_fails_with_evidence(
    tmp_path, monkeypatch
):
    """Requirement 4, negatively: the callback is awaited, not assumed."""
    page = user_data_journey(user_data_callback_returns=False)
    config = build_config(
        tmp_path, authentication_timeout_ms=2_000, monkeypatch=monkeypatch
    )
    ctx = build_context(config, page, tmp_path)

    with pytest.raises(FlowError) as exc:
        await ctx.auth.ensure_authenticated()

    message = str(exc.value)
    assert "authentication callback" in message
    assert "user-data confirmation screen was confirmed" in message
    # Confirmed once. A screen that stays put is not clicked again per poll.
    assert page.actions.count("click:user_data_accept") == 1
    # And it is not reported as a rejection or as an unknown screen.
    assert not isinstance(exc.value, AuthenticationFailed)
    assert "returned to the file-key form" not in message

    artifacts = auth_artifacts(tmp_path)
    assert set(artifacts) == {"screenshot", "elements", "text", "console", "network"}
    for path in artifacts.values():
        assert str(path) in message, f"{path.name} must be named in the error"
    # The evidence says which step the browser was on when it stalled.
    network = json.loads(artifacts["network"].read_text(encoding="utf-8"))
    assert "user-data" in network["phases_entered"]


async def test_success_still_requires_the_cabinet_url_and_the_marker(
    tmp_path, monkeypatch
):
    """Confirming the user data is not itself evidence of a session."""
    page = user_data_journey()
    # The callback lands, the cabinet renders — without «Записатись у чергу».
    page.screens["cabinet"] = []
    config = build_config(
        tmp_path, authentication_timeout_ms=LONG_CALLBACK_MS, monkeypatch=monkeypatch
    )
    ctx = build_context(config, page, tmp_path)

    with pytest.raises(AuthenticationFailed) as exc:
        await ctx.auth.ensure_authenticated()

    message = str(exc.value)
    assert "still does not show an authenticated cabinet" in message
    assert CABINET in message
    # It really did get all the way through the ID.GOV.UA journey first.
    assert "click:user_data_accept" in page.actions
    assert list((tmp_path / "debug" / "errors").glob("*auth-verification-failed*"))


async def test_a_reset_to_the_key_form_still_fails(tmp_path, monkeypatch, caplog):
    """The new screen must not turn the known rejection into a pass."""
    page = reset_journey()
    config = build_config(
        tmp_path, authentication_timeout_ms=LONG_CALLBACK_MS, monkeypatch=monkeypatch
    )
    ctx = build_context(config, page, tmp_path)

    with caplog.at_level(logging.INFO), pytest.raises(AuthenticationFailed) as exc:
        await ctx.auth.ensure_authenticated()

    assert "returned to the file-key form" in str(exc.value)
    assert "click:user_data_accept" not in page.actions
    assert "Confirming ID.GOV.UA user data" not in caplog.text


async def test_no_personal_data_from_the_screen_reaches_the_logs(
    tmp_path, monkeypatch, caplog
):
    """The log identifies the screen, never the person on it.

    Diagnostics still capture the page structurally, as they do for every other
    screen; what must not happen is personal data in ordinary output.
    """
    page = user_data_journey()
    config = build_config(
        tmp_path, authentication_timeout_ms=LONG_CALLBACK_MS, monkeypatch=monkeypatch
    )
    ctx = build_context(config, page, tmp_path)

    with caplog.at_level(logging.DEBUG):
        await ctx.auth.ensure_authenticated()

    for personal in (PERSONAL_NAME, PERSONAL_TAX_ID, PERSONAL_ADDRESS):
        assert personal not in caplog.text
    assert PASSWORD not in caplog.text
    # The screen is still named — semantically.
    assert "user-data confirmation screen" in caplog.text


# --------------------------------------------------------------------------- #
# Interrupted authentication (a stale ID.GOV.UA hand-over)
# --------------------------------------------------------------------------- #


async def test_a_cabinet_that_redirects_to_idgov_restarts_the_journey(
    tmp_path, monkeypatch, caplog
):
    """A dead attempt leaves /cabinet redirecting into an OIDC request.

    That is "authentication required", not "unexpected page" — and the stale
    authorization screen is left alone rather than driven.
    """
    page = AuthJourney(oidc_redirects=1)
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx = build_context(config, page, tmp_path)

    with caplog.at_level(logging.INFO):
        await ctx.auth.ensure_authenticated()

    assert "a previous authentication was interrupted" in caplog.text
    assert "Restarting the authentication journey" in caplog.text
    # It went back to the HSC entry page and walked the normal journey once.
    assert page.navigations == [CABINET, "https://eqn.hsc.gov.ua", CABINET]
    assert page.actions == JOURNEY
    assert page.journeys == 1
    assert page.url == CABINET


async def test_a_persistent_idgov_redirect_fails_without_a_retry_loop(
    tmp_path, monkeypatch
):
    page = AuthJourney(oidc_redirects=99)
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx = build_context(config, page, tmp_path)

    with pytest.raises(AuthenticationFailed) as exc:
        await ctx.auth.ensure_authenticated()

    assert "No second restart was attempted" in str(exc.value)
    # Exactly one recovery navigation, then it stopped.
    assert page.navigations.count("https://eqn.hsc.gov.ua") == 1
    assert page.actions == []
    assert page.journeys == 0


# --------------------------------------------------------------------------- #
# Uploading through the operating system's own dialog (production)
# --------------------------------------------------------------------------- #


def native_context(page: AuthJourney, config: AppConfig, tmp_path: Path | None = None
                   ) -> tuple[FlowContext, FakeNativeFileSelector]:
    selector = FakeNativeFileSelector(page)
    return build_context(config, page, tmp_path, file_selector=selector), selector


async def test_production_selects_the_key_in_the_native_dialog(tmp_path, monkeypatch):
    """The only mechanism ID.GOV.UA has been observed to accept."""
    page = AuthJourney()
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx, selector = native_context(page, config)

    await ctx.auth.ensure_authenticated()

    assert selector.selected, "the OS dialog was never driven"
    assert page.actions == JOURNEY
    assert page.url == CABINET


async def test_the_native_dialog_is_driven_after_the_control_is_clicked(
    tmp_path, monkeypatch
):
    """The click is what makes the browser open its picker; order matters."""
    page = AuthJourney()
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx, _ = native_context(page, config)

    await ctx.auth.ensure_authenticated()

    assert page.actions.index("click:key_file_trigger") < page.actions.index(
        "native:select"
    )
    assert page.actions.index("native:select") < page.actions.index("upload:key_file")


async def test_the_native_selector_receives_the_configured_key_path(
    tmp_path, monkeypatch
):
    key = tmp_path / "keys" / "Key-6.dat"
    key.parent.mkdir()
    key.write_bytes(b"not-a-real-key")
    page = AuthJourney()
    config = build_config(tmp_path, key_path=key, monkeypatch=monkeypatch)
    ctx, selector = native_context(page, config)

    await ctx.auth.ensure_authenticated()

    assert selector.selected == [str(key)]
    assert page.uploaded == [str(key)]


async def test_the_native_path_never_arms_playwrights_chooser(tmp_path, monkeypatch):
    """Listening for the chooser is what suppresses the native panel."""
    page = AuthJourney()
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx, _ = native_context(page, config)

    await ctx.auth.ensure_authenticated()

    assert "arm:file_chooser" not in page.actions
    assert page.pending_file_chooser is None


async def test_a_dialog_that_cannot_be_driven_fails_without_a_fallback(
    tmp_path, monkeypatch
):
    """No in-page fallback: that mechanism is the one known to be rejected."""
    page = AuthJourney()
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    selector = FakeNativeFileSelector(page, fails="No native file dialog appeared")
    ctx = build_context(config, page, tmp_path, file_selector=selector)

    with pytest.raises(FlowError) as exc:
        await ctx.auth.ensure_authenticated()

    message = str(exc.value)
    assert "No native file dialog appeared" in message
    assert "NOT written into the input as a fallback" in message

    assert page.uploaded == []
    assert "arm:file_chooser" not in page.actions
    assert "fill:password" not in page.actions
    assert "click:submit" not in page.actions
    dumps = list((tmp_path / "debug").glob("*auth-native-file-dialog-elements.json"))
    assert dumps and str(dumps[0]) in message


async def test_the_input_is_never_written_to_directly(tmp_path, monkeypatch):
    """set_input_files() on #PKeyFileInput is the known-broken mechanism."""
    page = AuthJourney()
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx, _ = native_context(page, config)

    def fail(*_a: Any, **_k: Any) -> None:
        raise AssertionError("BasePage.set_files() must not run in the auth journey")

    monkeypatch.setattr(BasePage, "set_files", fail)

    await ctx.auth.ensure_authenticated()

    assert page.uploaded, "the key still reached the page, via the OS dialog"


# --------------------------------------------------------------------------- #
# Uploading through Playwright's chooser (kept for A/B comparison)
# --------------------------------------------------------------------------- #


def chooser_config(tmp_path: Path, **kwargs: Any) -> AppConfig:
    return build_config(tmp_path, file_selection="chooser", **kwargs)


async def test_the_upload_goes_through_the_visible_control(
    tmp_path, monkeypatch, caplog
):
    page = AuthJourney()
    ctx = build_context(chooser_config(tmp_path, monkeypatch=monkeypatch), page)

    with caplog.at_level(logging.INFO):
        await ctx.auth.ensure_authenticated()

    assert page.actions == CHOOSER_JOURNEY
    assert "ID.GOV.UA requested key file selection" in caplog.text
    # The file arrived through the chooser the page opened in response.
    assert page.pending_file_chooser is not None
    assert page.pending_file_chooser.files == page.uploaded[0]


async def test_the_chooser_listener_is_armed_before_the_click(tmp_path, monkeypatch):
    """The event fires *during* the click, so listening afterwards misses it."""
    page = AuthJourney()
    ctx = build_context(chooser_config(tmp_path, monkeypatch=monkeypatch), page)

    await ctx.auth.ensure_authenticated()

    assert page.actions.index("arm:file_chooser") < page.actions.index(
        "click:key_file_trigger"
    )
    assert page.actions.index("click:key_file_trigger") < page.actions.index(
        "upload:key_file"
    )


async def test_the_chooser_receives_the_configured_key_path(tmp_path, monkeypatch):
    key = tmp_path / "keys" / "Key-6.dat"
    key.parent.mkdir()
    key.write_bytes(b"not-a-real-key")
    page = AuthJourney()
    ctx = build_context(
        chooser_config(tmp_path, key_path=key, monkeypatch=monkeypatch), page
    )

    await ctx.auth.ensure_authenticated()

    assert page.pending_file_chooser is not None
    assert page.pending_file_chooser.files == str(key)
    assert page.uploaded == [str(key)]


def test_the_production_upload_does_not_call_set_input_files():
    """Regression guard: this is what the A/B test disproved.

    A future refactor that "simplifies" the chooser dance back into a direct
    write would look tidier and fail in a way nothing explains — the key is
    read, the form resets, and the cause is three screens upstream.
    """
    source = Path(hsc_queue_monitor.__file__).parent / "pages" / "login_page.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    # Real calls only — the docstrings name the rejected mechanism on purpose.
    direct_writes = []
    called = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        attribute = node.func.attr
        called.add(attribute)
        on_self = isinstance(node.func.value, ast.Name) and node.func.value.id == "self"
        # chooser.set_files() is the *new* mechanism; self.set_files() is the old one.
        if attribute == "set_input_files" or (attribute == "set_files" and on_self):
            direct_writes.append(f"line {node.lineno}: {attribute}()")

    assert direct_writes == []
    assert "expect_file_chooser" in called


async def test_the_chooser_is_verified_against_the_private_key_input(
    tmp_path, monkeypatch, caplog
):
    page = AuthJourney()
    ctx = build_context(chooser_config(tmp_path, monkeypatch=monkeypatch), page)

    with caplog.at_level(logging.DEBUG):
        await ctx.auth.ensure_authenticated()

    assert page.pending_file_chooser is not None
    assert page.pending_file_chooser.element.element_id == "PKeyFileInput"
    assert "File chooser belongs to #PKeyFileInput" in caplog.text


async def test_a_chooser_for_the_certificates_input_is_refused(tmp_path, monkeypatch):
    """The screen's other file input must never receive the private key."""
    page = AuthJourney(chooser_input_id="ChoosePKCertsInput")
    config = chooser_config(tmp_path, monkeypatch=monkeypatch)
    ctx = build_context(config, page, tmp_path)

    with pytest.raises(FlowError) as exc:
        await ctx.auth.ensure_authenticated()

    message = str(exc.value)
    assert "#ChoosePKCertsInput" in message
    assert "#PKeyFileInput" in message
    assert "Nothing was uploaded" in message

    assert page.uploaded == []
    assert page.pending_file_chooser is not None
    assert page.pending_file_chooser.files is None
    assert "fill:password" not in page.actions
    assert "click:submit" not in page.actions


async def test_no_file_chooser_fails_without_a_direct_upload_fallback(
    tmp_path, monkeypatch
):
    """Falling back to the direct write would reintroduce the known failure."""
    page = AuthJourney(opens_file_chooser=False)
    config = chooser_config(tmp_path, monkeypatch=monkeypatch)
    ctx = build_context(config, page, tmp_path)

    with pytest.raises(FlowError) as exc:
        await ctx.auth.ensure_authenticated()

    message = str(exc.value)
    assert "did not make ID.GOV.UA ask for a file" in message
    assert "deliberately NOT written into the input" in message

    assert page.uploaded == []
    assert "fill:password" not in page.actions
    assert "click:submit" not in page.actions
    # Diagnostics were saved and named.
    dumps = list((tmp_path / "debug").glob("*auth-no-file-chooser-elements.json"))
    assert dumps and str(dumps[0]) in message


async def test_the_key_loaded_wait_still_gates_the_password(tmp_path, monkeypatch):
    """The chooser changed how the file arrives, not what happens after it."""
    page = AuthJourney(key_loads=False)
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx = build_context(config, page, tmp_path)

    with pytest.raises(FlowError, match="login.key_loaded never appeared"):
        await ctx.auth.ensure_authenticated()

    assert page.uploaded, "the chooser was still answered"
    assert "fill:password" not in page.actions
    assert "click:submit" not in page.actions


async def test_the_provider_is_still_selected_before_the_upload(tmp_path, monkeypatch):
    """The provider decides how the key is read, so it still comes first."""
    page = AuthJourney()
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx, _ = native_context(page, config)

    await ctx.auth.ensure_authenticated()

    assert page.actions.index("select:provider") < page.actions.index(
        "click:key_file_trigger"
    )


async def test_the_processing_flow_is_unchanged_by_the_new_upload(
    tmp_path, monkeypatch, caplog
):
    page = AuthJourney(callback_returns=False, resets_to_key_form=True)
    config = build_config(
        tmp_path, authentication_timeout_ms=LONG_CALLBACK_MS, monkeypatch=monkeypatch
    )
    ctx = build_context(config, page, tmp_path)

    with caplog.at_level(logging.INFO), pytest.raises(AuthenticationFailed):
        await ctx.auth.ensure_authenticated()

    assert "ID.GOV.UA is reading the private key" in caplog.text
    assert "ID.GOV.UA key processing completed" in caplog.text
    assert set(auth_artifacts(tmp_path)) == {
        "screenshot", "elements", "text", "console", "network"
    }


async def test_no_secret_leaks_through_the_new_upload_path(
    tmp_path, monkeypatch, caplog
):
    page = AuthJourney()
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx = build_context(config, page, tmp_path)

    with caplog.at_level(logging.DEBUG):
        await ctx.auth.ensure_authenticated()

    assert PASSWORD not in caplog.text
    assert "<redacted>" in caplog.text
    # The basename is fine to log; the contents are never read.
    assert "masterkey.dat" in caplog.text
    assert "not-a-real-key" not in caplog.text
    for path in (tmp_path / "debug").rglob("*"):
        if path.is_file():
            content = path.read_text(encoding="utf-8", errors="ignore")
            assert PASSWORD not in content
            assert "not-a-real-key" not in content


# --------------------------------------------------------------------------- #
# The two file inputs
# --------------------------------------------------------------------------- #


async def test_the_key_file_selector_targets_the_private_key_input(tmp_path, monkeypatch):
    """#PKeyFileInput takes the .dat; #ChoosePKCertsInput must stay untouched."""
    key = tmp_path / "Key-6.dat"
    key.write_bytes(b"not-a-real-key")
    page = AuthJourney()
    ctx = build_context(build_config(tmp_path, key_path=key, monkeypatch=monkeypatch), page)

    await ctx.auth.ensure_authenticated()

    assert page._element("key_file").attrs["files"] == str(key)
    assert "files" not in page._element("certificates").attrs
    assert page.actions.count("upload:key_file") == 1
    assert "upload:certificates" not in page.actions


async def test_a_broad_file_selector_is_ambiguous_across_the_two_inputs(
    tmp_path, monkeypatch
):
    """Why the id is required: the screen has two file inputs, not one.

    The ambiguity is reported instead of being resolved by position — an `nth:`
    would pick whichever input the site happens to render first. login.key_file
    is no longer the upload mechanism, but it is still the identity the file
    chooser is checked against, so it has to name exactly one input.
    """
    selectors = SELECTORS.replace("value: '#PKeyFileInput'", "value: 'input[type=\"file\"]'")
    page = AuthJourney()
    page.screen = "idgov_file"  # the screen that holds both file inputs
    config = build_config(tmp_path, selectors_yaml=selectors, monkeypatch=monkeypatch)
    ctx = build_context(config, page)

    with pytest.raises(LocatorAmbiguous) as exc:
        await ctx.login.resolve(LoginPage.KEY_FILE)

    message = str(exc.value)
    assert "login.key_file" in message
    assert "PKeyFileInput" in message
    assert "ChoosePKCertsInput" in message
    assert page.uploaded == []


# --------------------------------------------------------------------------- #
# No sleeping through the problem
# --------------------------------------------------------------------------- #


def test_nothing_forces_a_click_or_clicks_through_javascript():
    """A click that has to be forced is a click on something not ready.

    The live evidence says the click already lands; making it louder would only
    hide the state that follows it.
    """
    banned = (
        "force=True",
        "force = True",
        ".dispatch_event(",
        "el.click()",
        "element.click()",
        "HTMLElement.prototype.click",
    )
    offenders = []
    for path in sorted(Path(hsc_queue_monitor.__file__).parent.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        offenders += [f"{path.name}: {needle}" for needle in banned if needle in source]

    assert offenders == []


def test_the_authentication_path_contains_no_fixed_sleeps():
    """Every wait added for timing must be a condition poll.

    A `sleep(3)` "to let the key load" would pass on a fast day and fail on a
    slow one, and would hide exactly the state this work exists to observe.
    The only sleeps allowed are the poll intervals *between* condition checks.
    """
    allowed = {"_POLL_INTERVAL_MS / 1000", "2", "delay"}
    pattern = re.compile(r"(asyncio\.sleep|time\.sleep)\(([^)]*)\)")

    offenders = []
    for path in sorted(Path(hsc_queue_monitor.__file__).parent.rglob("*.py")):
        for call, argument in pattern.findall(path.read_text(encoding="utf-8")):
            if call == "time.sleep" or argument.strip() not in allowed:
                offenders.append(f"{path.name}: {call}({argument})")

    assert offenders == []


# --------------------------------------------------------------------------- #
# Configuration failures
# --------------------------------------------------------------------------- #


async def test_a_missing_key_path_fails_before_the_journey_starts(tmp_path):
    page = AuthJourney()
    ctx = build_context(build_config(tmp_path), page)

    with pytest.raises(ConfigError, match="IDGOV_SIGNING_KEY_PATH is not set"):
        await ctx.auth.ensure_authenticated()

    assert page.actions == []
    assert page.uploaded == []


async def test_a_nonexistent_key_file_fails_before_the_journey_starts(
    tmp_path, monkeypatch
):
    page = AuthJourney()
    missing = tmp_path / "not-there.dat"
    monkeypatch.setenv("IDGOV_SIGNING_KEY_PATH", str(missing))
    monkeypatch.setenv("IDGOV_SIGNING_KEY_PASSWORD", PASSWORD)
    ctx = build_context(build_config(tmp_path), page)

    with pytest.raises(ConfigError, match="does not exist"):
        await ctx.auth.ensure_authenticated()

    assert page.actions == []


async def test_a_missing_password_fails_before_the_journey_starts(tmp_path, monkeypatch):
    page = AuthJourney()
    config = build_config(tmp_path, password="", monkeypatch=monkeypatch)
    ctx = build_context(config, page)

    with pytest.raises(ConfigError, match="IDGOV_SIGNING_KEY_PASSWORD is not set"):
        await ctx.auth.ensure_authenticated()

    assert page.actions == []
    assert page.uploaded == []


async def test_an_unconfigured_selector_names_the_auth_inspector(tmp_path, monkeypatch):
    selectors = SELECTORS.replace(f'value: "{PASSWORD_LABEL}"', 'value: "TODO"')
    page = AuthJourney()
    config = build_config(tmp_path, selectors_yaml=selectors, monkeypatch=monkeypatch)
    ctx = build_context(config, page)

    with pytest.raises(SelectorNotConfigured) as exc:
        await ctx.auth.ensure_authenticated()

    assert "inspect-auth" in str(exc.value)
    # It got as far as the key upload, which is what tells you where to look.
    assert page.uploaded


# --------------------------------------------------------------------------- #
# Web-signature component
# --------------------------------------------------------------------------- #


async def test_a_missing_signing_component_stops_the_journey(tmp_path, monkeypatch):
    selectors = SELECTORS.replace(
        "  signature_unavailable:\n    strategy: text\n    value: \"TODO\"",
        f'  signature_unavailable:\n    strategy: text\n    value: "{NO_LIBRARY_TEXT}"',
    )
    page = AuthJourney(library_missing=True)
    config = build_config(tmp_path, selectors_yaml=selectors, monkeypatch=monkeypatch)
    ctx = build_context(config, page, tmp_path)

    with pytest.raises(SignatureExtensionUnavailable) as exc:
        await ctx.auth.ensure_authenticated()

    assert "web-signature component" in str(exc.value)
    assert NO_LIBRARY_TEXT in str(exc.value)
    # No fallback, no retry: it stopped on the method screen.
    assert page.actions == ["click:terms", "click:idgov"]
    assert page.uploaded == []
    assert page.journeys == 1
    assert list((tmp_path / "debug").glob("*.png")), "diagnostics should be saved"


async def test_an_unconfigured_warning_selector_does_not_block_the_journey(
    tmp_path, monkeypatch
):
    """The detector is optional — a TODO must not stop a working login."""
    page = AuthJourney(library_missing=True)
    ctx = build_context(build_config(tmp_path, monkeypatch=monkeypatch), page)

    await ctx.auth.ensure_authenticated()
    assert page.actions == JOURNEY


# --------------------------------------------------------------------------- #
# Failure after the journey
# --------------------------------------------------------------------------- #


async def test_a_session_that_does_not_take_is_not_retried(tmp_path, monkeypatch, caplog):
    page = AuthJourney(signing_succeeds=False)
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx = build_context(config, page, tmp_path)

    with caplog.at_level(logging.INFO), pytest.raises(AuthenticationFailed) as exc:
        await ctx.auth.ensure_authenticated()

    assert "No second login was attempted" in str(exc.value)
    assert page.journeys == 1
    assert page.actions.count("click:submit") == 1
    assert "HSC authenticated session established" not in caplog.text
    assert list((tmp_path / "debug" / "errors").glob("*.json"))


async def test_a_callback_that_never_returns_fails_clearly(tmp_path, monkeypatch):
    page = AuthJourney(callback_returns=False)
    ctx = build_context(build_config(tmp_path, monkeypatch=monkeypatch), page)

    with pytest.raises(FlowError, match="authentication callback"):
        await ctx.auth.ensure_authenticated()

    assert page.journeys == 1


async def test_an_unexpected_page_is_not_treated_as_a_login_screen(tmp_path, monkeypatch):
    page = AuthJourney()

    async def elsewhere(url: str, **_k: Any) -> None:
        page.calls.append(("goto", (url,), {}))
        page.url = "https://example.test/maintenance"

    page.goto = elsewhere  # type: ignore[method-assign]
    ctx = build_context(build_config(tmp_path, monkeypatch=monkeypatch), page, tmp_path)

    with pytest.raises(AuthenticationFailed, match="neither the authenticated cabinet"):
        await ctx.auth.ensure_authenticated()

    assert page.actions == []


async def test_concurrent_callers_authenticate_only_once(tmp_path, monkeypatch):
    """The lock is what keeps two cycles from racing into two logins."""
    page = AuthJourney()
    ctx = build_context(build_config(tmp_path, monkeypatch=monkeypatch), page)

    await asyncio.gather(
        ctx.auth.ensure_authenticated(),
        ctx.auth.ensure_authenticated(),
    )

    assert page.journeys == 1
    assert page.actions == JOURNEY


# --------------------------------------------------------------------------- #
# Integration: check-center
# --------------------------------------------------------------------------- #


def cabinet_screen() -> list[FakeElement]:
    """The controls check-center clicks once it is inside the cabinet."""
    extras = [
        element(name, tag="button", text=name) for name in CABINET_PREREQUISITES
    ]
    extras.append(
        element("search", tag="input", placeholder="Пошук сервісного центру МВС")
    )
    extras.append(element("center", tag="button", text=CENTER_3242.full_name))
    return extras


async def test_check_center_authenticates_before_the_prerequisites(
    tmp_path, monkeypatch, capsys
):
    page = AuthJourney(cabinet_extras=cabinet_screen())
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx = build_context(config, page)

    code = await run_check_center(config, ctx, service_center_id="3242")

    assert code == 0
    assert "Available:  YES" in capsys.readouterr().out

    # The whole journey happened, and it happened before the first cabinet click.
    assert page.actions[: len(JOURNEY)] == JOURNEY

    clicks = [a for a in page.actions[len(JOURNEY):] if a.startswith("click:")]
    assert clicks[:4] == [
        # queue.start_registration is the same link as the authenticated
        # marker, so the fake records it under that element's name.
        "click:authenticated_marker",
        *(f"click:{name}" for name in CABINET_PREREQUISITES),
    ]


async def test_check_center_with_a_live_session_only_checks_the_marker(
    tmp_path, monkeypatch, capsys
):
    """A valid session must cost one marker check and one navigation, no more."""
    page = AuthJourney(authenticated=True, cabinet_extras=cabinet_screen())
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx = build_context(config, page)

    code = await run_check_center(config, ctx, service_center_id="3242")

    assert code == 0
    assert page.navigations == [CABINET], "the cabinet must not be opened twice"
    assert page.journeys == 0
    assert page.uploaded == []
    assert not [a for a in page.actions if a in JOURNEY]


async def test_check_center_recovers_a_session_that_expired(tmp_path, monkeypatch):
    """The point of the whole feature: no separate login command first."""
    page = AuthJourney(cabinet_extras=cabinet_screen())
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx = build_context(config, page)

    code = await run_check_center(config, ctx, service_center_id="3242")

    assert code == 0
    assert page.journeys == 1
    assert page.url.startswith(CABINET)


async def test_check_center_reports_a_failed_login_as_an_auth_error(
    tmp_path, monkeypatch, capsys
):
    page = AuthJourney(signing_succeeds=False, cabinet_extras=cabinet_screen())
    config = build_config(tmp_path, monkeypatch=monkeypatch)
    ctx = build_context(config, page)

    code = await run_check_center(config, ctx, service_center_id="3242")

    assert code == 1
    err = capsys.readouterr().err
    assert "AuthenticationFailed" in err
    # The old symptom must not come back.
    assert "queue.start_registration" not in err
