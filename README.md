# LinguoAI-Website

Statische Produktwebsite für `https://linguoai.de/`.

## Struktur

- `index.html`: vollständige Produkt- und Nutzungsseite
- `assets/styles.css`: responsives Design und Animationen
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

