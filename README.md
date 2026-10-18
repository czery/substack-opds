# Substack → OPDS

Mehrnutzer-Server, der Substack-Artikel (Neueste, Für dich, Lesezeichen, Bezahlt) als OPDS-Katalog bereitstellt. Jeder Artikel wird beim Abruf in ein schlankes EPUB umgewandelt (Bilder in Graustufen, max. 700 px). Gedacht für CrossPoint auf dem Xteink X4/X3.

**Hinweis:** Substack hat keine offizielle API. Die Endpunkte in `app/substack.py` sind inoffiziell und noch nicht gegen einen echten Account getestet. Brechen sie, sind alle Pfade dort an einer Stelle anpassbar.

## Deployment auf dem VPS (Docker)

Voraussetzung: Docker mit Compose-Plugin, ein A-Record deiner Domain auf den VPS, Ports 80 und 443 offen.

```bash
cd substack-opds
cp .env.example .env
sed -i 's/^DOMAIN=.*/DOMAIN=deine-domain.de/' .env   # deine echte Domain eintragen
docker compose up -d --build
docker compose logs -f app
```

Danach `https://deine-domain.de` öffnen, Konto anlegen und die OPDS-Adresse kopieren.

- Registrierung schließen: in `.env` `REGISTRATION_OPEN=0` setzen, dann `docker compose up -d`.
- Mit Einladungscode: `INVITE_CODE=...` in `.env` setzen.
- Backup: den Ordner `data/` sichern. `data/keys.json` enthält den Schlüssel für die gespeicherten Cookies. Ohne ihn müssen sich alle neu verbinden.

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
