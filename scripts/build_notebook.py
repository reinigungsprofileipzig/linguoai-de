"""Erzeuge das selbstständige Colab-Notebook aus einer testbaren Python-Startzelle."""

from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INTRO = """# LinguoAI 2.2.1 für Google Colab

Eine Zelle erledigt Installation, Importprüfung und Start. Wähle **Laufzeit → Alle ausführen**.
Standardmäßig lädt sie das Projekt von linguoai.de und zeigt einen zufälligen, passwortgeschützten
Gradio-Link. Eine eigene Domain oder ein Tunnel-Token ist dafür nicht erforderlich.

Mehrere Videos kannst du nacheinander verarbeiten. Google Drive ist optional; bei Bedarf
MOUNT_GOOGLE_DRIVE aktivieren. LOCAL_TTS_EXTRA = "none" genügt für Edge TTS und gTTS.
Piper und Chatterbox brauchen das entsprechende Zusatzpaket. Die Standardeinstellung arbeitet
ohne automatischen Übersetzerwechsel; ein Google-Fallback muss ausdrücklich aktiviert werden.
Optional vor dem Start unter Laufzeit → Laufzeittyp ändern eine GPU auswählen.

## Optional: Parakeet auf der CPU

INSTALL_PARAKEET aktivieren, um sherpa-onnx und NumPy mitzuinstallieren. Das Modell wird
nicht automatisch geladen: einen vollständigen sherpa-onnx-Parakeet-Modellordner unter
/content oder im eingebundenen Drive bereitstellen und in der Oberfläche auswählen.
NVIDIA v3 unterstützt 25 europäische Sprachen; das primeline-Profil ist für Deutsch gedacht.
Bei Parakeet die Quellsprache ausdrücklich wählen. Whisper bleibt die Voreinstellung.

## Optional: benannter Cloudflare Tunnel

Nur mit SHARE_PROVIDER = "cloudflare": eigenen Cloudflare-Tunnel einrichten und eine öffentliche
HTTPS-Adresse deiner Domain mit dem Service http://127.0.0.1:7860 verbinden. Trage diese URL in
CLOUDFLARE_TUNNEL_HOSTNAME ein. app.linguoai.de ist lediglich ein Beispiel für den Betreiber,
kein allgemein verfügbarer Server. Den Token ausschließlich im Colab Secret
CLOUDFLARE_TUNNEL_TOKEN speichern und Notebook-Zugriff erlauben oder verdeckt eingeben.
Token niemals in die Zelle schreiben oder Notebook-Ausgaben weitergeben.

Cloudflare-Browseruploads sind standardmäßig auf 95 MB pro Datei begrenzt. Große Videos und
Piper-Modelle über relative MyDrive-Pfade laden. Der normale Gradio-Modus erlaubt bis zu 5 GB,
abhängig von Sitzung und Dienst. Quick Tunnels (trycloudflare.com) unterstützen die für
Gradio benötigten Server-Sent Events nicht und werden deshalb nicht verwendet.

Alle öffentlichen Links funktionieren nur während der laufenden Sitzung. Dateien unter /content
sind flüchtig: Ergebnisse herunterladen oder in Drive sichern. Referenzstimmen nur mit
Erlaubnis verwenden. Das NLLB-Standardmodell steht unter CC-BY-NC-4.0.
"""


def main() -> None:
    source = (ROOT / "scripts" / "colab_bootstrap.py").read_text(encoding="utf-8")
    ast.parse(source)
    notebook = {
        "cells": [
            {
                "cell_type": "markdown",
                "id": "linguoai-guide",
                "metadata": {},
                "source": INTRO.splitlines(keepends=True),
            },
            {
                "cell_type": "code",
                "id": "linguoai-start",
                "execution_count": None,
                "metadata": {"cellView": "form"},
                "outputs": [],
                "source": source.splitlines(keepends=True),
            },
        ],
        "metadata": {
            "colab": {"name": "LinguoAI_Colab.ipynb", "provenance": []},
            "kernelspec": {"display_name": "Python 3", "name": "python3"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    target = ROOT / "notebooks" / "LinguoAI_Colab.ipynb"
    target.write_text(
        json.dumps(notebook, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"Generated {target.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
