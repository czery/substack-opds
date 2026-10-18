# Substack → OPDS

Mehrnutzer-Server, der Substack-Artikel (Neueste, Für dich, Lesezeichen, Bezahlt) als OPDS-Katalog bereitstellt. Jeder Artikel wird beim Abruf in ein schlankes EPUB umgewandelt (Bilder in Graustufen, max. 700 px). Gedacht für CrossPoint auf dem Xteink X4/X3.

**Hinweis:** Substack hat keine offizielle API. Die Endpunkte in `app/substack.py` sind inoffiziell und noch nicht gegen einen echten Account getestet. Brechen sie, sind alle Pfade dort an einer Stelle anpassbar.

## Deployment mit Dokploy

1. In Dokploy eine Compose-Anwendung anlegen, Provider GitHub, Repo `substack-opds`, Branch `main`, Compose-Pfad `./docker-compose.yml`, Trigger *On Push*.
2. Im Tab *Environment* setzen: `PUBLIC_URL=https://deine-domain.de`, `REGISTRATION_OPEN=1` und optional `INVITE_CODE`.
3. Im Tab *Domains* die Domain für den Service `app` auf Port `8000` eintragen, HTTPS mit Let's Encrypt aktivieren.
4. Deploy starten. Danach löst jeder Push auf `main` ein neues Deployment aus.

HTTPS und Zertifikate übernimmt Traefik von Dokploy. Der Daten-Ordner liegt im Named Volume `appdata`.

- Registrierung schließen: `REGISTRATION_OPEN=0` setzen und neu deployen.
- Backup: das Volume `appdata` sichern. `keys.json` darin enthält den Schlüssel für die gespeicherten Cookies. Ohne ihn müssen sich alle neu verbinden.

## Substack verbinden

**Browser-Extension (empfohlen):**
1. In Chrome `chrome://extensions` öffnen, Entwicklermodus an, "Entpackte Erweiterung laden" und den Ordner `extension/` wählen.
2. Auf das Extension-Symbol klicken, Server-URL und Sync-Token eintragen (beides steht im Dashboard), speichern.
3. Ab dann sendet die Extension `substack.sid` bei jeder Änderung und alle 6 Stunden an den Server. Du musst bei Substack im Browser eingeloggt sein.

**Manuell:** Im Dashboard den Wert von `substack.sid` einfügen (DevTools → Application → Cookies → substack.com).

## Reader einrichten

Im CrossPoint-OPDS-Browser einen Server mit der Adresse aus dem Dashboard hinzufügen (`https://deine-domain.de/o/<token>/`). Es erscheinen vier Kataloge. Ein Tippen auf einen Artikel lädt das EPUB aufs Gerät.

## Sicherheit

- Cookies liegen verschlüsselt (Fernet) in SQLite, Passwörter als scrypt-Hash.
- Die OPDS-Adresse ist ein geheimer Token. Im Dashboard lässt sie sich erneuern.
- EPUBs werden pro Nutzer:in gecacht und nie geteilt (bezahlte Artikel!). Cache wird nach 30 Tagen geleert.
- Bilder werden nur von Substack-Hosts geladen, das Cookie geht nie an CDNs.
- Zugriffslogs sind aus, damit Tokens nicht in Logs landen.
- Betrieb für andere: Datenschutzerklärung nötig, und Substack kann die Nutzung in den AGB untersagen.

## Lokal testen

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
DATA_DIR=$(mktemp -d) HTTPS_ONLY=0 .venv/bin/python tests/smoke.py
DATA_DIR=./data HTTPS_ONLY=0 .venv/bin/uvicorn app.main:app --port 8000
```
