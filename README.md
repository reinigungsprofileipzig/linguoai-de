# LinguoAI-Website

Statische Produktwebsite für `https://linguoai.de/`.

Öffentliches Repository: `https://github.com/reinigungsprofileipzig/linguoai-de`. Dort liegen
Website, Colab-Notebook und Downloads. Das Notebook lädt das geprüfte Quellcode-ZIP beim Start
automatisch über `https://linguoai.de/`.

## Struktur

- `index.html`: vollständige Produkt- und Nutzungsseite
- `assets/styles.css` und `assets/funnel.css`: responsives Design, Funnel und Animationen
- `assets/site.js`: Navigation, Tabs, Copy-Buttons und Scroll-Reveals
- `downloads/`: direkt angebotene Projektdateien
- `impressum.html` und `datenschutz.html`: noch auszufüllende Entwürfe
- `404.html`, `robots.txt`, `sitemap.xml`, `site.webmanifest`: Veröffentlichungsdateien
- `_headers`: optionale Sicherheits-/Cache-Header für Cloudflare Pages und kompatible Hoster
- `DEPLOYMENT.md`: Vorschau- und Domainanleitung

Es werden keine externen Fonts, JavaScript-Bibliotheken, Analysewerkzeuge oder Cookies geladen.

## Vorschau

```powershell
python -m http.server 8080 --directory website
```

Dann `http://localhost:8080/` öffnen.
