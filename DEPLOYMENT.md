# linguoai.de veröffentlichen

Die Website ist statisch. Es gibt keinen Build-Schritt, keine externen Laufzeitpakete und kein
Tracking. Die Dateien aus `website/` liegen im öffentlichen GitHub-Repository im Root, damit
GitHub Pages sie direkt ausliefert. Das Colab-Notebook wird von dort geöffnet und lädt das
geprüfte Quellcode-ZIP anschließend automatisch über die kanonische HTTPS-Downloadadresse.

Beim Synchronisieren den vorhandenen `CNAME`-Eintrag für `linguoai.de` erhalten.

## Vor dem Livegang

1. `impressum.html` mit den echten Betreiberangaben vervollständigen.
2. `datenschutz.html` an Betreiber, Hoster, Server-Logs und Kontaktweg anpassen.
3. Beide Seiten fachlich prüfen und danach `noindex,nofollow` durch `index,follow` ersetzen.
4. Prüfen, dass alle Verweise auf `https://github.com/reinigungsprofileipzig/linguoai-de` zeigen.
5. Prüfen, dass `downloads/LinguoAI_Colab.ipynb` und die Projektdatei
   `notebooks/LinguoAI_Colab.ipynb` bytegleich sind.
6. Das Quellcode-ZIP neu bauen und testen, wenn Programm, README, Tests oder Notebook geändert wurden.

Die Vorlagen erfinden bewusst keine fehlenden Namen, Anschriften oder Kontaktangaben.

## Lokal ansehen

Im Projektordner:

```powershell
python -m http.server 8080 --directory website
```

Danach `http://localhost:8080/` öffnen. Der Server lässt sich mit `Strg+C` beenden.

## Aktueller Weg: GitHub Pages

- Repository: `https://github.com/reinigungsprofileipzig/linguoai-de`
- Produktionsbranch: `main`
- Veröffentlichungsordner: Repository-Root
- Eigene Domain: `linguoai.de` über `CNAME`

Nach einem Push warten, bis GitHub Pages die neue Revision veröffentlicht hat. Danach Startseite,
Colab-Link, Git-Klon und alle drei Downloads live prüfen. HTTPS im Pages- beziehungsweise
Cloudflare-Setup erzwingen.

## Alternative: Cloudflare Pages

Cloudflare Pages kann alternativ als statischer Hoster eingesetzt werden. Vor einem Wechsel die
bestehende GitHub-Pages-Domain, DNS-Einträge und HTTPS-Weiterleitungen dokumentieren und eine
Vorschau vollständig prüfen.

### Direkter Upload im Dashboard

1. Im Cloudflare-Dashboard **Workers & Pages** öffnen.
2. **Create application → Get started → Drag and drop your files** wählen.
3. Als Projektnamen zum Beispiel `linguoai` eintragen.
4. Den vollständigen Ordner `website/` hochladen und **Deploy site** wählen.
5. Erst die erzeugte `*.pages.dev`-Vorschau vollständig prüfen.
6. Im Pages-Projekt **Custom domains → Set up a domain** öffnen.
7. Zuerst `linguoai.de` verbinden. Da die Zone bereits Cloudflare-Nameserver verwendet,
   kann Cloudflare den benötigten DNS-Eintrag nach Bestätigung anlegen.
8. Optional `www.linguoai.de` ebenfalls verbinden und konsequent auf die Hauptdomain umleiten.

Cloudflare beschreibt den direkten Upload und die Domain-Verknüpfung hier:

- [Direct Upload](https://developers.cloudflare.com/pages/get-started/direct-upload/)
- [Custom domains](https://developers.cloudflare.com/pages/configuration/custom-domains/)

Wichtig: Die Domain zuerst über **Custom domains** mit dem Pages-Projekt verbinden. Laut
Cloudflare kann ein nur manuell gesetzter CNAME ohne diese Zuordnung zu einem 522-Fehler führen.

### Optional per Wrangler

Nach Anmeldung bei Cloudflare:

```powershell
npx wrangler pages deploy website --project-name=linguoai
```

Das lädt genau diesen statischen Ordner hoch. Externe Änderungen sollten erst nach Prüfung der
Vorschau und der Rechtstexte erfolgen.

## Git-Integration

Bei einer späteren Veröffentlichung des Projekts in einem Git-Repository kann Pages auch direkt
mit dem Repository verbunden werden:

- Build-Befehl: leer
- Build-Ausgabeordner: `website`
- Produktionsbranch: der tatsächlich verwendete Hauptbranch

Vorher entscheiden: Cloudflare weist darauf hin, dass ein als **Direct Upload** angelegtes
Pages-Projekt nicht nachträglich auf Git-Integration umgestellt wird; dafür wäre ein neues
Pages-Projekt nötig.

## Abschließende Kontrolle

- Desktop: Navigation, Tabs, Kopierbuttons und alle Links testen.
- Mobil: 360 px und 768 px Breite prüfen.
- „Bewegung reduzieren“ im Betriebssystem aktivieren und kontrollieren.
- `https://linguoai.de/robots.txt` und `/sitemap.xml` abrufen.
- HTTPS, Canonical und Weiterleitung von `www` prüfen.
- Download des Colab-Notebooks testen.
- Impressum und Datenschutz ohne Platzhalter veröffentlichen.
