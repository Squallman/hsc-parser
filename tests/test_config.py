"""Configuration loading, validation and TODO detection."""

from __future__ import annotations

import pytest
import yaml

from hsc_queue_monitor.config import (
    MIN_POLL_INTERVAL_SECONDS,
    SECRET_ENV_VARS,
    AppConfig,
    AppSettings,
    BrowserMonitorConfig,
    FlowConfig,
    MongoConfig,
    Paths,
    SecretSettings,
    SelectorRegistry,
    TelegramConfig,
    enabled_service_centers,
    load_secrets,
    load_service_centers,
)
from hsc_queue_monitor.models import ConfigError, SelectorNotConfigured, ServiceCenter


def registry(text: str) -> SelectorRegistry:
    return SelectorRegistry.from_dict(yaml.safe_load(text))


# --------------------------------------------------------------------------- #
# Selectors
# --------------------------------------------------------------------------- #


def test_shipped_selectors_file_loads(config_dir):
    """The template that ships with the repo must always parse."""
    selectors = SelectorRegistry.from_file(config_dir / "selectors.yaml")
    assert "login.key_file" in selectors
    assert "calendar.available_slot" in selectors


def test_shipped_key_file_selector_names_the_private_key_input(config_dir):
    """The screen has two file inputs; only the id says which one is the key.

    Position is not an acceptable answer here — an `nth:` would follow whatever
    order the site renders #PKeyFileInput and #ChoosePKCertsInput in. This is
    no longer the upload mechanism, but it is the identity the file chooser is
    checked against before the key is handed over.
    """
    spec = SelectorRegistry.from_file(config_dir / "selectors.yaml").require("login.key_file")
    assert spec.strategy == "css"
    assert spec.value == "#PKeyFileInput"
    assert spec.value != "#ChoosePKCertsInput"
    assert spec.nth is None
    # The input is hidden behind the site's own upload widget.
    assert spec.visible is False


def test_shipped_key_file_trigger_is_the_visible_control(config_dir):
    """The upload is driven by the words a person clicks, not by the input.

    Writing to the input directly leaves ID.GOV.UA unable to use the key —
    established by A/B test — so the visible control is production.
    """
    spec = SelectorRegistry.from_file(config_dir / "selectors.yaml").require(
        "login.key_file_trigger"
    )
    assert spec.strategy == "text"
    assert spec.value == "оберіть його на своєму носієві"
    assert spec.exact is True
    assert spec.visible is True


def test_shipped_key_loaded_selector_does_not_depend_on_the_filename(config_dir):
    """It marks "the key was accepted", so it must survive a renamed .dat."""
    spec = SelectorRegistry.from_file(config_dir / "selectors.yaml").require(
        "login.key_loaded"
    )
    assert spec.strategy == "text"
    assert spec.value == "Завантажити інший файл"
    assert spec.exact is True
    assert ".dat" not in (spec.value or "")
    assert "Key-6" not in (spec.value or "")


def test_the_processing_selector_is_text_not_the_duplicated_id(config_dir):
    """The live page has two elements sharing #dimmerViewMessageLabel.

    An id that identifies two different messages identifies neither, so the
    key-reading state is matched by its own text instead.
    """
    spec = SelectorRegistry.from_file(config_dir / "selectors.yaml").require(
        "login.processing"
    )
    assert spec.strategy == "text"
    assert spec.value == "Зчитування особистого ключа"
    assert spec.exact is True
    assert "dimmerViewMessageLabel" not in (spec.value or "")


def test_the_idgov_error_selector_is_left_unconfigured(config_dir):
    """No real rejection has been captured, so it stays a TODO rather than a guess.

    A guessed wording would look like a working detector while never matching.
    Optional + TODO means the journey falls back to the timeout capture, which
    is what will provide the real value.
    """
    selectors = SelectorRegistry.from_file(config_dir / "selectors.yaml")
    assert selectors.get("login.auth_error").optional is True
    assert selectors.optional("login.auth_error") is None
    assert "login.auth_error" in selectors.todo_keys()


def test_shipped_user_data_accept_selector_is_the_exact_button_id(config_dir):
    """«Перевірте дані» and the key form both carry a «Продовжити».

    The accessible name therefore identifies neither of them, and the screen's
    other button («Відмовитись») abandons the authentication — so this is the
    one place where only an exact id will do, and it must not be login.submit.
    """
    selectors = SelectorRegistry.from_file(config_dir / "selectors.yaml")
    spec = selectors.require("login.user_data_accept")

    assert spec.strategy == "css"
    assert spec.value == "#btnAcceptUserDataAgreement"
    assert spec.value != "#btnResetUserDataAgreement"
    assert spec.nth is None

    submit = selectors.require("login.submit")
    assert submit.name == "Продовжити"
    assert (spec.strategy, spec.value) != (submit.strategy, submit.value)


def test_shipped_user_data_screen_marker_is_wording_not_identity(config_dir):
    """A heading is a second opinion; the button id above is the identity."""
    spec = SelectorRegistry.from_file(config_dir / "selectors.yaml").require(
        "login.user_data_screen"
    )
    assert spec.strategy == "text"
    assert spec.value == "Перевірте дані"
    assert spec.exact is True
    assert spec.optional is True


def test_shipped_provider_selector_names_the_knedp_dropdown(config_dir):
    spec = SelectorRegistry.from_file(config_dir / "selectors.yaml").require("login.provider")
    assert spec.strategy == "css"
    assert spec.value == "#CAsServersSelect"
    assert spec.nth is None


def test_todo_selector_raises_selector_not_configured():
    selectors = registry(
        """
        calendar:
          available_slot:
            strategy: css
            value: "TODO"
        """
    )
    with pytest.raises(SelectorNotConfigured) as exc:
        selectors.require("calendar.available_slot")

    message = str(exc.value)
    assert "calendar.available_slot has not been configured" in message
    # The error must tell the user how to fix it.
    assert "inspect" in message
    assert "test-step calendar.available_slot" in message


def test_optional_returns_none_for_todo_and_missing():
    selectors = registry(
        """
        login:
          challenge:
            strategy: text
            value: "TODO"
          submit:
            strategy: role
            role: button
            name: "Увійти"
        """
    )
    assert selectors.optional("login.challenge") is None
    assert selectors.optional("login.nonexistent") is None
    assert selectors.optional("login.submit") is not None


def test_todo_keys_partition_the_shipped_selectors(config_dir):
    """Asserted as an invariant, not by name: the repo's TODOs shrink over time."""
    selectors = SelectorRegistry.from_file(config_dir / "selectors.yaml")
    todo, configured = selectors.todo_keys(), selectors.configured_keys()

    assert sorted(todo + configured) == sorted(selectors)
    assert not set(todo) & set(configured)
    assert all(selectors.get(key).is_todo for key in todo)
    assert all(not selectors.get(key).is_todo for key in configured)
    # The file input is a real guess, not a placeholder.
    assert "login.key_file" not in todo


def test_role_strategy_requires_role_and_name():
    with pytest.raises(ConfigError, match="requires a `role:` key"):
        registry("login:\n  submit:\n    strategy: role\n    name: Go")
    with pytest.raises(ConfigError, match="requires a `name:` key"):
        registry("login:\n  submit:\n    strategy: role\n    role: button")


def test_unknown_strategy_is_rejected():
    with pytest.raises(ConfigError, match="strategy must be one of"):
        registry("login:\n  submit:\n    strategy: xpath\n    value: //button")


def test_unknown_option_is_rejected():
    with pytest.raises(ConfigError, match="unknown selector option"):
        registry("login:\n  submit:\n    strategy: css\n    value: '#a'\n    wait: 5")


def test_negative_nth_is_rejected():
    with pytest.raises(ConfigError, match="non-negative integer"):
        registry("login:\n  submit:\n    strategy: css\n    value: '#a'\n    nth: -1")


def test_unknown_selector_key_lists_known_keys():
    selectors = registry("login:\n  submit:\n    strategy: css\n    value: '#a'")
    with pytest.raises(ConfigError, match="login.submit"):
        selectors.get("login.typo")


# --------------------------------------------------------------------------- #
# Flow
# --------------------------------------------------------------------------- #


def test_shipped_flow_file_loads(config_dir):
    flow = FlowConfig.from_file(config_dir / "flow.yaml")
    assert flow.queue_url.endswith("/cabinet/queue")
    assert flow.login_enabled is True
    assert flow.queue_steps  # order is the user's to change; it must not be empty
    assert flow.timeouts.default_locator > 0


def test_flow_steps_must_be_strings():
    with pytest.raises(ConfigError, match="list of step names"):
        FlowConfig.from_dict({"flow": {"queue": {"steps": [{"name": "x"}]}}})


def test_flow_defaults_when_sections_absent():
    flow = FlowConfig.from_dict({})
    assert flow.queue_steps == ()
    assert flow.debug.screenshots is True


# --------------------------------------------------------------------------- #
# Authentication (the КНЕДП of the MasterKey)
# --------------------------------------------------------------------------- #

MASTERKEY_PROVIDER = 'КНЕДП "MASTERKEY" ТОВ "АРТ-МАСТЕР"'


def test_shipped_flow_file_configures_the_key_provider(config_dir):
    """The provider is configuration, so the shipped flow.yaml carries it."""
    flow = FlowConfig.from_file(config_dir / "flow.yaml")
    assert flow.authentication.key_provider == MASTERKEY_PROVIDER
    assert flow.authentication.require_key_provider() == MASTERKEY_PROVIDER


def test_key_provider_is_read_from_the_authentication_section():
    flow = FlowConfig.from_dict({"authentication": {"key_provider": MASTERKEY_PROVIDER}})
    assert flow.authentication.require_key_provider() == MASTERKEY_PROVIDER


@pytest.mark.parametrize("raw", [{}, {"authentication": {}},
                                 {"authentication": {"key_provider": "   "}}])
def test_a_missing_or_blank_key_provider_is_a_configuration_error(raw):
    flow = FlowConfig.from_dict(raw)
    with pytest.raises(ConfigError, match="authentication.key_provider is not set"):
        flow.authentication.require_key_provider()


def test_the_shipped_flow_file_uses_the_native_file_dialog(config_dir):
    """Production is the OS dialog: the other mechanisms are known to fail.

    The process name is deliberately not pinned — which browser build is
    installed is a property of the machine, not of the project.
    """
    flow = FlowConfig.from_file(config_dir / "flow.yaml")
    assert flow.authentication.file_selection == "native"
    assert flow.authentication.browser_process.strip()


def test_file_selection_defaults_to_native_when_unset():
    flow = FlowConfig.from_dict({"authentication": {"key_provider": MASTERKEY_PROVIDER}})
    assert flow.authentication.file_selection == "native"


def test_an_unknown_file_selection_mode_is_rejected():
    with pytest.raises(ConfigError, match="file_selection must be one of"):
        FlowConfig.from_dict({"authentication": {"file_selection": "applescript"}})


def test_a_blank_browser_process_is_rejected():
    with pytest.raises(ConfigError, match="browser_process must be"):
        FlowConfig.from_dict({"authentication": {"browser_process": "  "}})


def test_key_provider_must_be_a_string():
    with pytest.raises(ConfigError, match="key_provider must be a string"):
        FlowConfig.from_dict({"authentication": {"key_provider": ["a", "b"]}})


def test_unknown_authentication_option_is_rejected():
    with pytest.raises(ConfigError, match="unknown option"):
        FlowConfig.from_dict({"authentication": {"provider": MASTERKEY_PROVIDER}})


def test_the_key_provider_is_not_taken_from_the_environment(monkeypatch, tmp_path):
    """It is not a secret and not a per-machine path — .env has no say in it."""
    monkeypatch.setenv("HSC_KEY_PROVIDER", "КНЕДП ДПС")
    secrets = load_secrets(env_file=tmp_path / "missing.env")
    assert not any("КНЕДП" in secret for secret in secrets.redactable())
    assert not hasattr(secrets, "key_provider")


# --------------------------------------------------------------------------- #
# Service centres
# --------------------------------------------------------------------------- #


def test_shipped_service_centers_file_loads(config_dir):
    centers = load_service_centers(config_dir / "service_centers.yaml")
    assert centers
    assert all(c.name for c in centers)


def test_placeholder_centres_are_rejected():
    with pytest.raises(SelectorNotConfigured, match="still placeholders"):
        enabled_service_centers([ServiceCenter("TODO_SERVICE_CENTER_1", enabled=True)])


def test_enabled_service_centers_filters_disabled():
    centers = [
        ServiceCenter("ТСЦ 8041", enabled=True),
        ServiceCenter("ТСЦ 8042", enabled=False),
    ]
    assert [c.name for c in enabled_service_centers(centers)] == ["ТСЦ 8041"]


def test_service_centers_file_must_have_the_list_key(tmp_path):
    path = tmp_path / "service_centers.yaml"
    path.write_text("centers: []", encoding="utf-8")
    with pytest.raises(ConfigError, match="expected a `service_centers:` list"):
        load_service_centers(path)




# --------------------------------------------------------------------------- #
# Secrets — the environment, and only the environment
# --------------------------------------------------------------------------- #


def test_the_environment_carries_exactly_six_values():
    """The list is the contract: .env.example, CI and this test agree on it."""
    assert SECRET_ENV_VARS == (
        "IDGOV_SIGNING_KEY_PATH",
        "IDGOV_SIGNING_KEY_PASSWORD",
        "HSC_MONGODB_URI",
        "HSC_SESSION_ENCRYPTION_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_USERS",
    )


def test_env_example_lists_exactly_those_six_keys():
    from hsc_queue_monitor.config import PROJECT_ROOT

    lines = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    keys = [line.split("=", 1)[0] for line in lines if "=" in line and not line.startswith("#")]
    assert keys == list(SECRET_ENV_VARS)
    # Every one of them is empty: the example is a shape, not a leak.
    assert all(line.endswith("=") for line in lines if "=" in line and not line.startswith("#"))


@pytest.mark.parametrize(
    "name",
    [
        "HSC_POLL_INTERVAL_SECONDS",
        "HSC_HEADLESS",
        "HSC_MONGODB_DATABASE",
        "HSC_MONGODB_COLLECTION",
        "HSC_MONITOR_INTERVAL_SECONDS",
        "HSC_READ_TIMEOUT_SECONDS",
        "HSC_SLOT_REQUEST_INTERVAL_SECONDS",
        "TELEGRAM_CHAT_ID",
    ],
)
def test_retired_environment_variables_are_not_read(name, monkeypatch, tmp_path):
    """Each of these used to be an env var. Setting one must now do nothing."""
    monkeypatch.setenv(name, "999")
    secrets = load_secrets(env_file=tmp_path / "missing.env")
    assert "999" not in secrets.redactable()
    assert AppSettings() == AppSettings.from_dict({})


def test_missing_key_path_is_reported_before_login(monkeypatch, tmp_path):
    monkeypatch.setenv("IDGOV_SIGNING_KEY_PATH", str(tmp_path / "nope.dat"))
    secrets = load_secrets(env_file=tmp_path / "missing.env")
    with pytest.raises(ConfigError, match="does not exist"):
        secrets.require_key_path()


def test_an_unset_key_path_says_it_is_local_only(monkeypatch, tmp_path):
    monkeypatch.delenv("IDGOV_SIGNING_KEY_PATH", raising=False)
    secrets = load_secrets(env_file=tmp_path / "missing.env")
    with pytest.raises(ConfigError, match="GitHub Actions never receives it"):
        secrets.require_key_path()


def test_existing_key_path_is_accepted(monkeypatch, tmp_path):
    key = tmp_path / "masterkey.dat"
    key.write_bytes(b"not-a-real-key")
    monkeypatch.setenv("IDGOV_SIGNING_KEY_PATH", str(key))
    secrets = load_secrets(env_file=tmp_path / "missing.env")
    assert secrets.require_key_path() == key


def test_secrets_are_collected_for_redaction(monkeypatch, tmp_path):
    for name in SECRET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("IDGOV_SIGNING_KEY_PASSWORD", "hunter2")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:AAA")
    monkeypatch.setenv("TELEGRAM_USERS", "987654321")
    secrets = load_secrets(env_file=tmp_path / "missing.env")
    # The recipient id is in there too: it identifies a person.
    assert set(secrets.redactable()) == {"hunter2", "123456:AAA", "987654321"}


def test_the_key_path_is_redacted_because_it_names_a_home_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("IDGOV_SIGNING_KEY_PATH", str(tmp_path / "masterkey.dat"))
    secrets = load_secrets(env_file=tmp_path / "missing.env")
    assert str(tmp_path / "masterkey.dat") in secrets.redactable()


def test_repr_names_the_variables_that_are_set_and_never_their_values():
    secrets = SecretSettings(key_password="hunter2", telegram_bot_token="123:AAA")
    text = repr(secrets)
    assert "hunter2" not in text
    assert "123:AAA" not in text
    assert "IDGOV_SIGNING_KEY_PASSWORD" in text and "TELEGRAM_BOT_TOKEN" in text
    assert "HSC_MONGODB_URI" not in text


def test_the_redacted_view_shows_presence_only():
    view = SecretSettings(mongodb_uri="mongodb://user:pw@host").as_redacted_dict()
    assert view["HSC_MONGODB_URI"] == "<redacted>"
    assert view["IDGOV_SIGNING_KEY_PASSWORD"] == ""
    assert not any("pw" in value for value in view.values())


def test_persistence_needs_both_halves():
    assert not SecretSettings(mongodb_uri="mongodb://x").persistence_configured
    both = SecretSettings(mongodb_uri="mongodb://x", session_encryption_key="k")
    assert both.persistence_configured
    assert both.require_persistence() == ("mongodb://x", "k")


def test_a_uri_without_an_encryption_key_explains_why_one_is_needed():
    secrets = SecretSettings(mongodb_uri="mongodb://x")
    with pytest.raises(ConfigError, match="ever written encrypted"):
        secrets.require_persistence()


def test_no_uri_at_all_is_its_own_message():
    with pytest.raises(ConfigError, match="HSC_MONGODB_URI is not set"):
        SecretSettings().require_persistence()


def test_a_bot_with_nobody_to_notify_is_refused():
    with pytest.raises(ConfigError, match="nobody to notify"):
        SecretSettings(telegram_bot_token="123:AAA").require_telegram()


def test_recipients_with_no_bot_are_refused():
    with pytest.raises(ConfigError, match="no bot to send with"):
        SecretSettings(telegram_users=(1,)).require_telegram()


def test_telegram_unconfigured_is_not_an_error():
    """Notifications are optional. Half of them are not."""
    SecretSettings().require_telegram()
    assert not SecretSettings().telegram_configured


def test_derived_paths_live_under_the_data_dir(tmp_path):
    paths = Paths(data_dir=tmp_path)
    assert paths.profile_dir == tmp_path / "browser-profile"
    assert paths.state_path == tmp_path / "state.json"
    assert paths.events_path == tmp_path / "debug" / "events.jsonl"
    assert paths.error_dir == tmp_path / "debug" / "errors"


# --------------------------------------------------------------------------- #
# app.yaml — the non-sensitive settings
# --------------------------------------------------------------------------- #


def app_settings(text: str) -> AppSettings:
    return AppSettings.from_dict(yaml.safe_load(text) or {})


def test_the_shipped_app_file_loads(config_dir):
    settings = AppSettings.from_file(config_dir / "app.yaml")
    assert settings.mongodb.database
    assert settings.mongodb.session_collection
    assert settings.api.monitor_interval_seconds > 0
    assert settings.api.retry.max_attempts >= 1


def test_the_shipped_app_file_keeps_the_measured_values(config_dir):
    """These four were measured against the live site, not chosen."""
    api = AppSettings.from_file(config_dir / "app.yaml").api
    assert api.read_timeout_seconds == 60.0  # /slots timed out at 30s
    assert api.slot_request_interval_seconds >= 2.0  # 429 one second apart
    assert api.monitor_interval_seconds == 300.0  # 900s session lifetime
    assert api.retry.max_retry_after_seconds == 60.0


def test_defaults_apply_when_the_file_is_absent(tmp_path):
    assert AppSettings.from_file(tmp_path / "no-app.yaml") == AppSettings()


def test_an_unknown_section_is_rejected():
    with pytest.raises(ConfigError, match="unknown section"):
        app_settings("notifications:\n  enabled: true\n")


@pytest.mark.parametrize(
    "text",
    [
        "mongodb:\n  uri: mongodb://user:pw@host\n",
        "telegram:\n  token: 123456:AAA\n",
        "telegram:\n  telegram_users: 123\n",
        "api:\n  secret: hunter2\n",
        "browser:\n  key_path: /home/me/key.dat\n",
        "browser_monitor:\n  password: hunter2\n",
        "hsc_mongodb_uri: mongodb://host\n",
    ],
)
def test_a_secret_in_a_committed_file_is_refused_not_ignored(text):
    with pytest.raises(ConfigError, match="never appear in a version-controlled file"):
        app_settings(text)


def test_mongo_names_come_from_the_file():
    settings = app_settings("mongodb:\n  database: other\n  session_collection: docs\n")
    assert settings.mongodb == MongoConfig(database="other", session_collection="docs")


def test_an_empty_mongo_name_is_rejected():
    with pytest.raises(ConfigError, match="non-empty name"):
        app_settings("mongodb:\n  database: '  '\n")


def test_unknown_mongo_option_is_rejected():
    with pytest.raises(ConfigError, match="mongodb: unknown option"):
        app_settings("mongodb:\n  collection: docs\n")


def test_telegram_can_be_switched_off_without_removing_the_secrets():
    assert app_settings("telegram:\n  enabled: false\n").telegram == TelegramConfig(
        enabled=False
    )


def test_telegram_enabled_must_be_a_boolean():
    with pytest.raises(ConfigError, match="must be true or false"):
        app_settings("telegram:\n  enabled: sometimes\n")


def test_api_settings_are_read_from_the_file():
    settings = app_settings(
        "api:\n"
        "  monitor_interval_seconds: 120\n"
        "  connect_timeout_seconds: 3\n"
        "  read_timeout_seconds: 45\n"
        "  slot_request_interval_seconds: 4\n"
    )
    assert settings.api.monitor_interval_seconds == 120.0
    assert settings.api.timeout == (3.0, 45.0)
    assert settings.api.slot_request_interval_seconds == 4.0


def test_the_retry_policy_is_read_from_the_file():
    retry = app_settings(
        "api:\n"
        "  retry:\n"
        "    max_attempts: 2\n"
        "    initial_backoff_seconds: 1\n"
        "    max_backoff_seconds: 8\n"
        "    multiplier: 3\n"
        "    max_retry_after_seconds: 30\n"
    ).api.retry
    assert (retry.max_attempts, retry.multiplier) == (2, 3.0)
    assert (retry.initial_backoff_seconds, retry.max_backoff_seconds) == (1.0, 8.0)
    assert retry.max_retry_after_seconds == 30.0


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("api:\n  retry:\n    max_attempts: 9\n", "between 1 and"),
        ("api:\n  retry:\n    max_attempts: two\n", "whole number"),
        ("api:\n  retry:\n    multiplier: 0.5\n", "the waits would shrink"),
        (
            "api:\n  retry:\n    initial_backoff_seconds: 10\n    max_backoff_seconds: 5\n",
            "below",
        ),
        ("api:\n  retry:\n    backoff: 5\n", "api.retry: unknown option"),
        ("api:\n  read_timeout_seconds: 0\n", "greater than zero"),
        ("api:\n  read_timeout_seconds: 999\n", "the most this client will wait"),
        ("api:\n  monitor_interval_seconds: 99999\n", "the most this monitor will schedule"),
        ("api:\n  slot_request_interval_seconds: -1\n", "cannot be negative"),
        ("api:\n  timeout: 5\n", "api: unknown option"),
    ],
)
def test_an_out_of_range_api_setting_is_rejected(text, match):
    with pytest.raises(ConfigError, match=match):
        app_settings(text)


def test_the_browser_defaults_to_a_visible_window():
    assert AppSettings().headless is False
    assert app_settings("browser:\n  headless: true\n").headless is True


def test_headless_must_be_a_boolean():
    with pytest.raises(ConfigError, match="browser.headless must be true or false"):
        app_settings("browser:\n  headless: yes please\n")


def test_the_browser_monitor_poll_interval_is_clamped_to_the_minimum():
    settings = app_settings("browser_monitor:\n  poll_interval_seconds: 5\n")
    assert settings.browser_monitor.poll_interval_seconds == MIN_POLL_INTERVAL_SECONDS


def test_browser_monitor_values_are_whole_seconds():
    with pytest.raises(ConfigError, match="whole number of seconds"):
        app_settings("browser_monitor:\n  poll_jitter_seconds: -1\n")


def test_browser_monitor_defaults_are_kept():
    assert BrowserMonitorConfig().notify_cooldown_seconds == 6 * 3600


# --------------------------------------------------------------------------- #
# The two sources, together
# --------------------------------------------------------------------------- #


def test_app_config_loads_the_real_repo_configuration(tmp_path, config_dir):
    config = AppConfig.load(config_dir=config_dir, data_dir=tmp_path,
                            env_file=tmp_path / "missing.env")
    assert len(config.selectors) > 0
    assert config.flow.queue_steps
    assert config.service_centers
    assert config.app.mongodb.database
    assert config.paths.data_dir == tmp_path


def test_the_cli_headless_flag_overrides_the_file(tmp_path, config_dir):
    config = AppConfig.load(config_dir=config_dir, data_dir=tmp_path,
                            env_file=tmp_path / "missing.env", headless=True)
    assert config.app.headless is True


def test_flow_yaml_no_longer_carries_operational_api_settings(config_dir):
    """One home per setting. api: moved to app.yaml and must not come back."""
    raw = yaml.safe_load((config_dir / "flow.yaml").read_text(encoding="utf-8"))
    assert "api" not in raw
    assert not hasattr(FlowConfig(), "api")
