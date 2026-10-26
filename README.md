# makeleaps-client

Unofficial Python client library and CLI for the MakeLeaps REST API. This project is not affiliated with or endorsed by MakeLeaps, Inc.

The client targets API keys using **MakeLeaps API version `v2023.06.26`**. Response shapes and behavior were checked against that version. MakeLeaps assigns the API version to your API key; the client does not select a version in requests. Other API key versions have not been verified.

Requires Python 3.12 or newer. Distributed under the MIT license.

```sh
pip install makeleaps-client
```

The package provides the `makeleaps_client` library and the `makeleaps` command. Both support read operations only: your company, documents of every type, current document PDFs, Document Archives, and PDFs preserved at the time of sending. Choose the PDF source appropriate for your records; an archive may not exist for every sent document.

## Async Python library

Pass credentials explicitly. The library reads neither environment variables nor configuration files. It discovers the one company associated with the API key, so no company argument is needed.

```python
import asyncio
from pathlib import Path

from makeleaps_client import MakeLeapsClient, MakeLeapsError


async def read_documents(client_id: str, client_secret: str):
    async with MakeLeapsClient(client_id, client_secret) as api:
        company = await api.get_partner()
        print(company.name)

        async for document in api.list_documents(
            document_type="invoice",
            date_from="2025-01-01",
            date_to="2025-12-31",
            filters={"sent": "true"},
        ):
            print(document.mid, document.total, document.client.display_name)

        document = await api.get_document("example-document-mid")
        print(document.model_dump())
        Path("current.pdf").write_bytes(
            await api.get_document_pdf("example-document-mid")
        )

        async for archive in api.list_archives():
            print(archive.mid, archive.date_sent)
        Path("sent.pdf").write_bytes(
            await api.get_archive_pdf("example-archive-mid")
        )


# Supply credentials from your application's own credential handling.
# asyncio.run(read_documents(client_id, client_secret))
```

Use one client within one event loop. The async context manager closes its HTTP connections; alternatively call `await api.aclose()`. Handle request failures with `MakeLeapsError`.

| Operation | Result |
| --- | --- |
| `await api.get_partner()` | `Partner` |
| `api.list_documents(document_type=None, date_from=None, date_to=None, filters=None)` | Async iterator of `Document` |
| `await api.get_document(mid)` | `Document` |
| `await api.get_document_pdf(mid)` | PDF `bytes` |
| `api.list_archives()` | Async iterator of `DocumentArchive` |
| `await api.get_archive_pdf(mid)` | PDF `bytes` |

Date bounds are inclusive (`date__gte` and `date__lte`). They accept `datetime.date` or ISO date strings. Other filters pass through using the API's parameter names and values. To repeat a parameter, supply pairs, for example `filters=[("document_number", "EXAMPLE-A"), ("document_number", "EXAMPLE-B")]`. MakeLeaps may silently ignore unsupported filters; consult its API documentation.

List operations yield every matching record in API order. Pages contain up to 100 records, and up to eight subsequent pages are fetched together when iteration advances beyond the current batch. Stopping iteration avoids subsequent batches; pages already requested in the current batch may have completed. Use `await iterator.aclose()` when explicitly holding an iterator and ending it early.

Documents include full client and client contact resources. Each document operation loads the client and contact lists once and joins them by URL. Those lists are not cached across operations. An unavailable referenced resource causes `MakeLeapsError` rather than returning a partially expanded document.

Models are permissive pydantic models: all fields are optional, unknown fields are retained, and enum-like values remain strings. Known monetary and numeric fields become `Decimal`, dates become `datetime.date`, and timestamps become `datetime.datetime`. Nested line items and tax totals are also typed. `model_dump()` defaults to JSON-compatible API values, retaining unknown fields and omitting fields absent from the response. Explicit `mode="python"` produces Python values instead.

The default response language is Japanese. Set `MakeLeapsClient(client_id, client_secret, language="en")` to change `Accept-Language` on all requests.

## Command line

The CLI reads `MAKELEAPS_CLIENT_ID` and `MAKELEAPS_CLIENT_SECRET` from its environment and passes them to the library. Set them using your shell's or process runner's credential handling. It does not read configuration files.

```sh
makeleaps --help
makeleaps partner
makeleaps documents list --type invoice --date-from 2025-01-01 --date-to 2025-12-31
makeleaps documents list --filter sent=true --filter document_number=EXAMPLE-A --limit 10
makeleaps documents get example-document-mid
makeleaps documents pdf example-document-mid --output current.pdf
makeleaps archives list --limit 10
makeleaps archives pdf example-archive-mid --output sent.pdf
makeleaps documents list --language en
```

Company, document, and archive results are written as complete YAML values to stdout. Each list is a single YAML sequence, including when empty. Monetary values remain strings in YAML. PDF commands write the original bytes to files. Failures produce an explanatory message on stderr and a nonzero exit status; completed files from an interrupted or failed bulk export remain on disk.

Bulk PDF downloads create the destination directory if needed:

```sh
makeleaps documents pdf --dir ./current-pdfs --type invoice --date-from 2025-01-01 --filter sent=true
makeleaps archives pdf --dir ./sent-pdfs --limit 100
```

Documents accept the same filters for `list` and bulk `pdf`. Archives support `--limit` and `--language`. Without `--limit`, list and bulk PDF commands process every matching record; `--limit 0` processes none. `--language` is accepted on every command, and defaults to `ja`.

Bulk filenames use `<date>_<document_type>_<document_number>_<mid>.pdf`. Characters that cannot be used in portable filenames are replaced with `_`. A file with the same `mid` suffix already in the destination directory is skipped without fetching that PDF, even if its document number changed. The current document PDF and its archive PDF can differ; use separate directories for each source.

## Authentication and requests

Requests use HTTPS exclusively on `api.makeleaps.com`, with redirects disabled. Access tokens remain in process memory. Expired tokens and a 401 response cause reauthentication; a second 401 fails. Error messages do not include credentials, tokens, or raw response bodies.

All requests on one client share a rolling limiter of 250 requests per 60 seconds and 20,000 per day, based on observed API response headers. Concurrent clients or other applications using the same credentials can consume the same server quota. A 429 response delays subsequent requests according to `Retry-After` (seconds or an HTTP date). Transport errors and server errors use exponential backoff. A request has at most four attempts, and reauthentication after a 401 is attempted once. The HTTP timeout is 60 seconds.

No third-party MakeLeaps client is used. Tests replace the HTTP boundary with synthetic responses and never contact MakeLeaps:

```sh
uv run --python 3.12 pytest
uv run --python 3.13 pytest
uv build
```
