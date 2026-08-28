# Debian VPS deployment

This deployment periodically uses PressReader's visible, supported
**Export to eReader → Nook** workflow for the current edition of every title in
**My Publications**. Nook is used because its EPUB contains slightly
higher-resolution editorial images than the Kobo and Sony variants. The worker
then removes duplicated articles, print-page images, thumbnails, page markers,
and repeated navigation chrome; it retains the cover, article text, headlines,
bylines, captions, credits, and editorial images. It does not call private
content APIs or extract the page viewer.

## 1. Install prerequisites

On Debian, install Docker Engine with its Compose plugin. Then clone or copy
this repository to the VPS and run:

```sh
cd deploy
sh setup.sh
nano .env
```

Replace `PRESSREADER_SYNC_TOKEN` with the output of:

```sh
openssl rand -hex 32
```

The browser profile contains login cookies and must be treated like a password.
`setup.sh` restricts the profile and state directories; the worker container
normalizes their ownership for its unprivileged browser user when it starts.

## 2. Perform the one-time login

Do not run the background worker while using the login browser; Chromium allows
only one process to own a profile.

```sh
docker compose stop worker
docker compose --profile login up --build login
```

From your computer, open a second terminal and create an SSH tunnel:

```sh
ssh -L 6080:127.0.0.1:6080 YOUR_USER@YOUR_VPS
```

Open `http://127.0.0.1:6080/vnc.html` locally. Sign in to PressReader, confirm
that the green HotSpot cup and **My Publications** are visible, then close the
browser tab inside the remote desktop. The login container will stop and the
profile will remain in `deploy/data/browser`.

The noVNC port is bound to VPS localhost and is therefore unavailable without
the SSH tunnel.

## 3. Start synchronization

```sh
docker compose up -d --build bridge worker
docker compose logs -f worker
```

The default regular interval is six hours. A run discovers **My Publications**, opens
the latest issue of each saved title, uses **Export to eReader → Nook**, checks
and cleans the EPUB, and stores it under
`deploy/data/library/<publication>/`. Previously exported issue dates are
skipped. If an export fails, the worker keeps retrying that publication after
10 minutes, 30 minutes, 1 hour, and then every 3 hours until it succeeds. A new
issue resets the retry delay. By default Nook is preferred, with Kobo and Sony
tried as official EPUB fallbacks. Configure the order with
`PRESSREADER_SYNC_EXPORT_DEVICES`.

Run an immediate check with:

```sh
docker compose stop worker
docker compose run --rm worker python /app/pressreader_worker.py once
docker compose up -d worker
```

If the website changes, HTML and screenshots are written to
`deploy/data/state/diagnostics`. Check current status with:

```sh
curl -H "Authorization: Bearer YOUR_TOKEN" http://127.0.0.1:8787/v1/status
```

## 4. Connect KOReader

Install `pressreadersync.koplugin`, then set its bridge URL and token. For a Kobo
outside the VPS network, put the bridge behind HTTPS or a private VPN such as
WireGuard/Tailscale. Avoid exposing plain HTTP directly to the public Internet.

The bridge port defaults to all interfaces. Set `PRESSREADER_SYNC_BIND=127.0.0.1` when
using a reverse proxy on the same VPS.

## Maintenance

Library/HotSpot entitlements can expire. When the worker reports that login has
expired, repeat the login procedure. PressReader Sync does not attempt to bypass CAPTCHA,
MFA, access expiry, publisher restrictions, or unavailable export controls.

Useful commands:

```sh
docker compose logs --tail=200 worker
docker compose restart worker
docker compose pull
docker compose build --pull
docker compose up -d
```
