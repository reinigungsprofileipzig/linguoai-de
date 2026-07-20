# linguoai.de veröffentlichen

Die Website ist statisch. Es gibt keinen Build-Schritt, keine externen Laufzeitpakete und kein
Tracking. Veröffentlicht wird der komplette Inhalt des Ordners `website/`.

## Vor dem Livegang

1. `impressum.html` mit den echten Betreiberangaben vervollständigen.
2. `datenschutz.html` an Betreiber, Hoster, Server-Logs und Kontaktweg anpassen.
3. Beide Seiten fachlich prüfen und danach `noindex,nofollow` durch `index,follow` ersetzen.
4. Optional eine echte Projekt-/Repository-Adresse ergänzen, sobald sie öffentlich feststeht.
5. Prüfen, dass `downloads/LinguoAI_Colab.ipynb` der aktuellen Anwendungsversion entspricht.

Die Vorlagen erfinden bewusst keine fehlenden Namen, Anschriften oder Kontaktangaben.

## Lokal ansehen

Im Projektordner:

```powershell
python -m http.server 8080 --directory website
```

Danach `http://localhost:8080/` öffnen. Der Server lässt sich mit `Strg+C` beenden.

## Empfohlener Weg: Cloudflare Pages

Der DNS-Check vom 20. Juli 2026 zeigt Cloudflare-Nameserver für `linguoai.de`, aber noch
keinen auflösbaren A-/AAAA-/CNAME-Eintrag für die Hauptdomain und keinen `www`-CNAME. Daher
passt Cloudflare Pages als einfacher statischer Hoster, die Domain ist derzeit aber noch nicht
mit einer veröffentlichten Website verbunden.

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

