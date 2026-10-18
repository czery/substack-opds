# Substack → OPDS

Multi-user server that exposes Substack articles (Latest, For you, Saved, Paid) as an OPDS catalog. Each article is converted on request into a lean EPUB (grayscale images, max. 700 px). Built for CrossPoint on the Xteink X4/X3.

**Note:** Substack has no official API. The endpoints in `app/substack.py` are unofficial. They were verified against a real account, but may change at any time. If they break, all paths can be adjusted in that one place. Catalogs and the number of articles per catalog are configurable per user in the web dashboard.

## Deployment with Dokploy

1. In Dokploy, create a Compose application: provider GitHub, repo `substack-opds`, branch `main`, compose path `./docker-compose.yml`, trigger *On Push*.
2. In the *Environment* tab, set `PUBLIC_URL=https://your-domain.com`, `REGISTRATION_OPEN=1` and optionally `INVITE_CODE`.
3. In the *Domains* tab, add your domain for the service `app` on port `8000` and enable HTTPS with Let's Encrypt.
4. Start the deployment. From then on, every push to `main` triggers a new deployment.

HTTPS and certificates are handled by Dokploy's Traefik. The data folder lives in the named volume `appdata`.

- Close registration: set `REGISTRATION_OPEN=0` and redeploy.
- Backup: back up the `appdata` volume. The `keys.json` inside holds the key for the stored cookies. Without it, everyone has to reconnect.

## Connect Substack

**Browser extension (recommended):**
1. Open `chrome://extensions` in Chrome, enable developer mode, click "Load unpacked" and select the `extension/` folder.
2. Click the extension icon, enter the server URL and sync token (both are shown in the dashboard) and save.
3. From then on, the extension sends `substack.sid` to the server whenever it changes and every 6 hours. You need to be logged in to Substack in your browser.

**Manually:** Paste the value of `substack.sid` into the dashboard (DevTools → Application → Cookies → substack.com).

## Set up your reader

In the CrossPoint OPDS browser, add a server with the address from the dashboard (`https://your-domain.com/o/<token>/`). Four catalogs appear. Tapping an article downloads the EPUB to the device.

## Security

- Cookies are stored encrypted (Fernet) in SQLite, passwords as scrypt hashes.
- The OPDS address is a secret token. It can be rotated in the dashboard.
- EPUBs are cached per user and never shared (paid articles!). The cache is purged after 30 days.
- Images are only loaded from Substack hosts; the cookie is never sent to CDNs.
- Access logs are disabled so tokens don't end up in logs.
- If you run this for others: you need a privacy policy, and Substack's terms may prohibit this kind of use.

## Local testing

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
DATA_DIR=$(mktemp -d) HTTPS_ONLY=0 .venv/bin/python tests/smoke.py
DATA_DIR=./data HTTPS_ONLY=0 .venv/bin/uvicorn app.main:app --port 8000
```
