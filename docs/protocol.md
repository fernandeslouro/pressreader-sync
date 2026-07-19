# PressReader Sync bridge protocol v1

All endpoints return JSON except file downloads. When configured, the token is
sent as `Authorization: Bearer <token>`.

| Endpoint | Result |
| --- | --- |
| `GET /v1/status` | Bridge health and indexed counts |
| `GET /v1/publications` | Publications, newest first |
| `GET /v1/publications/{id}/issues` | Editions for one publication, newest first |
| `GET /v1/latest?publication={id}` | Newest edition for one publication |
| `GET /v1/files/{id}` | Edition bytes |

Publication IDs and issue IDs are opaque. Clients must not derive filesystem
paths from them. The current response schema is intentionally small:

```json
{
  "publications": [{
    "id": "opaque",
    "title": "Newspaper",
    "language": "en",
    "issue_count": 12,
    "latest_date": "2026-07-19"
  }]
}
```

```json
{
  "issues": [{
    "id": "opaque",
    "publication_id": "opaque",
    "title": "2026-07-19 - Sunday",
    "date": "2026-07-19",
    "format": "epub",
    "size_bytes": 123456,
    "filename": "2026-07-19 - Sunday.epub",
    "download_url": "/v1/files/opaque"
  }]
}
```

Unknown endpoints and IDs return `404`; bad authentication returns `401`.
Responses contain `Cache-Control: no-store`.

When the optional worker status file is configured, `/v1/status` also contains
an `automation` object with the last run state, timestamps, discovered/exported
counts, and errors.
