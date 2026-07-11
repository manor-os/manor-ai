from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
I18N_DIR = ROOT / "apps" / "web" / "src" / "lib" / "i18n"
LOCALES = ("en", "zh", "de", "es")
ASSISTANT_PROCESS_PREFIXES = (
    "component.assistant_message_blocks.",
    "component.assistant_process.",
)


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
