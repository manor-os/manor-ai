from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
I18N_DIR = ROOT / "apps" / "web" / "src" / "lib" / "i18n"
LOCALES = ("en", "zh", "de", "es", "fr")
ASSISTANT_PROCESS_PREFIXES = (
    "component.assistant_message_blocks.",
    "component.assistant_process.",
)
VOICE_PREFIXES = ("chat.call.", "chat.voice.")


def _locale_keys(locale: str) -> set[str]:
    source = (I18N_DIR / f"{locale}.ts").read_text(encoding="utf-8")
    return {match.group(1) for match in re.finditer(r'"([^"]+)"\s*:', source)}


def test_assistant_process_i18n_keys_exist_in_all_locales() -> None:
    keys_by_locale = {locale: _locale_keys(locale) for locale in LOCALES}
    assistant_keys = sorted(
        key for keys in keys_by_locale.values() for key in keys if key.startswith(ASSISTANT_PROCESS_PREFIXES)
    )

    assert assistant_keys
    expected = set(assistant_keys)
    missing_by_locale = {locale: sorted(expected - keys) for locale, keys in keys_by_locale.items() if expected - keys}

    assert missing_by_locale == {}


def test_voice_i18n_keys_exist_in_all_locales() -> None:
    keys_by_locale = {locale: _locale_keys(locale) for locale in LOCALES}
    expected = {key for key in keys_by_locale["en"] if key.startswith(VOICE_PREFIXES)}
    missing_by_locale = {
        locale: sorted(expected - keys)
        for locale, keys in keys_by_locale.items()
        if expected - keys
    }

    assert expected
    assert missing_by_locale == {}


def test_french_locale_is_registered_globally() -> None:
    source = (I18N_DIR.parent / "i18n.ts").read_text(encoding="utf-8")
    api_source = (ROOT / "apps" / "web" / "src" / "lib" / "api.ts").read_text(encoding="utf-8")
    layout_source = (ROOT / "apps" / "web" / "src" / "layouts" / "AppLayout.tsx").read_text(encoding="utf-8")
    timestamp_source = (ROOT / "apps" / "web" / "src" / "components" / "chat" / "ChatTimestamp.tsx").read_text(encoding="utf-8")
    format_source = (ROOT / "apps" / "web" / "src" / "lib" / "format.ts").read_text(encoding="utf-8")

    assert 'import fr from "./i18n/fr";' in source
    assert '{ code: "fr", name: "Fran\\u00E7ais"' in source
    assert "normalizeLocale(localStorage.getItem" in api_source
    assert 'fr: "fr-FR"' in timestamp_source
    assert 'fr: "fr-FR"' in format_source
    assert 'fr: "fr"' in layout_source
    assert '<span>{t("nav.support")}</span>' in layout_source


def test_french_core_ui_has_native_translations() -> None:
    french_keys = _locale_keys("fr")
    required = {
        "nav.chat",
        "nav.tasks",
        "nav.knowledge",
        "nav.workspaces",
        "nav.settings",
        "settings.language",
        "page.login.sign_in",
        "page.tasks.new_task",
        "page.workspaces.create_workspace",
        "component.cookie_consent.title",
    }

    assert required <= french_keys
