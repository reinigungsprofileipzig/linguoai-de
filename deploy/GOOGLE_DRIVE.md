# Privaten Google-Drive-Speicher verbinden

Endnutzer benötigen kein Google-Konto. Der Betreiber autorisiert einmal seinen eigenen Drive; die Server-App legt den Ordner „linguoai uploads“ an. Der Dienst bleibt bis zur erfolgreichen Freigabe und Prüfung auf dem vorhandenen Serverspeicher.

## 1. Google-Zugang vorbereiten

Im [Google-Cloud-Clientbereich](https://console.cloud.google.com/auth/clients) mit dem Konto anmelden, dessen Drive verwendet werden soll. Ein bestehendes eigenes Projekt verwenden oder ein eigenes Projekt für LinguoAI anlegen. Die Google Drive API aktivieren und die OAuth-Einrichtung für dieses Betreiberkonto vervollständigen.

Einen OAuth-Client vom Typ **Desktop-App** anlegen und dessen JSON privat herunterladen. Die Datei in `.linguoai-web/drive-operator/client.json` speichern. Keine Schlüssel in den Chat, Git oder öffentliche Downloads kopieren. Unter Windows den Ordnerzugriff auf das eigene Konto und Systemadministratoren begrenzen.

Die App fordert ausschließlich `https://www.googleapis.com/auth/drive.file` an. Das erlaubt den Zugriff auf Dateien, die diese App erstellt oder für die sie gezielt freigegeben wurde. Ein über eine andere App angelegter Ordner ist deshalb nicht automatisch für diesen Serverclient nutzbar. [Google: Drive-Zugriff](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)

Für dauerhaftes Arbeiten den OAuth-Veröffentlichungsstatus prüfen: Bei externen Apps im Status **Testing** läuft ein Refresh-Token für Drive-Zugriff nach sieben Tagen ab. Die Betreiberfreigabe muss bei Änderungen oder Widerruf gegebenenfalls erneuert werden. [Google: Token-Laufzeit](https://developers.google.com/identity/protocols/oauth2#expiration)

## 2. Betreiberfreigabe auf diesem Computer

Im Projektverzeichnis ausführen:

```powershell
python -m pip install -e ".[drive]"
python -m scripts.connect_drive --client-file .linguoai-web/drive-operator/client.json --credentials-file .linguoai-web/drive-operator/drive-credentials.json --config-fragment .linguoai-web/drive-operator/drive-settings.json
```

Das Skript öffnet den Systembrowser. Die Google-Freigabe selbst bestätigen. Die Rückmeldung kommt ausschließlich über einen lokalen Loopback-Port mit PKCE; ein Kopieren von Autorisierungscodes ist nicht erforderlich. [Google: Desktop-OAuth](https://developers.google.com/identity/protocols/oauth2/native-app)

Danach legt das Skript den privaten App-Ordner an beziehungsweise verwendet einen bereits markierten Ordner desselben Clients. Es lädt eine kleine eigene Testdatei hoch, liest sie bytegenau zurück und löscht sie wieder. Erst nach allen drei erfolgreichen Prüfungen entsteht das separate Konfigurationsfragment. Die laufende `server.json` wird nicht automatisch überschrieben.

## 3. Auf dem Server aktivieren

`drive-credentials.json` über den vorhandenen privaten SSH-Zugang nach `/etc/linguoai-web/drive-credentials.json` übertragen. Datei nur für root und die Dienstgruppe lesbar halten: Eigentümer `root:linguoai-web`, Dateirechte `0640`; das Konfigurationsverzeichnis ist bereits geschützt.

Vorhandene Serverkonfiguration sichern. Nur die vier Speicherfelder aus `drive-settings.json` in `/etc/linguoai-web/server.json` übernehmen; Benutzer, Sitzungsschlüssel, Worker-Schlüssel, Grenzen und öffentliche Adresse erhalten. Zuvor die Drive-Abhängigkeiten installieren und die Credentials auf dem Server nochmals prüfen.

Das Testskript kann vorhandene Freigaben ohne erneuten Browser verwenden:

```sh
cd /opt/linguoai-web
.venv/bin/python -m scripts.connect_drive --skip-oauth --credentials-file /etc/linguoai-web/drive-credentials.json --config-fragment /etc/linguoai-web/drive-settings.json
```

Für diesen rootseitigen Prüfaufruf zunächst `chmod 0600 /etc/linguoai-web/drive-credentials.json` verwenden; nach erfolgreicher Prüfung wieder Dienstgruppe und `0640` setzen. Erst dann API neu starten und einen kleinen eigenen Auftrag mit geschütztem Download prüfen.

Neue Uploads gehen nach der Aktivierung zu Drive. Der Speicherrouter hält vorhandene lokale Dateien weiter für Downloads und Aufräumen erreichbar. Drive-Fehler führen nicht zu einem stillen Wechsel des Speicherziels. Die reguläre Aufbewahrungsfrist gilt weiter; für Verarbeitung und große Transfers wird weiterhin begrenzter lokaler Zwischenspeicher benötigt.

## Aktueller Stand

Einrichtungshelfer und Speicherrouter sind implementiert und mit simulierten Google-Diensten getestet. Es gibt noch keine echte Betreiberfreigabe, keinen tatsächlich angelegten Drive-Ordner und keine aktive Drive-Serverkonfiguration. Für die Aktivierung fehlen derzeit die Google-Anmeldung und die Desktop-OAuth-Client-Datei.
