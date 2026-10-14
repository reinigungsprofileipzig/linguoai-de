"""Build static LinguoAI website variants from the German source pages.

The German homepage, Colab quick start, and detailed guide remain the
authoritative templates. Locale catalogs map their visible German strings to
translations. The generated pages are completely static and therefore remain
usable without JavaScript.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup, Comment, Doctype, NavigableString, Tag

ROOT = Path(__file__).resolve().parents[1]
WEBSITE = ROOT / "website" if (ROOT / "website" / "index.html").is_file() else ROOT
CATALOG_DIR = WEBSITE / "assets" / "locales"
SOURCE_PAGES = {
    "index": WEBSITE / "index.html",
    "colab": WEBSITE / "colab.html",
    "anleitung": WEBSITE / "anleitung.html",
}

LOCALES = {
    "de": {"name": "Deutsch", "og": "de_DE"},
    "en": {"name": "English", "og": "en_US"},
    "ru": {"name": "Русский", "og": "ru_RU"},
    "uk": {"name": "Українська", "og": "uk_UA"},
    "tr": {"name": "Türkçe", "og": "tr_TR"},
    "es": {"name": "Español", "og": "es_ES"},
    "it": {"name": "Italiano", "og": "it_IT"},
}

SKIP_TAGS = {"script", "style", "code", "pre", "kbd", "samp", "svg"}
TRANSLATED_ATTRIBUTES = ("aria-label", "alt", "placeholder", "title")
JSON_LD_TRANSLATED_KEYS = {
    "description",
    "featureList",
    "name",
    "softwareRequirements",
    "text",
}
TOKEN_PATTERN = re.compile(r"__LINGUOAI_(\d{4})__")
PROTECTED_TERMS = (
    "LinguoAI",
    "Faster-Whisper",
    "Google Translate",
    "Google Colab",
    "Google Drive",
    "Colab",
    "Edge TTS",
    "Edge-TTS",
    "Edge",
    "TTS",
    "gTTS",
    "Chatterbox",
    "Piper",
    "NLLB-200",
    "NLLB",
    "Whisper",
    "DeepL",
    "Gemini",
    "Gradio",
    "MyDrive",
    "FFmpeg",
    "ffprobe",
    "PyTorch",
    "CUDA",
    "ONNX",
    "FLORES-200",
    "BCP-47",
    "PowerShell",
    "Python",
    "Hugging-Face",
    "Hugging Face",
    "NVIDIA",
    "PerTh",
    "JavaScript",
    "HTTPS",
    "HTTP",
    "HTML",
    "CSS",
    "URL",
    "Git",
    "T4",
    "Windows",
    "Linux",
    "Notebook",
    "Wheel",
    "SRT",
    "CLI",
    "GUI",
    "CPU",
    "GPU",
    "MP4",
    "MKV",
    "MOV",
    "AVI",
    "WEBM",
    "M4V",
    "JSON",
    "API",
)
MANUAL_OVERRIDES = {
    "en": {
        "Ausgabe": "Output",
        "Ausgaben": "Outputs",
        "Dein erster Auftrag in der Oberfläche": "Your first job in the interface",
        "Ein kompletter lokaler Auftrag": "A complete local job",
        "Freundlich eingerichtet": "Easy setup",
        "LinguoAI – Videos übersetzen und synchron neu vertonen": (
            "LinguoAI – Translate and dub videos in sync"
        ),
        "Neuer Auftrag": "New job",
        "Natürlich vertont.": "Naturally dubbed.",
        "Oberfläche öffnen": "Open interface",
        "Stimmen": "Voices",
        "Stimmen vergleichen": "Compare voices",
        "Vertonen": "Dub",
    },
    "ru": {
        "Ausgabe": "Вывод",
        "Ausgaben": "Результаты",
        "Dein erster Auftrag in der Oberfläche": "Ваше первое задание в интерфейсе",
        "Die": "Сначала откройте",
        "Ein kompletter lokaler Auftrag": "Полное локальное задание",
        "Freundlich eingerichtet": "Простая настройка",
        "LinguoAI – Videos übersetzen und synchron neu vertonen": (
            "LinguoAI – переводите и синхронно озвучивайте видео"
        ),
        "Neuer Auftrag": "Новое задание",
        "Natürlich vertont.": "Естественная озвучка.",
        "Oberfläche öffnen": "Открыть интерфейс",
        "Stimmen": "Голоса",
        "Stimmen vergleichen": "Сравнить голоса",
        "Vertonen": "Озвучить",
        "öffnen, das Notebook von linguoai.de laden und bei Google Colab hochladen.": (
            "затем скачайте Notebook с linguoai.de и загрузите его в Google Colab."
        ),
    },
    "uk": {
        "Ausgabe": "Виведення",
        "Ausgaben": "Результати",
        "Dein erster Auftrag in der Oberfläche": "Ваше перше завдання в інтерфейсі",
        "Ein kompletter lokaler Auftrag": "Повне локальне завдання",
        "Freundlich eingerichtet": "Просте налаштування",
        "LinguoAI – Videos übersetzen und synchron neu vertonen": (
            "LinguoAI – перекладайте та синхронно озвучуйте відео"
        ),
        "Neuer Auftrag": "Нове завдання",
        "Natürlich vertont.": "Природне озвучення.",
        "Oberfläche öffnen": "Відкрити інтерфейс",
        "Stimmen": "Голоси",
        "Stimmen vergleichen": "Порівняти голоси",
        "Vertonen": "Озвучити",
    },
    "tr": {
        "Ausgabe": "Çıktı",
        "Ausgaben": "Çıktılar",
        "Dein erster Auftrag in der Oberfläche": "Arayüzdeki ilk işleminiz",
        "Die": "İlk olarak",
        "Ein kompletter lokaler Auftrag": "Eksiksiz bir yerel işlem",
        "Freundlich eingerichtet": "Kolay kurulum",
        "LinguoAI – Videos übersetzen und synchron neu vertonen": (
            "LinguoAI – Videoları çevirin ve senkronize şekilde seslendirin"
        ),
        "Neuer Auftrag": "Yeni işlem",
        "Natürlich vertont.": "Doğal seslendirme.",
        "Oberfläche öffnen": "Arayüzü aç",
        "Stimmen": "Sesler",
        "Stimmen vergleichen": "Sesleri karşılaştır",
        "Vertonen": "Seslendir",
        "öffnen, das Notebook von linguoai.de laden und bei Google Colab hochladen.": (
            "bölümünü açın, Notebook dosyasını linguoai.de adresinden indirin ve "
            "Google Colab'a yükleyin."
        ),
    },
    "es": {
        "Ausgabe": "Salida",
        "Ausgaben": "Resultados",
        "Dein erster Auftrag in der Oberfläche": "Tu primera tarea en la interfaz",
        "Ein kompletter lokaler Auftrag": "Una tarea local completa",
        "Freundlich eingerichtet": "Configuración sencilla",
        "LinguoAI – Videos übersetzen und synchron neu vertonen": (
            "LinguoAI: traduce y dobla vídeos de forma sincronizada"
        ),
        "Neuer Auftrag": "Nueva tarea",
        "Natürlich vertont.": "Doblaje natural.",
        "Oberfläche öffnen": "Abrir interfaz",
        "Stimmen": "Voces",
        "Stimmen vergleichen": "Comparar voces",
        "Vertonen": "Doblar",
    },
    "it": {
        "Ausgabe": "Output",
        "Ausgaben": "Risultati",
        "Dein erster Auftrag in der Oberfläche": "La tua prima attività nell'interfaccia",
        "Ein kompletter lokaler Auftrag": "Un'attività locale completa",
        "Freundlich eingerichtet": "Configurazione semplice",
        "LinguoAI – Videos übersetzen und synchron neu vertonen": (
            "LinguoAI – Traduci e doppia i video in modo sincronizzato"
        ),
        "Neuer Auftrag": "Nuova attività",
        "Natürlich vertont.": "Doppiaggio naturale.",
        "Oberfläche öffnen": "Apri interfaccia",
        "Stimmen": "Voci",
        "Stimmen vergleichen": "Confronta le voci",
        "Vertonen": "Doppia",
    },
}


def normalize_text(value: str) -> str:
    return " ".join(value.split())


def should_translate(value: str) -> bool:
    return bool(value and any(character.isalpha() for character in value))


def is_ignored(node: NavigableString | Tag) -> bool:
    if isinstance(node, (Comment, Doctype)):
        return True
    parent = node.parent if isinstance(node, NavigableString) else node
    while isinstance(parent, Tag):
        if parent.name in SKIP_TAGS or parent.has_attr("data-i18n-ignore"):
            return True
        parent = parent.parent
    return False


def iter_json_ld_strings(value: Any, *, parent_key: str = "") -> Iterable[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in JSON_LD_TRANSLATED_KEYS or isinstance(item, (dict, list)):
                yield from iter_json_ld_strings(item, parent_key=key)
    elif isinstance(value, list):
        for item in value:
            yield from iter_json_ld_strings(item, parent_key=parent_key)
    elif isinstance(value, str) and parent_key in JSON_LD_TRANSLATED_KEYS:
        normalized = normalize_text(value)
        if should_translate(normalized):
            yield normalized


def extract_strings(path: Path) -> set[str]:
    soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
    strings: set[str] = set()

    for node in soup.find_all(string=True):
        if is_ignored(node):
            continue
        normalized = normalize_text(str(node))
        if should_translate(normalized):
            strings.add(normalized)

    for element in soup.find_all(True):
        if is_ignored(element):
            continue
        for attribute in TRANSLATED_ATTRIBUTES:
            normalized = normalize_text(str(element.get(attribute, "")))
            if should_translate(normalized):
                strings.add(normalized)

    for meta in soup.select(
        'meta[name="description"], meta[property="og:title"], meta[property="og:description"]'
    ):
        normalized = normalize_text(str(meta.get("content", "")))
        if should_translate(normalized):
            strings.add(normalized)

    for script in soup.select('script[type="application/ld+json"]'):
        if not script.string:
            continue
        data = json.loads(script.string)
        strings.update(iter_json_ld_strings(data))

    return strings


def source_strings() -> list[str]:
    strings: set[str] = set()
    for path in SOURCE_PAGES.values():
        strings.update(extract_strings(path))
    return sorted(strings, key=lambda value: (value.casefold(), value))


def load_catalog(locale: str) -> dict[str, str]:
    path = CATALOG_DIR / f"{locale}.json"
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    translations = data.get("translations", {})
    if not isinstance(translations, dict):
        raise ValueError(f"Ungültiger Katalog: {path}")
    return {str(key): str(value) for key, value in translations.items()}


def save_catalog(locale: str, translations: dict[str, str]) -> None:
    CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    translations.update(
        (source, target)
        for source, target in MANUAL_OVERRIDES.get(locale, {}).items()
        if source in translations
    )
    payload = {
        "locale": locale,
        "name": LOCALES[locale]["name"],
        "translations": dict(sorted(translations.items(), key=lambda item: item[0].casefold())),
    }
    (CATALOG_DIR / f"{locale}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def chunk_strings(values: list[str], *, maximum: int = 4000) -> list[list[str]]:
    chunks: list[list[str]] = []
    current: list[str] = []
    length = 0
    for value in values:
        addition = len(value) + 32
        if current and length + addition > maximum:
            chunks.append(current)
            current = []
            length = 0
        current.append(value)
        length += addition
    if current:
        chunks.append(current)
    return chunks


def translate_chunk(values: list[str], locale: str) -> list[str]:
    from deep_translator import GoogleTranslator

    term_map = {f"ZXQ{index:03d}QXZ": term for index, term in enumerate(PROTECTED_TERMS)}

    def protect(value: str) -> str:
        protected = value
        for token, term in term_map.items():
            protected = protected.replace(term, token)
        return protected

    def restore(value: str) -> str:
        restored = value
        for token, term in term_map.items():
            restored = re.sub(
                re.escape(token), lambda _match, term=term: term, restored, flags=re.IGNORECASE
            )
        return restored

    protected_values = [protect(value) for value in values]
    payload = "\n".join(
        f"__LINGUOAI_{index:04d}__\n{value}" for index, value in enumerate(protected_values)
    )
    translated = str(GoogleTranslator(source="de", target=locale).translate(payload))
    matches = list(TOKEN_PATTERN.finditer(translated))
    if len(matches) != len(values):
        translator = GoogleTranslator(source="de", target=locale)
        return [restore(str(translator.translate(value))) for value in protected_values]

    results: list[str] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(translated)
        results.append(restore(normalize_text(translated[start:end])))
    return results


def validate_protected_terms(source: str, translated: str, locale: str) -> None:
    if re.search(r"ZXQ\d{3}QXZ", translated, flags=re.IGNORECASE):
        raise ValueError(f"{locale}: nicht ersetzter Schutzmarker in {source!r}")
    missing = [term for term in PROTECTED_TERMS if term in source and term not in translated]
    if missing:
        raise ValueError(f"{locale}: geschützte Begriffe fehlen in {source!r}: {missing}")


def update_catalogs(locales: Iterable[str], *, refresh: bool = False) -> None:
    sources = source_strings()
    save_catalog("de", {source: source for source in sources})

    for locale in locales:
        if locale == "de":
            continue
        translations = {} if refresh else load_catalog(locale)
        missing = [source for source in sources if not translations.get(source, "").strip()]
        obsolete = set(translations) - set(sources)
        for key in obsolete:
            translations.pop(key, None)
        if not missing:
            save_catalog(locale, translations)
            continue

        chunks = chunk_strings(missing)
        print(f"{locale}: {len(missing)} neue Texte in {len(chunks)} Blöcken")
        for number, chunk in enumerate(chunks, start=1):
            translated = translate_chunk(chunk, locale)
            if len(translated) != len(chunk):
                raise RuntimeError(f"{locale}: Übersetzungsblock {number} ist unvollständig.")
            for source, target in zip(chunk, translated, strict=True):
                validate_protected_terms(source, target, locale)
            translations.update(zip(chunk, translated, strict=True))
            save_catalog(locale, translations)
            print(f"  Block {number}/{len(chunks)} gespeichert")


def translated_value(value: str, catalog: dict[str, str]) -> str:
    normalized = normalize_text(value)
    if not should_translate(normalized):
        return value
    translated = catalog.get(normalized)
    if not translated:
        raise KeyError(f"Fehlende Übersetzung: {normalized!r}")
    return translated


def translate_json_ld(value: Any, catalog: dict[str, str], *, parent_key: str = "") -> Any:
    if isinstance(value, dict):
        translated: dict[str, Any] = {}
        for key, item in value.items():
            if key == "inLanguage":
                translated[key] = catalog["__locale__"]
            elif key in JSON_LD_TRANSLATED_KEYS or isinstance(item, (dict, list)):
                translated[key] = translate_json_ld(item, catalog, parent_key=key)
            else:
                translated[key] = item
        return translated
    if isinstance(value, list):
        return [translate_json_ld(item, catalog, parent_key=parent_key) for item in value]
    if isinstance(value, str) and parent_key in JSON_LD_TRANSLATED_KEYS:
        return translated_value(value, catalog)
    return value


def page_url(locale: str, page: str) -> str:
    return f"https://linguoai.de{route_path(locale, page)}"


def route_path(locale: str, page: str) -> str:
    if page not in SOURCE_PAGES:
        raise ValueError(f"Unbekannte Seite: {page}")
    prefix = "" if locale == "de" else f"{locale}/"
    suffix = "" if page == "index" else f"{page}.html"
    return f"/{prefix}{suffix}"


def localize_site_url(value: str, locale: str) -> str:
    """Localize source-page URLs, preserving external URLs and local anchors."""
    parts = urlsplit(value)
    if parts.scheme and parts.scheme not in {"http", "https"}:
        return value
    if parts.netloc and parts.netloc != "linguoai.de":
        return value
    if not parts.path:
        return value
    source_routes = {"/": "index", "/index.html": "index", "index.html": "index"}
    for page in SOURCE_PAGES:
        if page != "index":
            source_routes[f"/{page}.html"] = page
            source_routes[f"{page}.html"] = page
    target_page = source_routes.get(parts.path)
    if target_page is None:
        return value
    return urlunsplit(
        (parts.scheme, parts.netloc, route_path(locale, target_page), parts.query, parts.fragment)
    )


def localize_json_ld_urls(value: Any, locale: str) -> Any:
    if isinstance(value, dict):
        return {
            key: localize_site_url(item, locale)
            if key in {"url", "@id"} and isinstance(item, str)
            else localize_json_ld_urls(item, locale)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [localize_json_ld_urls(item, locale) for item in value]
    return value


def localize_links(soup: BeautifulSoup, locale: str, page: str) -> None:
    for link in soup.find_all("a", href=True):
        link["href"] = localize_site_url(str(link["href"]), locale)

    for link in soup.select("[data-locale-link]"):
        target = str(link.get("data-locale-link", "de"))
        link["href"] = route_path(target, page)
        if target == locale:
            link["aria-current"] = "page"
        else:
            link.attrs.pop("aria-current", None)


def build_page(locale: str, page: str, catalog: dict[str, str]) -> str:
    source = SOURCE_PAGES[page]
    soup = BeautifulSoup(source.read_text(encoding="utf-8"), "html.parser")
    html = soup.html
    if html is None:
        raise ValueError(f"Kein html-Element in {source}")
    html["lang"] = locale
    html["data-locale-page"] = locale
    html["data-locale-detect"] = "false"

    for node in list(soup.find_all(string=True)):
        if is_ignored(node):
            continue
        original = str(node)
        normalized = normalize_text(original)
        if not should_translate(normalized):
            continue
        translated = translated_value(normalized, catalog)
        leading = original[: len(original) - len(original.lstrip())]
        trailing = original[len(original.rstrip()) :]
        node.replace_with(f"{leading}{translated}{trailing}")

    for element in soup.find_all(True):
        if is_ignored(element):
            continue
        for attribute in TRANSLATED_ATTRIBUTES:
            value = str(element.get(attribute, ""))
            normalized = normalize_text(value)
            if should_translate(normalized):
                element[attribute] = translated_value(normalized, catalog)

    for meta in soup.select(
        'meta[name="description"], meta[property="og:title"], meta[property="og:description"]'
    ):
        meta["content"] = translated_value(str(meta.get("content", "")), catalog)

    canonical = page_url(locale, page)
    canonical_node = soup.select_one('link[rel="canonical"]')
    if canonical_node:
        canonical_node["href"] = canonical
    og_url = soup.select_one('meta[property="og:url"]')
    if og_url:
        og_url["content"] = canonical
    og_locale = soup.select_one('meta[property="og:locale"]')
    if og_locale:
        og_locale["content"] = LOCALES[locale]["og"]
    for alternate in soup.select('link[rel="alternate"][hreflang]'):
        target = str(alternate["hreflang"])
        if target == "x-default":
            target = "en"
        if target in LOCALES:
            alternate["href"] = page_url(target, page)

    json_catalog = {**catalog, "__locale__": locale}
    for script in soup.select('script[type="application/ld+json"]'):
        if not script.string:
            continue
        data = json.loads(script.string)
        localized = localize_json_ld_urls(translate_json_ld(data, json_catalog), locale)
        script.string = "\n" + json.dumps(localized, ensure_ascii=False, indent=2) + "\n"

    localize_links(soup, locale, page)
    locale_code = soup.select_one("[data-locale-code]")
    if locale_code:
        locale_code.string = locale.upper()

    for node in list(soup.contents):
        if isinstance(node, Doctype):
            node.extract()
    return "<!doctype html>\n" + str(soup).lstrip()


def validate_catalogs() -> None:
    sources = set(source_strings())
    for locale in LOCALES:
        catalog = load_catalog(locale)
        missing = sources - set(catalog)
        extra = set(catalog) - sources
        empty = {key for key, value in catalog.items() if not value.strip()}
        if missing or extra or empty:
            raise ValueError(
                f"{locale}: missing={len(missing)}, extra={len(extra)}, empty={len(empty)}"
            )
        for source, translated in catalog.items():
            validate_protected_terms(source, translated, locale)


def build_localized_pages() -> None:
    validate_catalogs()
    for locale in LOCALES:
        if locale == "de":
            continue
        catalog = load_catalog(locale)
        target_dir = WEBSITE / locale
        target_dir.mkdir(parents=True, exist_ok=True)
        for page in SOURCE_PAGES:
            output_name = f"{page}.html"
            (target_dir / output_name).write_text(
                build_page(locale, page, catalog),
                encoding="utf-8",
            )
        print(f"{locale}: statische Seiten erstellt")


def build_sitemap() -> None:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
            'xmlns:xhtml="http://www.w3.org/1999/xhtml">'
        ),
    ]
    for page in SOURCE_PAGES:
        for locale in LOCALES:
            lines.append("  <url>")
            lines.append(f"    <loc>{page_url(locale, page)}</loc>")
            for alternate in LOCALES:
                lines.append(
                    '    <xhtml:link rel="alternate" '
                    f'hreflang="{alternate}" href="{page_url(alternate, page)}" />'
                )
            lines.append(
                '    <xhtml:link rel="alternate" hreflang="x-default" '
                f'href="{page_url("en", page)}" />'
            )
            lines.append("  </url>")
    lines.append("</urlset>")
    (WEBSITE / "sitemap.xml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--translate-missing",
        action="store_true",
        help="Fehlende Katalogeinträge über Google Translate erzeugen.",
    )
    parser.add_argument(
        "--locales",
        nargs="*",
        choices=sorted(LOCALES),
        default=sorted(LOCALES),
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Vorhandene maschinelle Übersetzungen vollständig neu erzeugen.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.translate_missing:
        update_catalogs(args.locales, refresh=args.refresh)
    else:
        sources = source_strings()
        save_catalog("de", {source: source for source in sources})
    build_localized_pages()
    build_sitemap()


if __name__ == "__main__":
    main()
