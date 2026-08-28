# PressReader Sync

PressReader Sync is a fast, e-ink-friendly PressReader delivery app for
[KOReader](https://github.com/koreader/koreader). It has three small parts:

1. `pressreadersync.koplugin` runs on the reader. It browses publications,
   downloads editions, and opens them immediately.
2. `bridge/pressreader_sync_bridge.py` runs on a computer, NAS, or Android/Termux. It
   indexes publication files that you are authorised to use and serves them to
   the reader over the local network.
3. The optional VPS worker monitors PressReader **My Publications** and
   periodically uses its supported **Export to eReader → Nook** action and
   cleans the result into an article-first EPUB for KOReader.

PressReader Sync does **not** decrypt PressReader's Android cache, scrape protected
pages, or remove DRM. Put PDF, EPUB, CBZ, or DJVU editions obtained through an
authorised route into the bridge library and PressReader Sync handles the rest of the
trip to KOReader. The optional worker retains a browser session created
interactively by you and automates only the official eReader export controls
visible to that account. Credentials are never placed in configuration files.

## Fast setup

Arrange a library like this (filenames beginning with an ISO date sort best):

```text
library/
  The Guardian/
    2026-07-19 - Sunday.epub
    2026-07-18.pdf
  National Geographic/
    2026-07.pdf
```

Start the bridge:

```sh
python3 bridge/pressreader_sync_bridge.py --library /path/to/library --token choose-a-long-token
```

Copy the plugin directory to KOReader and restart it:

```text
koreader/plugins/pressreadersync.koplugin/
```

You can also run `make package` and extract
`dist/pressreadersync.koplugin.zip` inside KOReader's `plugins` folder.

In KOReader open **Search (magnifier) → PressReader Sync → Settings**, enter the bridge
URL (for example `http://192.168.1.20:8787`) and the same token. Then use
**Browse publications** to choose and download an edition.

Use **Download all latest editions** to fetch the newest available edition of
every publication in one batch. Editions already downloaded at the expected
file size are skipped, and the plugin reports any publication that failed.

**Downloaded publications** groups editions by publication and shows the newest
issue date. Publications and their editions are ordered by when they were
downloaded, so future-dated issues do not stay pinned to the top.

The bridge prints its usable LAN addresses at startup. Keep it on a trusted
network, set a token, and allow TCP port 8787 through the host firewall if
needed. HTTP is intentional for simple local networks; use a reverse proxy if
traffic crosses an untrusted network.

## Optional publication metadata

Add `publication.json` inside a publication folder:

```json
{
  "title": "The Guardian",
  "language": "en",
  "source_url": "https://www.pressreader.com/"
}
```

`source_url` is informational only and is never fetched by the bridge.

## CLI options

```text
--library PATH      folder to index (required)
--host ADDRESS      bind address (default 0.0.0.0)
--port PORT         listen port (default 8787)
--token TOKEN       bearer token (or PRESSREADER_SYNC_TOKEN)
--token-file PATH   read bearer token from a private file
--cache-seconds N   index cache lifetime (default 5)
```

The bridge server itself is read-only. The optional worker writes completed
exports into its library. The JSON API is documented in
[`docs/protocol.md`](docs/protocol.md).

For unattended PressReader exports on a Debian VPS, follow
[`docs/vps-deployment.md`](docs/vps-deployment.md).
The isolated, non-YunoHost live-test layout is documented in
[`docs/vps-test-install.md`](docs/vps-test-install.md).

## Development

Run the bridge tests with:

```sh
python3 -m unittest discover -s bridge/tests -v
```

Or run all available syntax and integration checks with `make test`.

The plugin targets the current KOReader plugin APIs and uses only modules that
ship with KOReader.

Known limitations and follow-up work are tracked in
[`docs/known-issues.md`](docs/known-issues.md).
