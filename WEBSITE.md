# LinguoAI-Website

Statische Produktwebsite für `https://linguoai.de/`.

Öffentliche Projektadresse: `https://linguoai.de/`. Website, Colab-Notebook und Downloads werden
dort direkt über die eigene Domain angeboten. Das Notebook lädt das geprüfte Quellcode-ZIP beim
Start ebenfalls über `https://linguoai.de/`.

## Struktur

- `index.html`: vollständige Produkt- und Nutzungsseite
- `colab.html`: Ein-Zellen-Start mit dynamischem Gradio-Link und optionalem Cloudflare Tunnel
- `assets/styles.css` und `assets/funnel.css`: responsives Design, Funnel und Animationen
- `assets/site.js`: Navigation, Tabs, Copy-Buttons und Scroll-Reveals
- `downloads/`: direkt angebotene Projektdateien
- `downloads/SHA256SUMS-2.2.1.txt`: Prüfsummen der aktuellen Release-Dateien
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

Im Hosting-Repository liegen diese Dateien direkt im Root; dort `--directory .` verwenden.
Release-Erstellung und GitHub-Pages-Veröffentlichung sind in `DEPLOYMENT.md` beschrieben.
