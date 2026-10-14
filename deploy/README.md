# Betrieb und Remote-Worker

Stand: 15. Oktober 2026. Der Dienst ist auf Debian 13 unter
[Webversion öffnen](https://stayinleipzig.de/linguoai/web.html) eingerichtet.
Die API lauscht auf `127.0.0.1:8765`; Nginx veröffentlicht den HTTPS-Unterpfad
`/linguoai/`. `linguoai-web.service` und `linguoai-worker.service` sind aktiviert.
Die Dateien in diesem Verzeichnis dienen außerdem als Vorlagen für weitere Hosts.
Sie enthalten keine Zugangsdaten.

Die Migration auf `https://studio.linguoai.de/` ist vorbereitet, aber öffentlich
noch nicht bestätigt. Für die gewählte Cloudflare-Anbindung dient
[`nginx-studio-http-proxy.conf`](nginx-studio-http-proxy.conf): HTTPS bis Cloudflare,
HTTP zwischen Cloudflare und Nginx, unveränderte Cloudflare-SSL-Einstellungen und
orangefarbener Proxy. Dafür ist kein Origin-Zertifikat erforderlich.
[`DOMAIN_MIGRATION.md`](DOMAIN_MIGRATION.md) beschreibt Installation, Prüfungen und
Adresswechsel. Andere Domains und ihre TLS-Konfigurationen bleiben unverändert.

## Aktueller Host

| Bereich | Wert |
| --- | --- |
| Quellcode und Venv | `/opt/linguoai-web`, `/opt/linguoai-web/.venv` |
| Dienstbenutzer | `linguoai-web` |
| Zustand / Arbeitsdateien | `/var/lib/linguoai-web`, `/var/lib/linguoai-web/work` |
| Private Konfiguration | `/etc/linguoai-web/server.json`, `/etc/linguoai-web/worker.env` |
| Parakeet-v3-Modell | `/var/lib/linguoai-web/models/parakeet-v3` |
| Dateispeicher | Privater Google-Drive-Ordner „linguoai uploads“; seit 15. Oktober 2026 aktiv |
| Eingabe / Ergebnis je Datei | 100 MiB / 1 GiB |
| Aktive Aufträge je Benutzer / insgesamt | 2 / 20 |
| Aufbewahrung / Worker-Lease | 24 Stunden / 90 Sekunden |
| API-CPU / Worker-CPU | 50 % / 150 % |
| API-RAM / Worker-RAM maximal | 512 MiB / 2 GiB |

Ein Worker verarbeitet jeweils einen Auftrag. Parakeet verwendet vier Threads
und 30-Sekunden-Audioabschnitte. Wartende Aufträge benötigen trotzdem Speicher;
Modelle, Arbeitsdateien und lokale Transferpuffer müssen auf die vorhandene Platte passen.
Die API entfernt abgeschlossene Dateien nach Ablauf der Aufbewahrungsfrist.

## Aktuelle Übersetzungsgrenze

Ein echter Server-Test hat deutsches Audio mit Parakeet erkannt:
2,752 Sekunden Audio, 2,104 Sekunden Decoderzeit (RTF 0,765), 4,739 Sekunden
Modellstart und etwa 835 MB RSS. Die folgende öffentliche Google-Übersetzung wurde
mit HTTP 429 („Too Many Requests“) abgewiesen. Ein vollständiges echtes übersetztes
Video wurde damit noch nicht geprüft. Die automatisierten API-/Worker-Integrationstests
verwenden eine simulierte Pipeline und bestätigen den technischen Auftragsablauf.

Der Nutzer hat entschieden, den Übersetzungszugang später einzurichten. Für DeepL
oder Gemini muss der Betreiber die jeweilige Abhängigkeit installieren und den
entsprechenden Schlüssel privat in `worker.env` konfigurieren. Danach den Worker
neu starten und einen echten kleinen Auftrag samt Downloads prüfen. Dafür wurden
bislang keine neuen Anbieterzugänge eingerichtet. Der private Drive-Zwischenspeicher
ist unabhängig von diesen Übersetzungszugängen aktiv.

## Installation auf einem weiteren API-Host

1. Python-Venv, FFmpeg und CA-Zertifikate installieren. Den Dienstbenutzer
   `linguoai-web` anlegen; Quellcode einschließlich `website/` nach
   `/opt/linguoai-web` kopieren. Quellcode und Venv bleiben root-eigen.
2. `/opt/linguoai-web/.venv` erstellen und aus dem Projektordner
   `pip install ".[web,drive,parakeet]"` ausführen. Torch/NLLB ist für den
   bereitgestellten CPU-Betrieb nicht erforderlich.
3. `/var/lib/linguoai-web/{tmp,work,cache,models}` dem Dienstbenutzer mit Modus `0700`
   zuweisen. `/etc/linguoai-web` erhält `root:linguoai-web` und Modus `0750`.
   Private Dateien bleiben `0600` beim Dienstbenutzer oder `0640` bei `root:linguoai-web`.
4. Die private Konfiguration interaktiv erzeugen; bestehende Dateien werden nicht überschrieben:

   ```sh
   /opt/linguoai-web/.venv/bin/python scripts/init_web.py \
     --config /etc/linguoai-web/server.json \
     --state /var/lib/linguoai-web \
     --public-url https://stayinleipzig.de/linguoai \
     --website /opt/linguoai-web/website \
     --worker-env /etc/linguoai-web/worker.env \
     --model-directory /var/lib/linguoai-web/models/parakeet-v3
   ```

5. Das geprüfte CPU-Modell herunterladen und dem Dienstbenutzer zugänglich machen:

   ```sh
   /opt/linguoai-web/.venv/bin/python scripts/download_parakeet.py \
     --directory /var/lib/linguoai-web/models/parakeet-v3
   ```

6. Beide `.service`-Dateien nach `/etc/systemd/system/` kopieren:

   ```sh
   systemctl daemon-reload
   systemctl enable --now linguoai-web.service linguoai-worker.service
   curl --fail http://127.0.0.1:8765/healthz
   ```

7. Für den bisherigen HTTPS-Unterpfad das Nginx-Snippet innerhalb des bestehenden
   Serverblocks einfügen; nach `nginx -t` Nginx neu laden. Port 8765 bleibt auf Loopback.
   Für Studio hinter Cloudflare stattdessen der separaten
   [Domainanleitung](DOMAIN_MIGRATION.md) folgen. Die Gesamtdateigrenzen bleiben
   100 MiB für Eingaben und 1 GiB für authentifizierte Worker-Ergebnisse.

## Uploads in kleinen Blöcken

Die Browseroberfläche nutzt `chunks-v1` für jede Eingabe; Worker verwenden es für
Ergebnisdateien über 8 MiB. Kleinere Worker-Ergebnisse und ältere Clients können
weiterhin die bestehenden Multipart-Endpunkte verwenden. Für große öffentliche
Transfers ist der Chunk-Weg erforderlich: Die ältere Nginx-Grenze von `1025m`
für Worker-Multipart vergrößert die Cloudflare-Grenze je Request nicht.

Das Protokoll trennt Anlegen, Rohdatenblöcke und Abschluss. Die folgenden Pfade
stehen relativ zur öffentlichen API-Adresse; beim bisherigen Host kommt
`/linguoai` davor, bei Studio liegen sie an der Domainwurzel:

| Schritt | Browser | Worker |
| --- | --- | --- |
| Anlegen | `POST /api/uploads` | `POST /api/worker/jobs/{job_id}/uploads/{artifact_name}` |
| Status / bestätigter Offset | `GET /api/uploads/{upload_id}` | `GET /api/worker/jobs/{job_id}/uploads/{upload_id}` |
| Block | `PUT /api/uploads/{upload_id}?offset=N` | `PUT /api/worker/jobs/{job_id}/uploads/{upload_id}?offset=N` |
| Abschluss | `POST /api/uploads/{upload_id}/complete` | `POST /api/worker/jobs/{job_id}/uploads/{upload_id}/complete` |
| Abbruch | `DELETE /api/uploads/{upload_id}` | `DELETE /api/worker/jobs/{job_id}/uploads/{upload_id}` |

Ein Block enthält höchstens 8 MiB Rohdaten und den Header `X-Chunk-SHA256`.
Metadaten sind auf 64 KiB begrenzt. Die API prüft die angegebene Gesamtgröße,
Anmeldung und Eigentümer vor der Datenannahme; Worker benötigen zusätzlich ihren
Worker-Schlüssel sowie die aktuelle Job-Lease. Abgelaufene Worker können keine
Ergebnisse veröffentlichen. Upload-IDs ersetzen keine Zugangsprüfung.

Die geöffneten Clients wiederholen vorübergehende Netzwerkfehler begrenzt und
fragen dabei den bestätigten Offset ab. Verlorene Antworten führen dadurch nicht
zu doppelten Blöcken oder doppelten Aufträgen. Das Protokoll speichert den Stand
in `state_directory/uploads/`; Datenbank und private `.part`-Dateien müssen bei
Updates und Neustarts erhalten bleiben. Ein neu geöffneter Browser erhält dadurch
keinen automatischen Zugriff auf seine zuvor ausgewählte lokale Datei.

Der Abschluss läuft im Hintergrund und kann nach einem API-Neustart fortgesetzt
werden. Die Eingabe wird erst nach vollständiger Speicherung für die Warteschlange
freigegeben. Die Oberfläche zeigt danach den normalen Auftragsstatus; fertige
Ergebnisse sind nur dem angemeldeten Besitzer zugänglich. Abbruch oder Verlust der
Lease verhindert eine spätere erfolgreiche Veröffentlichung. Abgebrochene und
abgelaufene Transfers werden automatisch bereinigt; bereits angehängte Ergebnisse
bleiben bis zur Auftrags-Aufbewahrungsfrist erhalten.

Die Studio-Proxyvorlage begrenzt Block-Requests auf `8m` und schaltet Request-/
Response-Buffering sowie Proxy-Caching aus. Private API- und Downloadantworten
verwenden `Cache-Control: no-store`; Cloudflare darf dieses Verhalten nicht mit
zwischengespeicherten privaten Antworten überschreiben. Blockgrenze und
Gesamtdateigrenzen werden von der API unabhängig geprüft.

## Konfiguration und Modelle

`server.json` enthält `state_directory`, `website_directory`, `public_url`, `users`,
`session_secret`, `worker_token`, `storage_backend`, `max_upload_bytes`,
`max_result_bytes`, `max_active_per_owner`, `max_active_total`, `retention_seconds`
und `lease_seconds`. `users` enthält PBKDF2-Hashes aus
`linguoai.web_settings.password_hash`; Sitzungs- und Worker-Schlüssel sind getrennte
private Zufallswerte mit mindestens 32 Zeichen. Der Initialisierer erzeugt diese
Werte ohne Ausgabe der Schlüssel.

`worker.env` hält den Worker-Schlüssel und operatorseitige Modellpfade.
Für den eingerichteten Host gelten:

```dotenv
LINGUOAI_PARAKEET_MODEL_DIRECTORY=/var/lib/linguoai-web/models/parakeet-v3
LINGUOAI_WORKER_TEMP=/var/lib/linguoai-web/work
LINGUOAI_MODEL_CACHE=/var/lib/linguoai-web/cache
LINGUOAI_WORKER_DEVICE=cpu
LINGUOAI_WORKER_COMPUTE_TYPE=int8
```

`LINGUOAI_WHISPER_MODEL` bestimmt das Whisper-Modell. Optional gibt es
`LINGUOAI_PARAKEET_GERMAN_MODEL_DIRECTORY`, `DEEPL_API_KEY`, `GEMINI_API_KEY`,
`LINGUOAI_PIPER_MODEL` und `LINGUOAI_PIPER_CONFIG` für entsprechend installierte
Modelle und Erweiterungen. Ohne passende Dateien oder Abhängigkeiten meldet der
Worker diese Fähigkeiten nicht. Das ONNX-Profil benötigt `tokens.txt`,
`encoder.int8.onnx`, `decoder.int8.onnx` und `joiner.int8.onnx`, alternativ die
Dateinamen ohne `.int8`. Der Downloader prüft die veröffentlichten Modell-Checksummen.

## Eigenständiger Job-Bot auf einem anderen Rechner

Ein Job-Bot startet ausschließlich `linguoai.web_worker`. Er benötigt keine
Upload-Webseite, Browser-Anmeldung, eingehenden Ports oder Drive-Zugangsdaten.
Der Betreiber stellt ihm den **separaten Worker-Schlüssel** privat bereit; dieser
Schlüssel ist kein Nutzerpasswort und gehört nicht in Kommandozeilen oder Logs.
Er berechtigt zur Worker-API und wird ausschließlich auf einem vertrauenswürdigen
Betreiberrechner gespeichert.

1. Auf dem Bot-Rechner FFmpeg, Python und den Projektquellcode bereitstellen.
   Im Projektordner:

   ```sh
   python3 -m venv .venv
   .venv/bin/python -m pip install ".[web,parakeet]"
   ```

2. Private, beschreibbare Arbeits- und Cacheordner sowie gegebenenfalls einen
   Parakeet-Modellordner anlegen. Den Downloader von oben mit dem eigenen Modellpfad
   verwenden. Eine nur für den Bot lesbare `worker.env` mit dem bereitgestellten
   `LINGUOAI_WORKER_TOKEN` und den eigenen Modell-/Arbeits-/Cachepfaden speichern.
   Dafür keine neue Serverkonfiguration und keinen neuen Browserzugang erzeugen.
3. Die private Umgebung laden und den Worker starten:

   ```sh
   set -a
   . /privater/pfad/worker.env
   set +a
   .venv/bin/python -m linguoai.web_worker \
     --server https://stayinleipzig.de/linguoai \
     --worker-id remote-cpu-01
   ```

   `/privater/pfad/worker.env` ist ein zu ersetzender Dateipfad. Der Worker fragt
   passende Aufträge ab, erhält eine Lease, lädt deren Eingaben über HTTPS und
   überträgt Video und Untertitel zurück. Ergebnisdateien über 8 MiB werden in
   Blöcken mit bestätigtem Offset und SHA-256 gesendet. Heartbeats laufen während
   der Übertragung weiter; bei Abbruch oder Lease-Verlust wird die Übertragung
   beendet. Er verarbeitet keine Browser-Sitzung und kontaktiert Drive nicht direkt.
   `--once` verarbeitet höchstens einen echten wartenden Auftrag und beendet sich
   danach; es ist kein Probelauf ohne Verarbeitung.
4. Für Dauerbetrieb die Worker-Unit übernehmen und Benutzer, lokale Pfade,
   `EnvironmentFile` und `ExecStart` auf diesem Rechner anpassen. Die lokale
   `linguoai-web.service`-Abhängigkeit entfernen und in `ExecStart` die öffentliche
   HTTPS-Adresse verwenden. Nur die Worker-Unit installieren und aktivieren.

Weitere Worker verwenden eindeutige IDs. Die Warteschlange verteilt Aufträge nur
an Worker mit passendem ASR-Profil, Übersetzungsdienst und TTS-Anbieter. GPU- oder
zusätzliche lokale Modelle werden auf dem jeweiligen Bot eingerichtet; ihre
Ressourcen sind unabhängig vom kleinen API-Host.

## Google Drive als privater Betreiber-Zwischenspeicher

Der laufende Dienst verwendet seit dem 15. Oktober 2026 `storage_backend: drive`.
Der private persönliche Betreiberordner „linguoai uploads“ ist angelegt und mit
echten OAuth-Zugangsdaten autorisiert. Der serverseitige Upload-, Download- und
Löschtest war erfolgreich. Ein isolierter HTTP-Rundlauf mit synthetischen Dateien
bestätigte außerdem Worker-Abruf, drei Ergebnisse und geschützte Downloads;
alle Downloads waren bytegleich, anonyme Ergebnisdownloads wurden mit HTTP 401
abgewiesen. Die produktive Queue blieb bei diesem Test unverändert und die vier
erzeugten Drive-Testdateien wurden gelöscht.
**Endnutzer benötigen kein Google-Konto; Drive bleibt im Nutzerablauf verborgen.**

Der konkrete Einrichtungsablauf steht in [Google Drive einrichten](GOOGLE_DRIVE.md).
Der Speicherrouter hält vorhandene lokale Dateien weiterhin für Downloads und
Bereinigung erreichbar. Für Verarbeitung, Uploads und Downloads werden weiterhin
begrenzte lokale Arbeits- und Transferpuffer benötigt.

Die private `server.json` verwendet diese Speicherfelder:

- `storage_backend`: `drive`
- `drive_folder_id`: ID des privaten, für die App erreichbaren Betreiberordners
- `drive_credentials_path`: `/etc/linguoai-web/drive-credentials.json`
- `drive_shared`: `false` beim aktiven persönlichen OAuth-Zugriff; `true` nur für Shared Drive

Für persönliches Drive ist `authorized_user`-JSON mit `drive.file`-Zugriff nötig.
`service_account`-JSON benötigt einen echten Workspace Shared Drive; ein bloß
freigegebener persönlicher Ordner reicht nicht. Zugangsdaten bleiben serverseitig,
der Ordner bleibt privat. Nach Änderungen an der Speicheranbindung die API neu
starten und Upload, Download und Löschung mit einem kleinen Auftrag prüfen.
Drive-Quoten und Anbieterbedingungen
gelten weiterhin; temporäre Verarbeitung benötigt weiterhin lokalen Speicher.

## Kontrolle, Updates und optionale Container

```sh
systemctl status linguoai-web.service linguoai-worker.service
journalctl -u linguoai-web.service -u linguoai-worker.service --since today
systemctl restart linguoai-web.service linguoai-worker.service
```

Die Units setzen `MemoryHigh`/`MemoryMax`, `TasksMax=96`, schreibgeschützte Systempfade,
`SIGINT` und eine 30-Sekunden-Stop-Frist. Bei OOM-Abbrüchen Modell und tatsächlichen
Spitzenbedarf prüfen. Updates nach abgeschlossenem Auftrag oder dessen Abbruch
installieren. Eine Rotation des Sitzungsschlüssels beendet bestehende Browser-Sitzungen;
bei Rotation des Worker-Schlüssels müssen alle Betreiber-Bots aktualisiert werden.

Docker Compose ist optional. Vor `docker compose -f deploy/compose.yaml up -d --build`
`LINGUOAI_CONFIG_DIRECTORY`, `LINGUOAI_STATE_DIRECTORY` und
`LINGUOAI_WORKER_ENV_FILE` auf private Hostpfade setzen. Zustand und Arbeitsordner
müssen UID/GID `10001:10001` gehören; Konfiguration benötigt Lesezugriff für GID `10001`
(`0750`/`0640`). Im Container zeigt `website_directory` auf `/app/website` und die
Modellpfade bleiben unter `/var/lib/linguoai-web/models/`. Zugangsdaten werden
schreibgeschützt zur Laufzeit eingebunden. Der Worker teilt den API-Netzwerk-Namespace
und verwendet Loopback. Auf dem kleinen Debian-Host läuft die systemd-Variante.
