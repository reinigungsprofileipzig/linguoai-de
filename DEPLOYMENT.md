# linguoai.de veröffentlichen

Die statische Website wird aus dem Root von
`reinigungsprofileipzig/linguoai-de`, Branch `main`, über GitHub Pages veröffentlicht.
Den bestehenden `CNAME` mit `linguoai.de` erhalten. Cloudflare Tunnel ist ausschließlich
eine optionale Verbindung zur kurzlebigen Colab-App, kein Ersatz für das Website-Hosting.

## Release prüfen und erstellen

Das lokale Arbeitsprojekt hat die Website im Unterordner `website/`; das Hosting-Repository
hat dieselben Dateien im Root. Die Build-Skripte unterstützen beide Strukturen.

```powershell
python -m pip install -e ".[dev]"
python scripts/build_notebook.py
python scripts/build_site_i18n.py
python scripts/build_release.py
python -m pytest
python -m ruff check linguoai tests scripts
```

`build_release.py` erstellt das Wheel in einer sauberen temporären Kopie des Quellcode-ZIPs,
vergleicht jedes Python-Modul mit dem Arbeitsstand und veröffentlicht erst nach erfolgreicher
Prüfung Notebook, ZIP, Wheel und `SHA256SUMS-<Version>.txt` im Downloadordner.
Bei einer neuen Version auch Notebook-Vorlage, Paketmetadaten und Website-Texte anpassen.
Ältere Downloads für bestehende Links unverändert aufbewahren.

## Vorschau und GitHub-Push

```powershell
# Lokales Arbeitsprojekt:
python -m http.server 8080 --bind 127.0.0.1 --directory website
# Im Hosting-Repository stattdessen --directory . verwenden.
```

Desktop- und Mobilnavigation, alle Sprachversionen, Colab-Anleitung und Downloads prüfen.
Im Hosting-Repository Quellcode, Tests, Skripte und Website gemeinsam committen und normal
nach `main` pushen; keine fremde Historie überschreiben und keinen Force-Push verwenden.
Die Website-Dokumentation heißt dort `WEBSITE.md`, die Projektanleitung `README.md`.

Nach erfolgreichem Pages-Build die tatsächliche Domain prüfen: Startseite und Colab-Seite
müssen die neue Version zeigen, Notebook und Downloads mit den lokalen Prüfsummen übereinstimmen.

## Colab: dynamischer Gradio-Link (Standard)

`SHARE_PROVIDER = "gradio"` benötigt kein Cloudflare-Konto, keine eigene Domain und keinen
Tunnel-Token. Die einzelne Startzelle installiert das Projekt und zeigt einen dynamischen,
passwortgeschützten Gradio-Link. Die Sitzung muss aktiv bleiben. Die Uploadgrenze ist 5 GB
je Datei; für große Dateien steht alternativ Google Drive zur Verfügung.

## Optional: eigener benannter Cloudflare Tunnel

1. Einen remotely managed Tunnel im eigenen Cloudflare-Konto erstellen.
2. Eine Published-application-Route für einen eigenen Hostnamen zum Service
   `http://127.0.0.1:7860` konfigurieren.
3. Den Token ausschließlich in Colab Secrets als `CLOUDFLARE_TUNNEL_TOKEN` hinterlegen.
   Niemals in Git, URLs, Notebook-Quelltext oder Chat kopieren.
4. `SHARE_PROVIDER = "cloudflare"` und `CLOUDFLARE_TUNNEL_HOSTNAME` setzen.
5. Startzelle ausführen; sie installiert `cloudflared` nur bei Bedarf, wartet auf dessen
   Bereitschaft und räumt den Prozess beim Abbruch wieder auf.
6. Die eigene HTTPS-Route und den Login im Browser prüfen. Optional Cloudflare Access ergänzen.

`app.linguoai.de` ist ein Beispiel, kein von diesem Projekt bereitgestellter Server.
Die Adresse ist nur bei aktiver Colab-Sitzung erreichbar. Für diesen Modus gilt eine
vorsorgliche Uploadgrenze von 95 MB pro Datei; größere Videos über Drive einlesen.
Quick Tunnels werden wegen fehlender Unterstützung für Server-Sent Events nicht verwendet.
Siehe [Cloudflare Quick Tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/).

## Betreiberangaben

Impressum und Datenschutz sind weiterhin zu vervollständigende Entwürfe. Keine Namen,
Anschriften oder rechtlichen Zusicherungen erfinden. Der Betreiber muss diese Angaben prüfen
und ergänzen; bis dahin die vorhandene Kennzeichnung und `noindex,nofollow` beibehalten.
