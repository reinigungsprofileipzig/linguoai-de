# studio.linguoai.de hinter Cloudflare

Stand: 14. Oktober 2026. Ziel ist `https://studio.linguoai.de/` auf der
Origin-IPv4 `195.90.219.12`. Der Nutzer möchte den orangefarbenen Cloudflare-Proxy
und die vorhandenen SSL-Einstellungen beibehalten. Der vorbereitete Betrieb
verwendet HTTPS vom Browser zu Cloudflare und HTTP von Cloudflare zu Nginx.
Für diesen Weg wird kein Origin-Zertifikat benötigt. Die CPU-Ersparnis wurde
nicht gemessen. [Cloudflare Flexible](https://developers.cloudflare.com/ssl/origin-configuration/ssl-modes/flexible/)

Diese Datei beschreibt die Vorbereitung und die Prüfungen vor der Aktivierung.
Sie bestätigt keine bereits abgeschlossene Migration. Die bisherige Adresse
ist `https://stayinleipzig.de/linguoai/`. Die öffentliche HTTPS-Seite der neuen Domain liefert derzeit die StayInLeipzig-Website; ein eindeutiger Studio-Prüfpfad antwortet dort mit 404. Derselbe Prüfpfad antwortet am vorbereiteten HTTP-Origin über Loopback mit 200. Direkte Origin-Zugriffe mit gefälschten Cloudflare-Headern werden mit 403 abgewiesen. Die vorhandene Cloudflare-Anmeldung ist in der verfügbaren Browsersitzung noch nicht vorhanden. Ohne Einblick in DNS-Ziel und Origin-Routing lässt sich die Ursache nicht eindeutig bestimmen. Der API-public_url bleibt deshalb auf der bisherigen funktionierenden Adresse. Zonenweite SSL-Änderungen werden nicht vorgenommen.

## DNS und HTTP-Origin

Den bestehenden Eintrag beibehalten:

| Typ | Name | Origin-Ziel | Proxy |
| --- | --- | --- | --- |
| A | `studio` | `195.90.219.12` | **Proxied**, orange Wolke |

Die ausgewählte Konfiguration ist `deploy/nginx-studio-http-proxy.conf`.
Sie wird im Nginx-`http`-Kontext geladen, wie es Debian mit `sites-enabled`
bereits vorsieht. `ngx_http_realip_module` muss verfügbar sein; vor der
Installation mit `nginx -V` und anschließend `nginx -t` prüfen.
Die früher vorbereiteten TLS-Dateien `nginx-studio.conf` und
`nginx-studio-cloudflare.conf` sind Alternativen und werden hierfür nicht geladen.
Bereits vorhandene Schlüssel und Zertifikatsanfragen bleiben privat und ungenutzt.

Der HTTP-VHost lauscht ausschließlich auf Port 80 für `studio.linguoai.de`
und leitet an `http://127.0.0.1:8765` weiter. Ein bisheriger Studio-Bootstrap-VHost
muss ersetzt werden, damit keine konkurrierenden `server_name`-Blöcke geladen
werden. Andere Domains und ihre TLS-Konfigurationen bleiben unverändert.

Die Konfiguration begrenzt Origin-Zugriffe auf die veröffentlichten
Cloudflare-IP-Netze und Loopback. `geo` prüft dafür die ursprüngliche Socket-IP
in `$realip_remote_addr`, auch nachdem Nginx die Besucher-IP wiederherstellt.
`CF-Connecting-IP` wird nur von diesen Cloudflare-Netzen übernommen; eingehende
`X-Forwarded-For`-Ketten werden ersetzt. Das vermeidet, dass ein direkter
Origin-Client mit selbst gesetzten Proxy-Headern Vertrauen erhält.
Die IPv4- und IPv6-Listen wurden am 14. Oktober 2026 gegen
[Cloudflare IPv4](https://www.cloudflare.com/ips-v4/) und
[Cloudflare IPv6](https://www.cloudflare.com/ips-v6/) geprüft. Änderungen an diesen
Listen in **beiden** Teilen des Templates aktualisieren: `geo` und
`set_real_ip_from`. [Nginx Real-IP-Modul](https://nginx.org/en/docs/http/ngx_http_realip_module.html)

Nginx übergibt `X-Forwarded-Proto: https` nur, wenn ein zugelassener
Cloudflare-Peer die ursprüngliche Browserverbindung als HTTPS meldet. Für
Diagnosen darf ausschließlich Loopback denselben Header setzen. Es gibt keinen
pauschalen Redirect anhand des Origin-`$scheme`: Das würde bei Flexible eine
Schleife erzeugen. Ein HTTP-Browserrequest wird nur dann auf die öffentliche
HTTPS-Adresse umgeleitet, wenn der verifizierte Cloudflare-Peer ausdrücklich
`X-Forwarded-Proto: http` meldet. HTTPS-Browserrequests bleiben erreichbar, obwohl
Cloudflare Nginx über HTTP anspricht.
[Cloudflare Proxy-Header](https://developers.cloudflare.com/fundamentals/reference/http-headers/),
[Cloudflare Redirect-Schleifen](https://developers.cloudflare.com/ssl/origin-configuration/ssl-modes/flexible/#limitations)

## Kleine Transfers für den Proxy

Browser und Remote-Bots übertragen große Dateien in Rohdatenblöcken von maximal
**8 MiB**. Jeder Block erhält einen Offset und eine SHA-256-Prüfsumme; ein
erneuter Versuch kann am bestätigten Offset fortsetzen. Das Anlegen und
Abschließen eines Transfers verwendet separate JSON-Requests. Die API begrenzt
Metadaten auf 64 KiB und prüft Anmeldung beziehungsweise Worker-Schlüssel,
Eigentümer, Lease und Gesamtdateigröße unabhängig von Nginx.

Die Chunk-Pfade sind:

- Browser: `PUT /api/uploads/{upload_id}?offset=N`.
- Worker: `PUT /api/worker/jobs/{job_id}/uploads/{upload_id}?offset=N`.
- Prüfsummenheader: `X-Chunk-SHA256`.

Nginx begrenzt diese Pfade auf `8m` je Request. Kompatible ältere Clients können
die bisherigen Multipart-Endpunkte verwenden: `POST /api/jobs` erhält `101m`,
Worker-Artefakte unter `/api/worker/jobs/{job_id}/artifacts/{name}` erhalten
`1025m`. Die API prüft weiterhin die eigentlichen Dateiobergrenzen von
100 MiB pro Eingabe und 1 GiB pro Ergebnis. Der lokale Worker nutzt Loopback.

Diese Legacy-Origin-Grenzen heben die Cloudflare-Grenze nicht auf: Cloudflare
nennt für Free und Pro 100 MB je proxied Request. Große Dateien über die
öffentliche Domain müssen daher den Chunk-Weg nutzen. Eine öffentliche
1-GiB-Gesamtübertragung gilt erst nach einem echten Transfer als bestätigt.
[Cloudflare Uploadgrenzen](https://developers.cloudflare.com/cache/concepts/default-cache-behavior/#upload-limits)

Request- und Response-Buffering sowie Nginx-Proxy-Caching sind ausgeschaltet.
Private API- und Downloadantworten behalten `Cache-Control: no-store`; für diese
Pfade darf keine Cloudflare-Regel das Caching erzwingen. Eine Ausnahme von einer
bestätigten blockierenden Regel nur für den betroffenen Host beziehungsweise
API-Pfad einrichten, ohne zonenweit Schutz- oder SSL-Einstellungen zu ändern.

## Adresse umstellen und prüfen

Vor dem Wechsel laufende Aufträge abschließen oder abbrechen. Die private
`/etc/linguoai-web/server.json` sichern, ohne ihre Schlüssel auszugeben.
Benutzer, Sitzungsschlüssel, Worker-Schlüssel, Datenbank und Speicher beibehalten;
nur `public_url` auf `https://studio.linguoai.de` setzen. Eine vorhandene
`LINGUOAI_PUBLIC_URL`-Umgebungsvariable muss denselben Wert enthalten.
Nginx-Konfiguration validieren und neu laden, danach die API neu starten.

Die Anwendung verwendet dann automatisch:

- Uvicorn-/FastAPI-`root_path` ohne `/linguoai`-Präfix.
- Cookiepfad `/`, weiterhin `Secure`, `HttpOnly` und `SameSite=Strict`.
- Browser-Origin `https://studio.linguoai.de` für die CSRF-Prüfung.
- Oberfläche `/web.html` und API-Pfade `/api/...`.

Zuerst auf dem Server prüfen, ohne öffentliche Zugangsdaten auszugeben:

```sh
curl --fail --silent --show-error \
  -H 'Host: studio.linguoai.de' \
  -H 'X-Forwarded-Proto: https' \
  http://127.0.0.1/healthz
```

Danach öffentlich `https://studio.linguoai.de/healthz`, `/web.html` und die
Assets prüfen. Ein direkter Origin-Request von außerhalb Cloudflare und Loopback
muss 403 liefern, auch mit gefälschten Cloudflare-Headern. Öffentliches HTTP muss
einmal auf HTTPS weiterleiten und darf keine Schleife bilden. Login, Logout,
Cookieattribute, Upload in mehreren Blöcken, Wiederaufnahme, eigene Auftragsliste,
Abbruch und Remote-Heartbeat mit einer Testdatei prüfen. Ergebnisdownloads nach
Einrichtung des Übersetzungszugangs mit einem abgeschlossenen Auftrag prüfen.
Der bekannte Google-HTTP-429-Fehler ist unabhängig vom Domainwechsel; der Nutzer
möchte den Übersetzungszugang später einrichten.

Bei einer öffentlichen 403-Antwort anhand von Cloudflare-Request-ID und
Origin-Zugriffslog feststellen, ob der Request Nginx erreicht. Ohne diese Prüfung
keine bestimmte WAF-, Challenge- oder DNS-Ursache behaupten und keine breite
Cloudflare-Ausnahme anlegen.

## Bots und bisherige Adresse

Der lokale Worker bleibt bei `http://127.0.0.1:8765`. Remote-Bots werden erst nach
bestätigter öffentlicher Erreichbarkeit auf die neue Adresse umgestellt:

```sh
python -m linguoai.web_worker --server https://studio.linguoai.de --worker-id remote-cpu-01
```

Worker-Schlüssel bleiben privat. Remote-Worker folgen Weiterleitungen nicht
automatisch; ihre Adresse ausdrücklich aktualisieren. Den neuen Uploadweg für
ein Ergebnis größer als einen Block prüfen.

Erst nach erfolgreicher öffentlicher Prüfung die alte Browseradresse
`https://stayinleipzig.de/linguoai/` auf die neue Domain weiterleiten. Den alten
Präfix entfernen, Restpfad und Query erhalten; andere Bereiche von
stayinleipzig.de beibehalten. Projektlinks erst nach dem bestätigten Wechsel
aktualisieren. Bei einem Fehler `public_url` aus der Sicherung wiederherstellen,
API neu starten und Remote-Bots auf ihre bisherige Adresse zurücksetzen.
