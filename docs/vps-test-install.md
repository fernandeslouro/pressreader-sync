# Isolated VPS test installation

For a trial that must not alter an existing YunoHost installation, keep the
entire test under one unprivileged user's home directory. Do not install an
nginx configuration, open a firewall port, or create system services during
the trial. The Docker Compose deployment in [`vps-deployment.md`](vps-deployment.md)
is the preferred reproducible setup.

Choose a private installation directory, for example:

```sh
INSTALL_DIR="$HOME/pressreader-sync-test"
```

Keep the browser profile, token, state, and exported library beneath that
directory. They are intentionally excluded from Git. The browser profile
contains an authenticated PressReader session and must be readable only by its
owner.

The bridge should listen on `127.0.0.1:8787` while testing. This avoids a port
collision with YunoHost's public services and prevents accidental public HTTP
exposure. Put it behind YunoHost-managed HTTPS or a private VPN only after the
workflow has been verified.

Useful read-only checks for a manually launched trial are:

```sh
pgrep -a -u "$USER" -f pressreader_worker.py
pgrep -a -u "$USER" -f pressreader_sync_bridge.py
tail -100 "$INSTALL_DIR/runtime/worker/worker.log"
cat "$INSTALL_DIR/runtime/state/worker-status.json"
find "$INSTALL_DIR/runtime/library" -name '*.epub' -type f
```

Read the KOReader token directly on the server instead of copying it into
documentation or shell history shared with others:

```sh
cat "$INSTALL_DIR/runtime/bridge/token"
```

A manually launched trial will not restart after a VPS reboot. Use the Compose
deployment once the trial is accepted and persistent operation is wanted.
