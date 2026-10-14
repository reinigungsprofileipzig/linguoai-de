import hashlib
import json
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
from zipfile import ZipFile

from linguoai import __version__

ROOT = Path(__file__).resolve().parents[1]
WEBSITE = ROOT / "website"
if not WEBSITE.is_dir():
    WEBSITE = ROOT
HAS_WEBSITE = (WEBSITE / "index.html").is_file()
SUPPORTED_LOCALES = ("de", "en", "ru", "uk", "tr", "es", "it")
LOCALIZED_PAGE_NAMES = ("index.html", "colab.html", "anleitung.html")
CANONICAL_ORIGIN = "https://linguoai.de"
COLAB_NOTEBOOK_URL = (
    "https://colab.research.google.com/github/reinigungsprofileipzig/linguoai-de/"
    "blob/main/notebooks/LinguoAI_Colab.ipynb"
)


class _WebsiteParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        self.references: list[tuple[str, str, str]] = []
        self.h1_count = 0
        self.html_language = ""
        self.canonical_urls: list[str] = []
        self.alternate_languages: list[tuple[str, str]] = []
        self.external_runtime_resources: list[str] = []
        self._json_ld_depth = 0
        self._json_ld_parts: list[str] = []
        self.json_ld: list[str] = []
        self.section_count = 0
        self.primary_colab_links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "html":
            self.html_language = values.get("lang", "") or ""
        if tag == "h1":
            self.h1_count += 1
        if tag == "section":
            self.section_count += 1
        if tag == "a" and "data-colab-start" in values:
            self.primary_colab_links.append(values.get("href", "") or "")
        if identifier := values.get("id"):
            self.ids.add(identifier)

        attribute = "href" if tag in {"a", "link"} else "src" if tag in {"img", "script"} else ""
        if attribute and (reference := values.get(attribute)):
            self.references.append((tag, attribute, reference))
            is_runtime_resource = tag in {"img", "script"} or (
                tag == "link" and values.get("rel") in {"stylesheet", "icon", "manifest", "preload"}
            )
            if is_runtime_resource and reference.startswith(("http://", "https://")):
                self.external_runtime_resources.append(reference)

        if tag == "link" and (href := values.get("href")):
            relationships = {
                relationship.casefold() for relationship in (values.get("rel") or "").split()
            }
            if "canonical" in relationships:
                self.canonical_urls.append(href)
            if "alternate" in relationships and (language := values.get("hreflang")):
                self.alternate_languages.append((language.casefold(), href))

        if tag == "script" and values.get("type") == "application/ld+json":
            self._json_ld_depth = 1
            self._json_ld_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._json_ld_depth:
            self.json_ld.append("".join(self._json_ld_parts))
            self._json_ld_depth = 0
            self._json_ld_parts = []

    def handle_data(self, data: str) -> None:
        if self._json_ld_depth:
            self._json_ld_parts.append(data)


def _parse(path: Path) -> _WebsiteParser:
    parser = _WebsiteParser()
    parser.feed(path.read_text(encoding="utf-8"))
    return parser


def _localized_page(locale: str, page_name: str) -> Path:
    if locale == "de":
        return WEBSITE / page_name
    return WEBSITE / locale / page_name


def _canonical_url(locale: str, page_name: str) -> str:
    locale_prefix = "" if locale == "de" else f"{locale}/"
    page_suffix = "" if page_name == "index.html" else page_name
    return f"{CANONICAL_ORIGIN}/{locale_prefix}{page_suffix}"


@unittest.skipUnless(HAS_WEBSITE, "Website-Dateien sind in diesem Quellpaket nicht enthalten.")
class WebsiteTests(unittest.TestCase):
    def test_required_static_site_files_exist(self) -> None:
        expected = [
            "index.html",
            "colab.html",
            "anleitung.html",
            "404.html",
            "impressum.html",
            "datenschutz.html",
            "robots.txt",
            "sitemap.xml",
            "site.webmanifest",
            "_headers",
            "assets/styles.css",
            "assets/site.js",
            "assets/locale.js",
            "assets/simple.css",
            "assets/colab-guide.css",
            "assets/favicon.svg",
            "assets/logo-mark.svg",
            "downloads/LinguoAI_Colab.ipynb",
            f"downloads/LinguoAI-{__version__}-source.zip",
            f"downloads/linguoai-{__version__}-py3-none-any.whl",
            f"downloads/SHA256SUMS-{__version__}.txt",
        ]
        expected.extend(f"assets/locales/{locale}.json" for locale in SUPPORTED_LOCALES)
        expected.extend(
            f"{locale}/{page_name}"
            for locale in SUPPORTED_LOCALES
            if locale != "de"
            for page_name in LOCALIZED_PAGE_NAMES
        )
        for relative in expected:
            with self.subTest(relative=relative):
                self.assertTrue((WEBSITE / relative).is_file())

    def test_top_level_html_pages_remain_german(self) -> None:
        for page in WEBSITE.glob("*.html"):
            with self.subTest(page=page.name):
                parser = _parse(page)
                self.assertEqual(parser.html_language, "de")

    def test_every_html_page_has_expected_language_and_one_h1(self) -> None:
        for page in WEBSITE.rglob("*.html"):
            relative = page.relative_to(WEBSITE)
            expected_language = (
                relative.parts[0]
                if len(relative.parts) > 1 and relative.parts[0] in SUPPORTED_LOCALES
                else "de"
            )
            with self.subTest(page=relative.as_posix()):
                parser = _parse(page)
                self.assertEqual(parser.html_language, expected_language)
                self.assertEqual(parser.h1_count, 1)

    def test_localized_pages_have_canonical_and_complete_hreflang_links(self) -> None:
        for locale in SUPPORTED_LOCALES:
            for page_name in LOCALIZED_PAGE_NAMES:
                page = _localized_page(locale, page_name)
                expected_canonical = _canonical_url(locale, page_name)
                with self.subTest(locale=locale, page=page_name):
                    parser = _parse(page)
                    self.assertEqual(parser.html_language, locale)
                    self.assertEqual(parser.h1_count, 1)
                    self.assertEqual(parser.canonical_urls, [expected_canonical])
                    self.assertEqual(len(parser.alternate_languages), 8)

                    alternates: dict[str, str] = {}
                    for language, url in parser.alternate_languages:
                        self.assertNotIn(language, alternates)
                        alternates[language] = url

                    self.assertEqual(
                        set(alternates),
                        {*SUPPORTED_LOCALES, "x-default"},
                    )
                    for alternate_locale in SUPPORTED_LOCALES:
                        self.assertEqual(
                            alternates[alternate_locale],
                            _canonical_url(alternate_locale, page_name),
                        )
                    self.assertEqual(
                        alternates["x-default"],
                        _canonical_url("en", page_name),
                    )

    def test_locale_catalogs_have_the_same_nonempty_keys_and_values(self) -> None:
        base_keys: set[str] | None = None
        for locale in SUPPORTED_LOCALES:
            path = WEBSITE / "assets" / "locales" / f"{locale}.json"
            with self.subTest(locale=locale):
                catalog = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(catalog.get("locale"), locale)
                self.assertIsInstance(catalog.get("name"), str)
                self.assertTrue(catalog["name"].strip())
                translations = catalog.get("translations")
                self.assertIsInstance(translations, dict)
                self.assertTrue(translations)

                invalid_entries = [
                    key
                    for key, value in translations.items()
                    if not isinstance(key, str)
                    or not key.strip()
                    or not isinstance(value, str)
                    or not value.strip()
                ]
                self.assertEqual(invalid_entries, [])

                keys = set(translations)
                if base_keys is None:
                    base_keys = keys
                    continue
                missing = sorted(base_keys - keys)
                unexpected = sorted(keys - base_keys)
                self.assertFalse(
                    missing or unexpected,
                    f"{locale}: missing={missing}, unexpected={unexpected}",
                )

    def test_internal_files_and_home_fragments_resolve(self) -> None:
        parsed_pages = {page.resolve(): _parse(page) for page in WEBSITE.rglob("*.html")}
        for page, parser in parsed_pages.items():
            for tag, attribute, reference in parser.references:
                with self.subTest(
                    page=page.relative_to(WEBSITE).as_posix(),
                    reference=reference,
                ):
                    parts = urlsplit(reference)
                    if parts.scheme and parts.netloc != "linguoai.de":
                        continue
                    if reference.startswith(("mailto:", "tel:")):
                        continue
                    if not parts.path:
                        if parts.fragment:
                            self.assertIn(parts.fragment, parser.ids)
                        continue
                    if parts.netloc == "linguoai.de":
                        target = WEBSITE / unquote(parts.path.lstrip("/") or "index.html")
                    elif parts.path.startswith("/"):
                        target = WEBSITE / unquote(parts.path.lstrip("/"))
                    else:
                        target = page.parent / unquote(parts.path)
                    if parts.path.endswith("/") and target.name != "index.html":
                        target /= "index.html"
                    self.assertTrue(target.resolve().is_file(), f"{tag}[{attribute}] -> {target}")
                    if parts.fragment and target.suffix.casefold() == ".html":
                        target_parser = parsed_pages.get(target.resolve()) or _parse(target)
                        self.assertIn(parts.fragment, target_parser.ids)

    def test_homepage_uses_no_external_runtime_assets(self) -> None:
        parser = _parse(WEBSITE / "index.html")
        self.assertEqual(parser.external_runtime_resources, [])

    def test_homepage_is_compact_and_keeps_technical_setup_in_guide(self) -> None:
        homepage = (WEBSITE / "index.html").read_text(encoding="utf-8")
        parser = _parse(WEBSITE / "index.html")
        self.assertLessEqual(parser.section_count, 6)
        self.assertTrue({"funktionen", "ablauf", "downloads", "fragen"} <= parser.ids)
        self.assertNotIn("pip install", homepage)
        self.assertNotIn("cuDNN", homepage)
        self.assertIn("anleitung.html#desktop", homepage)
        self.assertNotIn("iconify", homepage.casefold())
        self.assertNotIn("fontawesome", homepage.casefold())

    def test_every_start_button_opens_the_same_colab_notebook(self) -> None:
        for locale in SUPPORTED_LOCALES:
            for page_name in LOCALIZED_PAGE_NAMES:
                with self.subTest(locale=locale, page=page_name):
                    parser = _parse(_localized_page(locale, page_name))
                    self.assertTrue(parser.primary_colab_links)
                    self.assertEqual(set(parser.primary_colab_links), {COLAB_NOTEBOOK_URL})

    def test_guide_keeps_advanced_topics_and_colab_constraints(self) -> None:
        guide = (WEBSITE / "anleitung.html").read_text("utf-8")
        parser = _parse(WEBSITE / "anleitung.html")
        self.assertTrue({"desktop", "cli", "stimmen", "hardware", "cloudflare"} <= parser.ids)
        self.assertIn("pip install", guide)
        self.assertIn("CLOUDFLARE_TUNNEL_TOKEN", guide)
        for page_name in ("index.html", "colab.html"):
            source = (WEBSITE / page_name).read_text("utf-8")
            self.assertIn("https://research.google.com/colaboratory/faq.html", source)
            self.assertIn("Compute", source)

    def test_mobile_menu_has_an_inert_state(self) -> None:
        source = (WEBSITE / "assets" / "site.js").read_text("utf-8")
        self.assertIn("inert", source)

    def test_homepage_structured_data_is_valid_json(self) -> None:
        parser = _parse(WEBSITE / "index.html")
        self.assertEqual(len(parser.json_ld), 1)
        data = json.loads(parser.json_ld[0])
        types = {item["@type"] for item in data["@graph"]}
        self.assertEqual(types, {"WebSite", "SoftwareApplication", "FAQPage"})

    def test_legal_placeholders_do_not_leak_into_homepage(self) -> None:
        homepage = (WEBSITE / "index.html").read_text(encoding="utf-8")
        self.assertNotIn('class="placeholder"', homepage)
        self.assertNotIn("BITTE ERGÄNZEN", homepage)

    def test_colab_download_matches_current_notebook(self) -> None:
        source = ROOT / "notebooks" / "LinguoAI_Colab.ipynb"
        download = WEBSITE / "downloads" / "LinguoAI_Colab.ipynb"
        self.assertEqual(download.read_bytes(), source.read_bytes())

    def test_colab_notebook_uses_https_source_zip_by_default(self) -> None:
        notebook = json.loads((ROOT / "notebooks" / "LinguoAI_Colab.ipynb").read_text("utf-8"))
        source = "\n".join(
            line
            for cell in notebook["cells"]
            if cell["cell_type"] == "code"
            for line in cell["source"]
        )
        self.assertIn('PROJECT_SOURCE = "download_zip"', source)
        self.assertIn(
            f'SOURCE_ZIP_URL = "https://linguoai.de/downloads/LinguoAI-{__version__}-source.zip"',
            source,
        )
        self.assertIn("urllib.request.urlopen(request, timeout=120)", source)

    def test_public_pages_use_canonical_project_domain(self) -> None:
        homepage = (WEBSITE / "index.html").read_text(encoding="utf-8")
        colab_page = (WEBSITE / "colab.html").read_text(encoding="utf-8")
        self.assertIn(f"https://linguoai.de/downloads/LinguoAI-{__version__}-source.zip", homepage)
        self.assertIn("colab.html", homepage)
        self.assertIn("https://linguoai.de/downloads/LinguoAI_Colab.ipynb", colab_page)
        self.assertIn(COLAB_NOTEBOOK_URL, colab_page)
        combined = f"{homepage}\n{colab_page}".casefold()
        self.assertNotIn("http://linguoai.de", combined)

    def test_download_archives_are_installable_project_artifacts(self) -> None:
        with ZipFile(WEBSITE / "downloads" / f"LinguoAI-{__version__}-source.zip") as source_zip:
            source_names = set(source_zip.namelist())
            self.assertIn("pyproject.toml", source_names)
            self.assertIn("linguoai/__init__.py", source_names)
            self.assertEqual(
                source_zip.read("notebooks/LinguoAI_Colab.ipynb"),
                (ROOT / "notebooks" / "LinguoAI_Colab.ipynb").read_bytes(),
            )
            self.assertFalse(
                any("__pycache__" in name or name.endswith(".pyc") for name in source_names)
            )

        with ZipFile(WEBSITE / "downloads" / f"linguoai-{__version__}-py3-none-any.whl") as wheel:
            wheel_names = set(wheel.namelist())
            self.assertIn("linguoai/__init__.py", wheel_names)
            self.assertTrue(any(name.endswith(".dist-info/METADATA") for name in wheel_names))
            for path in (ROOT / "linguoai").rglob("*.py"):
                name = path.relative_to(ROOT).as_posix()
                self.assertEqual(wheel.read(name), path.read_bytes(), name)

    def test_download_checksums_match_artifacts(self) -> None:
        downloads = WEBSITE / "downloads"
        manifest = (downloads / f"SHA256SUMS-{__version__}.txt").read_text("utf-8")
        self.assertEqual(len(manifest.splitlines()), 3)
        for line in manifest.splitlines():
            checksum, name = line.split("  ", 1)
            self.assertEqual(hashlib.sha256((downloads / name).read_bytes()).hexdigest(), checksum)

    def test_notebook_has_one_cell_and_matches_bootstrap(self) -> None:
        notebook = json.loads((ROOT / "notebooks" / "LinguoAI_Colab.ipynb").read_text("utf-8"))
        cells = [cell for cell in notebook["cells"] if cell["cell_type"] == "code"]
        self.assertEqual(len(cells), 1)
        source = "".join(cells[0]["source"])
        self.assertEqual(source, (ROOT / "scripts" / "colab_bootstrap.py").read_text("utf-8"))
        self.assertIn('SHARE_PROVIDER = "gradio"', source)
        self.assertIn('CLOUDFLARE_TUNNEL_HOSTNAME = ""', source)


if __name__ == "__main__":
    unittest.main()
