# LinguoAI 2

LinguoAI transkribiert Videos, übersetzt die erkannten Cues und erzeugt eine zeitlich
synchronisierte neue Sprachspur. Desktop-GUI, CLI und Google-Colab-Weboberfläche verwenden
dieselbe testbare Kernpipeline.

Direkte Einstiege:

- [Projektwebsite mit Download und Anleitung](https://linguoai.de/)
- [Google-Colab-Anleitung öffnen](https://linguoai.de/colab.html)
- [Quellcode-ZIP 2.2.1 herunterladen](https://linguoai.de/downloads/LinguoAI-2.2.1-source.zip)
- [Änderungen in Version 2.2.1](CHANGELOG.md)

## So funktioniert es

```text
Video
  → FFmpeg extrahiert die Tonspur
  → Faster-Whisper oder optional Parakeet erkennt Text und Zeitmarken
  → NLLB lokal oder ein gewählter Online-Dienst übersetzt die Textabschnitte
  → Edge TTS, gTTS, Piper oder Chatterbox erzeugt die gewählte Stimme
  → LinguoAI passt jeden Sprachclip an sein Zeitfenster an
  → FFmpeg erstellt das neue Video plus zwei SRT-Dateien
```

Das Quellvideo wird nicht verändert. Neben dem neu vertonten Video entstehen eine
Original-Untertiteldatei (`*.original.srt`) und eine übersetzte Untertiteldatei
(`*.translated.srt`). Bei einem Fehler werden unvollständige Ergebnisgruppen nicht als
fertige Ausgabe stehen gelassen.

## Optionale CPU-Spracherkennung mit Parakeet

Whisper bleibt die Voreinstellung. Zusätzlich kann Parakeet über `sherpa-onnx`
auf der CPU mit standardmäßig vier Threads gewählt werden:

| Profil | Geeignet für | Modell |
| --- | --- | --- |
| `multilingual-v3` | 25 europäische Quellsprachen | NVIDIA parakeet-tdt-0.6b-v3 als sherpa-onnx-Export |
| `german-primeline` | Deutsch | primeline-Fine-Tune als kompatibler sherpa-onnx-Export |

Das optionale Paket installieren und einen vollständigen lokalen Modellordner
bereitstellen. Ein Modell wird nicht automatisch heruntergeladen:

```powershell
python -m pip install -e ".[parakeet]"
python -m linguoai --input "C:\Videos\quelle.mp4" --output "C:\Videos\quelle.en.mp4" --source-language de --language en --asr-engine parakeet --parakeet-profile multilingual-v3 --parakeet-model-directory "C:\Modelle\parakeet-v3" --parakeet-threads 4
```

Der Modellordner enthält `encoder.int8.onnx`, `decoder.int8.onnx`,
`joiner.int8.onnx` und `tokens.txt` (alternativ jeweils die unquantisierten
`encoder.onnx`, `decoder.onnx`, `joiner.onnx`). Externe `.onnx.data`-Dateien müssen
beim Export bleiben. Eine `.nemo`-Datei allein funktioniert hier nicht. Das Profil
wählt die Sprachprüfung; die ONNX-Dateien müssen tatsächlich zum gewählten Modell
gehören.

Desktop und Colab bieten dieselbe Auswahl in den erweiterten Einstellungen. In
Colab vor dem Start `INSTALL_PARAKEET` aktivieren und den Modellordner dort
bereitstellen. Bei Parakeet ist die **Quellsprache erforderlich**:
Der Decoder erkennt zwar mehrsprachigen Text, liefert im verwendeten Adapter aber
keine verlässlichen Sprachmetadaten für die Übersetzungsstufe. Das Deutsch-Profil
akzeptiert Deutsch; das NVIDIA-Profil die 25 veröffentlichten Sprachen.

Untertitel verwenden Token-Anfangszeiten; Wortenden werden geschätzt. Der Abbruch
eines nativen Decoderlaufs wird nach dem aktuellen Audioabschnitt wirksam. Vor
regelmäßiger Nutzung Text und Timing mit eigenen Aufnahmen prüfen. Automatisierte Tests
verwenden simulierte Decoder-Ausgaben; zusätzlich hat ein echter CPU-Server-Test
deutsches Audio mit Parakeet erkannt. Ein umfassender Geschwindigkeits- und
Qualitätsvergleich mit eigenen Aufnahmen bleibt offen.

[NVIDIA-Modellkarte](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3),
[primeline-Modellkarte](https://huggingface.co/primeline/parakeet-primeline),
[sherpa-onnx-Modelle](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/offline-transducer/nemo-transducer-models.html).
Parakeet ersetzt ausschließlich die Erkennung; Übersetzung und Sprachausgabe
laufen anschließend durch die gewählten Dienste.

## Webversion und Google Drive

Die [Webversion öffnen](https://linguoai.de/web.html) ist als eigener
Dienst eingerichtet: anmelden, Video hochladen, Zielsprache wählen und das fertige
Video mit Original- und übersetzten Untertiteln herunterladen. Eine persistente
SQLite-Warteschlange verwaltet Besitzer, Fortschritt, Abbruch und Worker-Leases.
Ein CPU-Worker verarbeitet die Aufträge; zusätzliche Rechner können als reine
Job-Worker über HTTPS angeschlossen werden.

Browser-Uploads verwenden `chunks-v1`: maximal 8 MiB je Request, bestätigte
Offsets und SHA-256 je Block. Bei vorübergehenden Verbindungsfehlern setzt die
geöffnete Oberfläche am bestätigten Stand fort; der Fortschritt zählt die bereits
übertragenen Bytes. Abbrechen beendet die Übertragung und den zugehörigen Auftrag.
Worker übertragen Ergebnisse über 8 MiB auf demselben Weg, kleinere Dateien weiterhin
per Multipart. Die Warteschlange gibt eine Eingabe erst nach vollständiger privater
Speicherung frei; Downloads erscheinen erst, wenn Video und beide SRT-Dateien
vollständig vorliegen.

Für `studio.linguoai.de` ist ein HTTP-Origin hinter dem orangefarbenen
Cloudflare-Proxy vorbereitet. Die vorhandenen Cloudflare-SSL-Einstellungen bleiben
bestehen; ein Origin-Zertifikat ist für den gewählten Weg nicht erforderlich.
Die öffentliche Domainmigration ist noch nicht bestätigt. Die genaue Konfiguration
und ihre Prüfungen stehen in [Domainmigration](deploy/DOMAIN_MIGRATION.md).

Der echte Server-Test hat Parakeet-Erkennung mit deutschem Audio bestätigt.
Die anschließende öffentliche Google-Übersetzung wurde mit HTTP 429 abgewiesen;
ein vollständig übersetztes echtes Ergebnis ist noch nicht bestätigt.
Der Übersetzungszugang wird gemäß Nutzerentscheidung später eingerichtet.
DeepL oder Gemini benötigen private Worker-Konfiguration und ihre Abhängigkeiten.

Der Dienst nutzt seit dem 15. Oktober 2026 **Google Drive als private Ablage**.
Die echte OAuth-Freigabe und der private Ordner „linguoai uploads“ sind eingerichtet;
Upload, bytegleicher Download und Löschen wurden lokal und auf dem Server geprüft.
Die öffentliche API bestätigt das aktive Drive-Backend. Die Google-App ist auf
„In Produktion“ gestellt; die danach erneuerte Betreiberfreigabe ist auf dem Server
installiert und geprüft. Die feste siebentägige Testfrist gilt damit für die neue
Freigabe nicht mehr. Die Drive-Anbindung bleibt serverseitig;
Endnutzer benötigen weder ein Google-Konto noch eine eigene Drive-Freigabe.

Aktuelle Grenzen: 100 MiB je Eingabe, bis 1 GiB je Ergebnis, zwei aktive Aufträge
je Benutzer und 20 insgesamt. Ergebnisse werden 24 Stunden nach Abschluss
entfernt. Parakeet v3 läuft auf dem CPU-Worker mit vier Threads und 30-Sekunden-
Audioabschnitten; Faster-Whisper bleibt wählbar. Die Weboberfläche berücksichtigt
die tatsächlich verfügbaren Worker-Engines und Anbieter. Die Dateigrenzen gelten
für die gesamte Datei; 8-MiB-Blöcke ändern diese Grenzen nicht. Der Reverse Proxy
behält `1025m` für kompatible ältere Worker-Multipart-Uploads bei. Große Transfers
über die öffentliche Cloudflare-Adresse verwenden die kleinen Blöcke.

[Betriebsübersicht](website/REMOTE_WORKER.md) und
[Installation, private Konfiguration und Remote-Worker](deploy/README.md) beschreiben
den eingerichteten Dienst. Colab bleibt als separate sitzungsgebundene Weboberfläche
verfügbar.

## Wichtigste Verbesserungen

- Colab startet mit einer einzigen Installations-/Startzelle und einem dynamischen,
  passwortgeschützten Gradio-Link. Ein benannter Cloudflare Tunnel bleibt optional.
- Mehrere Uploads oder mehrere Drive-Dateipfade (einer pro Zeile) lassen sich als Batch verarbeiten.
- Kontextbewusste Übersetzung, adaptive Zeitfenster und begrenzte Wiederholungsversuche
  erhalten die Verbesserungen des zuvor veröffentlichten 2.2.0-Quellcodes.
- Faster-Whisper arbeitet mit überlappenden 16-kHz-PCM-Chunks und echten Zeit-Offsets.
- Segmente bleiben cue-stabil; Text wird nicht proportional umverteilt oder durch `...` ersetzt.
- Nur der gewählte Übersetzer wird kontaktiert. Fallbacks sind ausdrückliches Opt-in.
- Als lokale Übersetzung steht NLLB-200 auf CPU oder CUDA zur Verfügung.
- DeepL verwendet das offizielle SDK; Gemini verwendet `google-genai` mit strukturiertem JSON.
- Die TTS-Engine ist pro Job wählbar: Edge TTS oder gTTS online, Piper mit lokalem
  ONNX-Modell oder Chatterbox Multilingual V2 mit einer autorisierten Referenzstimme.
- Edge-TTS-Aufrufe besitzen native Timeouts, begrenzte Parallelität und Retry-Regeln;
  lokale Engines und Modelle werden nur geladen, wenn sie ausgewählt sind.
- Jeder TTS-Clip wird nach 48-kHz-Mono-PCM normalisiert und samplegenau eingeordnet.
- Das Ausgabevideo reicht exakt bis zum Medienende; lange Clips verschieben keine Folgesegmente.
- Video und beide SRT-Dateien werden als zusammengehörige, rollback-fähige Ergebnismenge
  veröffentlicht. Gleichzeitige Jobs können dasselbe Ziel nicht überschreiben.
- GUI-Worker greifen nie direkt auf Tk zu; Abbruch beendet auch laufende FFmpeg-Prozesse.
- Die Desktop-GUI besitzt einen kompakten Drei-Tab-Aufbau, Hell-/Dunkelmodus, Systemcheck,
  anbieterabhängige Felder, eigene Sprach-/Modellwerte und direkt öffnende Ergebnisbuttons.
- API-Schlüssel werden aus Logs redigiert und nicht an FFmpeg-Unterprozesse vererbt.

## Voraussetzungen

- Python 3.10 oder neuer
- `ffmpeg` und `ffprobe` im `PATH`
- Internet für Edge TTS, gTTS, die Cloud-Übersetzer und erstmalige Modell-Downloads
- Optional CUDA für Whisper, NLLB, Piper und/oder Chatterbox

## Lokal ausführen: Windows-Schnellstart (CPU)

Die folgenden Befehle sind für PowerShell gedacht. Zuerst FFmpeg installieren:

```powershell
winget install --id Gyan.FFmpeg --exact --accept-package-agreements --accept-source-agreements
```

Danach **eine neue PowerShell** öffnen und das Projekt installieren:

```powershell
cd "C:\Pfad\zu\linguoai 2"

py -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
python -m pip install torch --index-url https://download.pytorch.org/whl/cpu
python -m pip install -e ".[desktop]"

ffmpeg -version
ffprobe -version
python -m linguoai
```

Dieser Schnellstart installiert bewusst die robuste CPU-Ausgabe von PyTorch. Für eine
NVIDIA-GPU stattdessen zuerst den passenden PyTorch-Befehl aus dem Abschnitt
[CPU und NVIDIA-GPU](#cpu-und-nvidia-gpu) verwenden und danach
`python -m pip install -e ".[desktop]"` ausführen.

`python -m linguoai` ohne weitere Argumente öffnet die grafische Oberfläche. Alternativ
kann nach der Installation `linguoai-gui` gestartet werden. Falls PowerShell die Aktivierung
blockiert, gilt nur für dieses Terminal:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
.\.venv\Scripts\Activate.ps1
```

Ohne Aktivierung funktioniert auch der direkte Aufruf:

```powershell
.\.venv\Scripts\python.exe -m linguoai
```

### Erster lokaler Lauf in der GUI

1. Unter **Start** mit `Datei …` ein Video wählen. LinguoAI schlägt die Ausgabe vor.
2. Ziel- und optional Quellsprache festlegen.
3. `NLLB lokal · CPU/GPU` als Übersetzungsart wählen.
4. Als TTS-Engine zunächst `Edge TTS · online` verwenden. Eine eigene Edge-Stimmen-ID
   kann direkt eingetragen werden; Piper und Chatterbox werden weiter unten eingerichtet.
5. Auf einem normalen CPU-PC `small`, `Nur CPU`, NLLB `Nur CPU` und Batchgröße `2`
   verwenden. Die NLLB-Einstellungen liegen unter **Erweitert**.
6. API-Schlüssel leer lassen und **Verarbeitung starten** anklicken.

Beim ersten Lauf werden das Whisper-Modell und für NLLB standardmäßig ein Modell von
etwa 2,5 GB geladen. Deshalb kann der erste Start deutlich länger dauern. Mehrere GB freier
Speicher sollten vorhanden sein. Die Caches liegen standardmäßig im Benutzerprofil unter
`.cache`; mit der Umgebungsvariable `HF_HOME` kann ein anderer Hugging-Face-Cache gewählt
werden.

Wichtig: **NLLB übersetzt lokal**, die voreingestellte Edge-TTS-Sprachausgabe ist jedoch ein
Microsoft-Onlinedienst. Die übersetzten Textabschnitte werden dafür an Edge TTS gesendet.
Mit NLLB plus Piper oder Chatterbox arbeitet die eigentliche Verarbeitung nach dem einmaligen
Download vollständig lokal.

Für jeden weiteren Start genügen:

```powershell
cd "C:\Pfad\zu\linguoai 2"
.\.venv\Scripts\Activate.ps1
python -m linguoai
```

## Weitere Installationsvarianten

Desktop mit lokaler Übersetzung und optional Gemini:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[desktop,gemini]"
ffmpeg -version
python -m linguoai
```

Schlankere Varianten:

```powershell
python -m pip install -e .             # CLI, Google, DeepL, Whisper, Edge TTS und gTTS
python -m pip install -e ".[gui]"      # zusätzlich Desktop-GUI
python -m pip install -e ".[local]"    # zusätzlich NLLB/PyTorch
python -m pip install -e ".[desktop]"  # GUI plus lokale NLLB-Übersetzung
python -m pip install -e ".[gemini]"   # zusätzlich Gemini
python -m pip install -e ".[piper]"    # lokale Piper-TTS-Engine
python -m pip install -e ".[voice-clone]" # Chatterbox Multilingual mit Referenzstimme
python -m pip install -e ".[all]"      # Desktop plus Colab/Gradio und alle Provider
```

Die bewusst getrennten Extras `piper` und `voice-clone` gehören nicht zu `all`: Beide bringen
eigene, teils große Laufzeitabhängigkeiten mit. Mehrere Extras lassen sich kombinieren, etwa
`python -m pip install -e ".[desktop,piper]"`. Für Chatterbox empfiehlt sich wegen seiner fest
abgestimmten Torch-/Audio-Abhängigkeiten eine eigene Python-3.11-Umgebung.

## Sprachausgabe wählen: Edge, gTTS, Piper oder Chatterbox

| Engine | Eigene Stimme | Netzwerk bei der Synthese | CPU/GPU | Benötigt |
| --- | --- | --- | --- | --- |
| Edge TTS | Eine vorhandene Edge-Stimmen-ID, keine Stimmkopie | Ja | Cloud-Dienst; lokale GPU ohne Bedeutung | Internet |
| gTTS | Sprachcode über Google Translate TTS, keine Stimmkopie | Ja | Cloud-Dienst; lokale GPU ohne Bedeutung | Internet |
| Piper | Ja, als bereits trainiertes Piper-ONNX-Modell | Nein | CPU; optional NVIDIA CUDA | `.onnx` plus `.onnx.json` |
| Chatterbox Multilingual V2 | Ja, echte Zero-Shot-Stimmkopie aus einer Aufnahme | Nein, nachdem das Modell geladen wurde | CPU möglich, NVIDIA-GPU empfohlen | Referenzaudio, Rechtebestätigung und Modell-Download |

Die vier Optionen meinen unterschiedliche Dinge. Bei Edge wählt `--tts-voice` lediglich
eine Stimme aus dem Microsoft-Katalog. gTTS nutzt Google Translate Text-to-Speech und akzeptiert
optional einen Sprachcode über `--tts-voice`, zum Beispiel `de`, `en` oder `fr`; echte
Stimmklone sind damit nicht möglich. Piper verwendet die Stimme, die bereits in einem
ONNX-Modell trainiert wurde; LinguoAI trainiert dieses Modell nicht selbst. Chatterbox leitet
die Stimme dagegen direkt aus einer Referenzaufnahme ab. Wähle nur eine Engine pro Job.

In der Desktop-GUI liegt die Engine-Auswahl unter **Start → 03 Erkennung und Stimme**. Bei
Edge erscheint dort die Stimmen-ID, bei gTTS der optionale Sprachcode. Die engineabhängigen Felder liegen unter
**Erweitert → Lokale Stimme und TTS-Engine** und werden passend ein- oder ausgeblendet:
Piper zeigt Modell, optionale JSON, Sprecher-ID und Längenfaktor; Chatterbox zeigt
Referenzaufnahme, Modellgeneration, Ausdruck, CFG-Stärke und Rechtebestätigung. Der
Systemcheck weist auf fehlende optionale Pakete oder Dateien hin.

### Edge TTS: unkompliziert und online

Ohne weitere Option nutzt LinguoAI die zur Zielsprache hinterlegte Standardstimme. Mit einer
vollständigen Edge-Stimmen-ID wird sie überschrieben:

```powershell
python -m linguoai -i quelle.mp4 -o ziel.mp4 -l de `
  --tts-engine edge `
  --tts-voice de-DE-KatjaNeural
```

Die ID ist kein lokales Modell und auch keine eigene Stimmaufnahme. Die Cue-Texte verlassen
den Rechner. Verfügbare Namen lassen sich mit `edge-tts --list-voices` anzeigen; der
zugrunde liegende [Edge-TTS-Client](https://github.com/rany2/edge-tts) verwendet Microsofts
Edge-Read-Aloud-Dienst.

### gTTS: Google-Voice schnell testen

gTTS ist eine schlanke Online-Option über Google Translate Text-to-Speech. Ohne `--tts-voice`
nutzt LinguoAI automatisch die Zielsprache; mit `--tts-voice` kann ein gTTS-Sprachcode
erzwungen werden:

```powershell
python -m linguoai `
  --input "C:\Videos\demo.mp4" `
  --output "C:\Videos\demo-de.mp4" `
  --language de `
  --tts-engine gtts `
  --tts-voice de
```

Installation einzeln:

```powershell
python -m pip install gTTS
```

gTTS ist laut PyPI eine Python-Bibliothek und ein CLI-Tool für Google Translate
Text-to-Speech. Es ist nicht Google Cloud TTS und kann sich ändern, wenn Google die
zugrunde liegende Schnittstelle verändert.

### Piper: lokale Stimme aus einem ONNX-Modell

Piper eignet sich für eine schnelle lokale Ausgabe und läuft gut auf der CPU. Installiere die
Engine zusätzlich:

```powershell
python -m pip install -e ".[piper]"
```

Eine vorhandene deutsche Beispielstimme lässt sich etwa so in einen eigenen Ordner laden:

```powershell
New-Item -ItemType Directory -Force ".\Stimmen"
python -m piper.download_voices --download-dir ".\Stimmen" de_DE-thorsten-medium
```

Eine Stimme besteht immer aus zwei zusammengehörigen Dateien, zum Beispiel
`de_DE-meine-stimme-medium.onnx` und `de_DE-meine-stimme-medium.onnx.json`. Liegt die JSON-
Datei direkt daneben und folgt dieser Benennung, ist `--piper-config` optional. Ein fremdes
JSON oder ein Modell einer anderen Stimme ist nicht austauschbar. Die offizielle
[Piper-Stimmenübersicht](https://github.com/OHF-Voice/piper1-gpl/blob/main/docs/VOICES.md)
verweist auch auf die jeweilige Modellkarte; dort muss die **separate Lizenz der Stimme**
geprüft werden. Ein eigenes trainiertes Modell kann genauso gewählt werden. Hinweise zum
Training stehen in der [offiziellen Piper-Trainingsanleitung](https://github.com/OHF-Voice/piper1-gpl/blob/main/docs/TRAINING.md).

```powershell
python -m linguoai -i quelle.mp4 -o ziel.mp4 -l de -s nllb `
  --tts-engine piper `
  --tts-device cpu `
  --piper-model "C:\Modelle\meine-stimme.onnx" `
  --piper-config "C:\Modelle\meine-stimme.onnx.json" `
  --piper-speaker-id 0 `
  --piper-length-scale 1.0
```

`--piper-speaker-id` ist nur für Mehrsprecher-Modelle nötig. Ein höherer
`--piper-length-scale` spricht langsamer, ein kleinerer schneller. Die Python-Integration folgt
der [offiziellen Piper-API](https://github.com/OHF-Voice/piper1-gpl/blob/main/docs/API_PYTHON.md).

Für CUDA muss der `CUDAExecutionProvider` von ONNX Runtime verfügbar sein. Installiere
**nicht gleichzeitig** `onnxruntime` und `onnxruntime-gpu`; nach der Piper-Installation ist
typischerweise dieser Austausch nötig:

```powershell
python -m pip uninstall -y onnxruntime onnxruntime-gpu
python -m pip install onnxruntime-gpu
python -c "import onnxruntime as ort; print(ort.get_available_providers())"
```

Erst wenn `CUDAExecutionProvider` ausgegeben wird, `--tts-device cuda` verwenden. `auto`
nimmt den Provider, wenn er verfügbar ist, sonst die CPU. Eine installierte PyTorch-CUDA-
Version allein beschleunigt Piper nicht. Die aktuelle Piper-Engine steht unter
[GPL-3.0-or-later](https://github.com/OHF-Voice/piper1-gpl/blob/main/COPYING); diese Lizenz
ersetzt nicht die separate Lizenz des verwendeten Stimmenmodells.

### Chatterbox Multilingual V2: Stimme aus Referenzaufnahme

Chatterbox ist die Option für eine echte Referenzstimme. Verwende vorzugsweise eine eigene
Aufnahme mit **6–10 Sekunden sauberer Sprache**, genau einer Person, wenig Raumhall und ohne
Musik oder Hintergrundgeräusche. Du musst bestätigen, dass du die Stimme verwenden darfst.
Die Generierung ist probabilistisch: Ergebnis, Aussprache und Stimmähnlichkeit sollten vor
einer Veröffentlichung angehört und geprüft werden.

Für die derzeit am besten getestete Kombination wird Python 3.11 empfohlen:

```powershell
py -3.11 -m venv .venv-chatterbox
.\.venv-chatterbox\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[voice-clone]"
```

Für die grafische Oberfläche kann stattdessen
`python -m pip install -e ".[gui,voice-clone]"` verwendet werden. Beim ersten Einsatz lädt
V2 ungefähr 3,7 GB Modellbestandteile in den lokalen Cache. Der genaue Platzbedarf kann sich
mit Modellrevision und bereits vorhandenen Dateien ändern. CPU-Betrieb funktioniert, ist aber
sehr langsam; für längere Videos ist eine NVIDIA-GPU mit passender PyTorch-CUDA-Installation
deutlich sinnvoller. Native Windows-Ausführung ist wegen der ML-Audio-Abhängigkeiten
experimentell; Colab oder Linux ist bei Installationsproblemen die verlässlichere Umgebung.
Vor dem Laden von Chatterbox gibt LinguoAI ein geladenes Whisper- und gegebenenfalls
NLLB-Modell frei, damit sich die großen Modelle nicht gleichzeitig GPU- oder RAM-Speicher
teilen. Bei einem Videoordner müssen diese Modelle deshalb für das nächste Video neu geladen
werden; das ist langsamer, verhindert aber typische Speicherfehler auf kleineren GPUs.

```powershell
python -m linguoai -i quelle.mp4 -o ziel.mp4 -l de -s nllb `
  --tts-engine chatterbox `
  --tts-device auto `
  --tts-reference-audio "C:\Audio\meine-stimme.wav" `
  --confirm-voice-rights `
  --chatterbox-model v2 `
  --chatterbox-exaggeration 0.5 `
  --chatterbox-cfg-weight 0.5
```

Unterstützte Sprachcodes sind `ar`, `da`, `de`, `el`, `en`, `es`, `fi`, `fr`, `he`, `hi`,
`it`, `ja`, `ko`, `ms`, `nl`, `no`, `pl`, `pt`, `ru`, `sv`, `sw`, `tr` und `zh`. LinguoAI
reicht `zh-CN` dabei als Chatterbox-Code `zh` weiter. Regionale Varianten werden sonst auf
ihren Basissprachcode reduziert. LinguoAI pinnt bewusst die
[veröffentlichte PyPI-Version](https://pypi.org/project/chatterbox-tts/0.1.7/)
`chatterbox-tts==0.1.7` mit Multilingual V2; die neuere V3-Schnittstelle im Repository ist
noch nicht Bestandteil dieses Wheels. `exaggeration` steuert den Ausdruck, `cfg-weight` die
Bindung an
Stimme und Sprechweise; die Standardwerte `0.5` sind ein vernünftiger Ausgangspunkt.

Alle Chatterbox-Ausgaben enthalten ein eingebettetes **PerTh-Wasserzeichen**. Modell,
Sprachliste, API-Beispiele und Wasserzeichen sind im offiziellen
[Chatterbox-Repository](https://github.com/resemble-ai/chatterbox) und auf der
[Modellkarte](https://huggingface.co/ResembleAI/chatterbox) dokumentiert. Das Projekt steht
unter der [MIT-Lizenz](https://github.com/resemble-ai/chatterbox/blob/master/LICENSE); das
Recht an einer realen Referenzstimme wird dadurch nicht erteilt.

## CPU und NVIDIA-GPU

`auto` versucht eine verfügbare NVIDIA-CUDA-GPU und fällt sonst auf die CPU zurück.
AMD- und Intel-GPUs werden von dieser Konfiguration derzeit nicht als CUDA-Gerät verwendet.
Whisper, NLLB und TTS können getrennt platziert werden; so kann beispielsweise Whisper auf
der CPU, NLLB auf der GPU und Piper wieder auf der CPU laufen. Dafür dienen `--device`,
`--nllb-device` und `--tts-device`.

Für eine NVIDIA-GPU sollte PyTorch mit dem zum System passenden Befehl aus dem offiziellen
[PyTorch-Installationsselektor](https://pytorch.org/get-started/locally/) installiert werden.
Danach prüft dieser Befehl NLLB/PyTorch:

```powershell
python -c "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available())"
```

Faster-Whisper verwendet CTranslate2 und benötigt für GPU-Betrieb zusätzlich die passenden
CUDA-12-cuBLAS- und cuDNN-9-Laufzeitbibliotheken im `PATH`; eine funktionierende
PyTorch-CUDA-Installation allein reicht dafür nicht. Details stehen bei den
[Faster-Whisper-GPU-Anforderungen](https://github.com/SYSTRAN/faster-whisper#gpu).

Eine robuste erste GPU-Konfiguration ist deshalb: Whisper `Nur CPU`/`int8`, NLLB
`NVIDIA-GPU (CUDA)`. Erst nach erfolgreicher Einrichtung von Faster-Whisper sollte auch
Whisper auf `NVIDIA-GPU (CUDA)`/`float16` gestellt werden.

## Lokale NLLB-Übersetzung

Der Standard ist `facebook/nllb-200-distilled-600M` (etwa 2,5 GB). `auto` verwendet eine
verfügbare GPU und sonst die CPU. Whisper und NLLB lassen sich getrennt platzieren:

```powershell
python -m linguoai `
  -i "C:\Videos\quelle.mp4" `
  -o "C:\Videos\quelle.de.mp4" `
  -l de -s nllb `
  --device cuda `
  --nllb-device cpu
```

Ein eigenes oder fine-getuntes, NLLB-kompatibles Modell kann direkt geladen werden:

```powershell
python -m linguoai -i quelle.mp4 -o ziel.mp4 -l de -s nllb `
  --nllb-model "C:\Modelle\mein-nllb" `
  --nllb-revision "mein-getesteter-tag"
```

Für weitere NLLB-Sprachen können FLORES-200-Codes explizit gesetzt werden, zum Beispiel
`--language ace --nllb-source-code deu_Latn --nllb-target-code ace_Latn`. Der BCP-47-Code
bei `--language` bestimmt Dateiname und Metadaten; der FLORES-Code steuert das Modell. Eine
passende TTS-Ausgabe muss unabhängig davon verfügbar sein: bei Edge gegebenenfalls mit
`--tts-voice`, bei Piper als sprachlich passendes ONNX-Modell. Chatterbox akzeptiert nur die
oben aufgeführten 23 Basissprachcodes. Der FLORES-Override erweitert ausschließlich die
Übersetzungszuordnung; die Ausgangssprache muss weiterhin von Whisper transkribierbar sein.

Das NLLB-Standardmodell steht unter **CC-BY-NC-4.0**, ist laut Modellkarte ein
Forschungsmodell und nicht für Produktion oder zertifizierte Übersetzungen freigegeben.
NLLB kann ein kompatibles fine-getuntes Modell laden, führt aber selbst kein Training durch.
Ob auch die Sprachausgabe lokal bleibt, hängt von der TTS-Engine ab: Edge und gTTS sind online,
Piper und Chatterbox synthetisieren nach dem Modell-Download lokal.

## Google Colab mit Weboberfläche

Der einfachste Browserstart beginnt mit der
[Google-Colab-Anleitung auf linguoai.de](https://linguoai.de/colab.html).
Das Notebook liegt außerdem unter
[`notebooks/LinguoAI_Colab.ipynb`](notebooks/LinguoAI_Colab.ipynb) und kann separat
[heruntergeladen werden](https://linguoai.de/downloads/LinguoAI_Colab.ipynb).

1. Das [Colab-Notebook](https://linguoai.de/downloads/LinguoAI_Colab.ipynb) herunterladen.
2. [Google Colab](https://colab.research.google.com/) öffnen, anmelden und das Notebook hochladen.
3. Optional unter **Laufzeit → Laufzeittyp ändern** eine T4-GPU auswählen.
4. In der einzigen Codezelle die voreingestellte Projektquelle `download_zip` und
   `SHARE_PROVIDER = "gradio"` unverändert lassen. Das Notebook
   lädt das geprüfte Quellcode-ZIP automatisch von `https://linguoai.de/`.
5. Google Drive nur einbinden, wenn Dateien daraus gelesen, Modelle dort gecacht oder
   Ergebnisse dort gespeichert werden sollen.
6. Für den ersten Test `LOCAL_TTS_EXTRA = "none"` lassen. Edge TTS und gTTS funktionieren
   damit direkt; für Piper oder Chatterbox das entsprechende Zusatzpaket auswählen.
7. **Laufzeit → Alle ausführen** wählen und die Installation abwarten.
8. Den angezeigten Gradio-Link öffnen und mit Benutzername und Zufallspasswort anmelden.
9. Ein oder mehrere Videos hochladen oder relative `MyDrive`-Dateipfade (einen pro Zeile)
   eintragen, den Auftrag starten und die Ergebnisdateien herunterladen oder nach Drive kopieren.

Alternativ kann in der Startzelle `PROJECT_SOURCE = "upload_zip"` gewählt und das
[Quellcode-ZIP](https://linguoai.de/downloads/LinguoAI-2.2.1-source.zip) ungeöffnet
hochgeladen werden. Eine eigene installierbare Git-Quelle kann über `PROJECT_SOURCE = "git"`
und `GIT_URL` verwendet werden. Beide Wege sind für eigene oder zuvor geänderte Projektfassungen
nützlich.

In der Weboberfläche kann ein Video direkt im Browser hochgeladen werden. Alternativ wird
ein Pfad relativ zu `MyDrive` angegeben, etwa `Videos/demo.mp4`. Arbeitsdateien werden zuerst
nach `/content` kopiert; Ergebnisse stehen als Browser-Download bereit und können zusätzlich
in einen relativen Drive-Ordner geschrieben werden. Drive-Pfade dürfen `MyDrive` nicht
verlassen, und das Drive-Verzeichnis wird nicht durch Gradio als Downloadpfad freigegeben.

Edge TTS und gTTS funktionieren mit `LOCAL_TTS_EXTRA = "none"` direkt und bleiben online. Für Piper können
`.onnx` und `.onnx.json` jeweils hochgeladen oder als sichere, relative `MyDrive`-Pfade
angegeben werden. Für Chatterbox gilt dasselbe für die Referenzaufnahme; die
Rechtebestätigung bleibt verpflichtend. Eine UI-Auswahl installiert keine fehlende Engine
nachträglich: Nach einer Änderung von `LOCAL_TTS_EXTRA` muss die Installationszelle erneut
ausgeführt werden. Eigene BCP-47-Zielcodes können direkt in das Zielfeld geschrieben und mit
einem FLORES-Code sowie einer zur gewählten Engine passenden Stimme kombiniert werden.

Ein Abbrechen-Knopf stoppt FFmpeg, TTS und NLLB an sicheren Prüfpunkten; ein gerade laufender
Modell-Download, einzelner Whisper-Decode oder einzelne Chatterbox-Generierung kann erst nach
Rückkehr aus der jeweiligen Bibliothek enden.

### Gradio-Link als Standard; Cloudflare optional

Der Standard benötigt weder eine eigene Domain noch ein Cloudflare-Konto oder einen
Tunnel-Token. Gradio erzeugt beim Start eine zeitlich begrenzte öffentliche Adresse;
Benutzername und Zufallspasswort werden nur in der aktiven Notebook-Ausgabe angezeigt.
Der Link funktioniert nur bei laufender Colab-Sitzung. Standard-Uploadgrenze: 5 GB je Datei.

Für eine eigene feste Adresse in der Startzelle `SHARE_PROVIDER = "cloudflare"` wählen
und `CLOUDFLARE_TUNNEL_HOSTNAME` auf den eigenen eingerichteten Hostnamen setzen.
Im Cloudflare-Dashboard wird dafür ein **Published application**-Eintrag mit dem Service
`http://127.0.0.1:7860` angelegt. Das Notebook installiert `cloudflared` nur in diesem Modus
und startet es mit dem Colab Secret `CLOUDFLARE_TUNNEL_TOKEN`.
`app.linguoai.de` ist lediglich ein Beispiel und kein bereitgestellter Anwendungsserver.
Der Start wartet auf die Tunnel-Verbindung; die Hostname-Route muss der Betreiber zusätzlich prüfen.

Der Token ist ein Zugang zum Tunnel: Er gehört ausschließlich in Colab Secrets und darf nicht in
Git, ins Notebook, in Logs oder in eine URL gelangen. Die Gradio-App bindet nur an
`127.0.0.1:7860`; im Cloudflare-Modus stellt der Tunnel die einzige öffentliche Verbindung her.
Die Adresse bleibt gleich, ist aber nur erreichbar, solange die jeweilige Colab-Sitzung aktiv ist.
Die Cloudflare-Uploadgrenze ist vorsorglich auf 95 MB je Datei gesetzt; größere Videos über
Google Drive einlesen. Quick Tunnels (`trycloudflare.com`) werden nicht verwendet, weil sie
die für Gradio nötigen Server-Sent Events nicht unterstützen.

Browser-Ausgaben und Gradio-Cache werden nach spätestens sechs Stunden bereinigt. Ein
Drive-Ergebnis wird zunächst in einen versteckten temporären Ordner kopiert und erst nach
allen drei Dateien als fertiger Ordner sichtbar gemacht.

Die erste NLLB- oder Chatterbox-Ausführung lädt das jeweilige Modell. Der Notebook-Cache kann
in Drive gespeichert werden. Eine Colab-VM und ihr `/content`-Speicher sind flüchtig;
GPU-Verfügbarkeit, GPU-Typ und Sitzungsdauer sind nicht garantiert. Piper und Chatterbox
können auf der CPU laufen, Chatterbox ist dort für längere Videos jedoch sehr langsam. Das
Notebook erleichtert Einrichtung und Dateiauswahl, garantiert aber weder eine bestimmte
Laufzeit noch, dass eine kostenlose Colab-Sitzung einen großen Job beendet.

## Cloud-Provider

Google Translate ohne API-Key:

```powershell
python -m linguoai -i quelle.mp4 -o ziel.mp4 -l de -s google
```

DeepL:

```powershell
$env:DEEPL_API_KEY = "dein-key"
python -m linguoai -i quelle.mp4 -o ziel.mp4 -l de -s deepl
```

Gemini 3.5 Flash mit optionaler ASR-Korrektur:

```powershell
$env:GEMINI_API_KEY = "dein-key"
python -m linguoai -i quelle.mp4 -o ziel.mp4 -l de -s gemini --optimize
```

Ein zweiter Anbieter wird nur explizit als Fallback verwendet:

```powershell
$env:GEMINI_API_KEY = "dein-key"
python -m linguoai -i quelle.mp4 -o ziel.mp4 -l de `
  -s nllb --fallback-service gemini
```

Wichtige Optionen:

```text
--source-language en          Quellsprache vorgeben statt erkennen
--whisper-model small         Schnelleres/kleineres ASR-Modell
--device auto|cpu|cuda        Whisper-Gerät
--compute-type default        z. B. int8 auf CPU oder float16 auf GPU
--nllb-device auto|cpu|cuda   separates lokales Übersetzungsgerät
--nllb-batch-size 8           bei Speichermangel reduzieren
--tts-engine edge|gtts|piper|chatterbox
--tts-device auto|cpu|cuda    separates TTS-Gerät für lokale Engines
--tts-voice ID                Edge-Stimmen-ID oder gTTS-Sprachcode
--piper-model PFAD            lokales Piper-Modell (.onnx)
--piper-config PFAD           zugehörige .onnx.json; bei Standardname optional
--piper-speaker-id 0          nur für Mehrsprecher-Modelle
--piper-length-scale 1.0      Piper-Sprechdauer; größer ist langsamer
--tts-reference-audio PFAD    Referenzaufnahme für Chatterbox
--confirm-voice-rights        erforderliche Rechtebestätigung
--chatterbox-model v2         veröffentlichte Multilingual-Version
--chatterbox-exaggeration 0.5 Ausdrucksstärke von 0 bis 2
--chatterbox-cfg-weight 0.5   Stimm-/Stilbindung von 0 bis 1
--max-tts-speed 2.0           höchste Tempoanpassung
--overwrite                   bestehende Video-/SRT-Gruppe ersetzen
```

Zu jedem Video entstehen außerdem `<name>.original.srt` und `<name>.translated.srt`.
Bei Ordnerverarbeitung lautet der Exitcode `1`, wenn mindestens eine Datei fehlschlug,
`2` bei erwartbaren Konfigurations-/Verarbeitungsfehlern und `130` bei Abbruch.

## Datenschutz und Dienstgrenzen

- NLLB verarbeitet Cues in der lokalen Laufzeit.
- DeepL, Gemini oder Google erhalten Cues nur, wenn sie ausgewählt oder als Fallback gesetzt
  wurden. Die Gemini-Textoptimierung ist ein separates Opt-in.
- Edge TTS sendet jeden übersetzten Cue an Microsofts Online-Sprachdienst. `edge-tts` nutzt
  die Edge-Read-Aloud-Schnittstelle und ist keine garantierte, vertraglich stabile API.
- gTTS sendet jeden übersetzten Cue an Google Translate Text-to-Speech. `gTTS` ist keine
  Google-Cloud-API und benötigt Internet.
- Piper verarbeitet Text und Stimme lokal. Das gewählte ONNX-Modell wird weder hochgeladen
  noch von LinguoAI trainiert; Modellherkunft und Lizenz bleiben trotzdem zu prüfen.
- Chatterbox verarbeitet Cue-Texte und Referenzaufnahme nach dem Download innerhalb der
  gewählten Laufzeit – lokaler PC oder Colab-VM. Die Aufnahme wird nicht an einen LinguoAI-
  Dienst gesendet. Sie enthält jedoch biometrisch sensible
  Stimmmerkmale: nur mit Einwilligung verwenden, sicher speichern und nicht unnötig teilen.
  Die Pflichtbestätigung ist eine technische Schutzschranke, kein Ersatz für Einwilligung,
  Urheber-, Persönlichkeits- oder sonstige anwendbare Rechte. Ausgaben tragen das
  PerTh-Wasserzeichen.
- Der Google-Adapter von `deep-translator` greift auf den öffentlichen Google-Translate-
  Webdienst zu und kann Rate-Limits oder Änderungen unterliegen.
- GUI-Schlüssel gelten nur für die Sitzung. Die CLI liest `DEEPL_API_KEY` und
  `GEMINI_API_KEY` aus der Umgebung statt aus Kommandozeilenargumenten. Eine direkt
  eingetippte PowerShell-Zuweisung kann dennoch in der Shell-Historie landen; auf gemeinsam
  genutzten Rechnern sollte dafür ein sicherer Secret-Store verwendet werden.

## Tests und Entwicklung

Die normale Suite lädt kein ML-Modell und benötigt kein Netzwerk:

```powershell
python -m pip install -e ".[dev]"
python -m unittest discover -v
pytest
ruff check .
ruff format --check .
python -m compileall -q linguoai tests main.py
```

Abgedeckt sind unter anderem Config-Normalisierung, Provider- und TTS-Engine-Auswahl,
Rechtebestätigung für Referenzstimmen, Secret-Redaction, Entity-Kollisionen, FLORES-Mapping,
Colab-Pfadgrenzen, Notebook-Syntax, SRT-Rundung, Chunk-Grenzen, exakte Audio-Endlänge,
Output-Reservierung und Transaktions-Rollback.

## Projektstruktur

```text
linguoai/
  cli.py               CLI und Exitcodes
  gui.py               Tk/ttkbootstrap; nur Mainthread-Zugriffe
  colab_ui.py          passwortgeschützte Gradio-Oberfläche
  config.py            unveränderliche Job-Konfiguration
  media.py             FFmpeg, PCM-Normalisierung und Zeitachse
  transcription.py     Faster-Whisper-Adapter
  translation.py       Google-, DeepL-, Gemini- und Manager-Adapter
  local_translation.py lokaler NLLB-Adapter
  speech.py            Edge-, gTTS-, Piper- und Chatterbox-Adapter
  pipeline.py          UI-unabhängige Orchestrierung
notebooks/
  LinguoAI_Colab.ipynb
```

Weiterführend: [NLLB-Dokumentation](https://huggingface.co/docs/transformers/model_doc/nllb),
[NLLB-Modellkarte](https://huggingface.co/facebook/nllb-200-distilled-600M),
[Faster-Whisper](https://github.com/SYSTRAN/faster-whisper),
[DeepL Python SDK](https://github.com/DeepLcom/deepl-python) und
[Google Gen AI SDK](https://googleapis.github.io/python-genai/),
[gTTS](https://pypi.org/project/gTTS/),
[Piper](https://github.com/OHF-Voice/piper1-gpl) sowie
[Chatterbox](https://github.com/resemble-ai/chatterbox).
