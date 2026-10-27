# LinguoAI-Website

Statische Produktwebsite für `https://linguoai.de/`.

Öffentliche Projektadresse: `https://linguoai.de/`. Website, Colab-Notebook und Downloads werden
dort direkt über die eigene Domain angeboten. Das Notebook lädt das geprüfte Quellcode-ZIP beim
Start ebenfalls über `https://linguoai.de/`.

## Struktur

- `index.html`: Einstieg mit Versionsvergleich, Nutzen, Ablauf, Downloads und häufigen Fragen
- `colab.html`: direkter Notebook-Start und kurze Drei-Schritt-Anleitung
- `anleitung.html`: lokale Installation, Stimmen, CLI, Hardware und optionaler Cloudflare Tunnel
- `assets/simple.css`: gemeinsames responsives Design für Startseite und Anleitungen
- `assets/editorial.css`: gemeinsame Farb- und Typografietokens, Sprachbeispiel, Versionsvergleich und öffentliche Layouts
- `assets/editorial-guides.css`: Leselayouts, Einrichtungsschritte und Codebeispiele für beide Anleitungen
- `assets/fonts/`: lokal gehostete variable Manrope-Schrift mit SIL-OFL-Lizenz
- `assets/guide.css` und `assets/colab-guide.css`: ergänzende Anleitungs-Layouts
- `assets/styles.css`: bestehendes Layout der Rechtstexte und Fehlerseite
- `assets/site.js`: Versionsauswahl, Textbeispiel, gespeichertes Farbschema, Mobilnavigation, Copy-Buttons und FAQ
- `web.html`: Video Studio mit Anmeldung, Upload, Auftragsstatus und Ergebnisdownloads
- `assets/web-app.css` und `assets/web-app.js`: responsives Studio und `chunks-v1`-Uploads
- `REMOTE_WORKER.md`: Betriebsübersicht für Upload, Remote-Verarbeitung und private Drive-Ablage
- `downloads/`: direkt angebotene Projektdateien
- `downloads/SHA256SUMS-2.2.1.txt`: Prüfsummen der aktuellen Release-Dateien
- `impressum.html` und `datenschutz.html`: noch auszufüllende Entwürfe
- `404.html`, `robots.txt`, `sitemap.xml`, `site.webmanifest`: Veröffentlichungsdateien
- `_headers`: optionale Sicherheits-/Cache-Header für Cloudflare Pages und kompatible Hoster
- `DEPLOYMENT.md`: Vorschau- und Domainanleitung

Die statischen Produktseiten laden keine externen Fonts, JavaScript-Bibliotheken,
Analysewerkzeuge oder Cookies. Das separate Video Studio benötigt ein geschütztes
Sitzungscookie für Login und eigene Aufträge.

## Video Studio

Die aktuelle Dienstadresse ist
[Webversion öffnen](https://linguoai.de/web.html).
Die Oberfläche lädt Videos in maximal 8 MiB großen Blöcken mit SHA-256 und
bestätigten Offsets hoch. Vorübergehende Verbindungsfehler werden begrenzt
wiederholt; der Upload kann in der geöffneten Seite am bestätigten Stand fortsetzen.
Abbruch beendet den Upload und den zugehörigen Auftrag. Verarbeitung und Downloads
bleiben über die eigene Auftragsliste erreichbar; ein fertiger Auftrag zeigt
Verfügbarkeitszeit und Ende der Downloadfrist.

Das Studio benötigt die API und einen Job-Worker. Die statische Vorschau unten
stellt diese Dienste nicht bereit. Die API bestimmt verfügbare ASR-, Übersetzungs-
und Sprachausgabeoptionen aus den tatsächlichen Worker-Fähigkeiten. Die
Übersetzungszugänge werden später eingerichtet; ein echtes vollständig übersetztes
Server-Ergebnis ist noch nicht bestätigt. Seit dem 15. Oktober 2026 nutzt die API
den privaten Google-Drive-Ordner „linguoai uploads“ mit echter Betreiber-OAuth-Freigabe.
Der serverseitige Upload-, Download- und Löschtest sowie ein isolierter HTTP-Rundlauf
mit synthetischen Dateien und geschützten Downloads waren erfolgreich. Vorhandene
lokale Downloads bleiben über den Speicherrouter erreichbar; für Verarbeitung und
Transfers werden weiterhin begrenzte lokale Arbeits- und Zwischenspeicher benötigt.

`studio.linguoai.de` ist als neue Adresse vorbereitet. Die Aktivierung bleibt bis
zur öffentlichen Prüfung unbestätigt. Die gewählte Proxyvorlage verwendet HTTP
zum Origin und erhält die vorhandenen Cloudflare-SSL-Einstellungen. Siehe
[Domainmigration](../deploy/DOMAIN_MIGRATION.md).

## Vorschau

```powershell
python -m http.server 8080 --directory website
```

Dann `http://localhost:8080/` öffnen.

Im Hosting-Repository liegen diese Dateien direkt im Root; dort `--directory .` verwenden.
Release-Erstellung und GitHub-Pages-Veröffentlichung sind in `DEPLOYMENT.md` beschrieben.

Die drei Nutzungsseiten werden in sieben Sprachen ausgeliefert. Nach Textänderungen alle
Kataloge in `assets/locales/` ergänzen und `python scripts/build_site_i18n.py` ausführen.
Website-Änderungen allein verändern die bereits veröffentlichten Software-Artefakte nicht.

## Versionsauswahl

`#versionen` vergleicht Colab, Desktop und Video Studio mit Voraussetzungen und direkten
Einstiegen. Ohne JavaScript bleiben alle drei Versionskarten und Links sichtbar. Mit JavaScript
filtern native Radiobuttons nach Bedarf; die Auswahl ist über `#version-colab`,
`#version-desktop` und `#version-studio` direkt verlinkbar und bleibt beim Sprachwechsel
erhalten. „Alle vergleichen“ zeigt wieder sämtliche Optionen.

Das Studio setzt einen erhaltenen Zugang voraus und ist als Testbetrieb gekennzeichnet.
Die Desktop-Anleitung führt zur lokalen Einrichtung; der Download ist ausdrücklich
als Quellcode statt als fertiger Installer beschrieben. Eine explizite deutsche
Sprachwahl (`?lang=de`) hat auch bei gesperrtem Browserspeicher Vorrang vor der Erkennung.

## Design und Sprachbeispiel

Das helle Standardschema kombiniert Manrope, blaue Akzente und einheitliche SVG-Piktogramme.
Ein dunkles Schema kann gewählt werden; die Präferenz bleibt lokal gespeichert.
Alle sieben Sprachfassungen nutzen dieselben deutschen Vorlagen und vollständige Textkataloge.
Das Sprachbeispiel auf der Startseite wechselt zwischen drei vorformulierten Übersetzungen.
Es ist ausdrücklich als Textbeispiel gekennzeichnet, ruft keinen Dienst auf und spielt kein Audio ab.
Ohne JavaScript bleibt das englische Beispiel sichtbar; nicht funktionierende Auswahlknöpfe bleiben verborgen.

Geprüft am 27. Oktober 2026: 19 Website-Tests, 12 bestehende Upload-Tests,
alle 21 Produktseiten und die fünf Studio-/Neben-/Rechtseiten bei 320 px,
Startseiten zusätzlich bei 760, 1060 und deutscher Desktopansicht, Versionsfilter per
Tastatur, Sprachwechsel mit Versionsauswahl, Mobilmenü und Hell-/Dunkelschema.
Die lokale Studio-Vorschau zeigt ohne API den erwarteten Verbindungsfehler;
eine echte Videoübersetzung war nicht Bestandteil der Designprüfung.
