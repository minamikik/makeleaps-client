# makeleaps-client

Unofficial Python client library and CLI for the MakeLeaps REST API. This project is not affiliated with or endorsed by MakeLeaps, Inc.

The client targets API keys using **MakeLeaps API version `v2023.06.26`**. Response shapes and behavior were checked against that version. MakeLeaps assigns the API version to your API key; the client does not select a version in requests. Other API key versions have not been verified.

Requires Python 3.12 or newer. Distributed under the MIT license.

```sh
pip install makeleaps-client
```

The package provides the `makeleaps_client` library and the `makeleaps` command. Both read your company, documents of every type, current document PDFs, Document Archives, and PDFs preserved at the time of sending. Both also create, edit, and delete documents. Sending documents is not supported. Choose the PDF source appropriate for your records; an archive may not exist for every sent document.

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
| `await api.create_document(data)` | Created `Document` |
| `await api.update_document(mid, data)` | Updated `Document` |
| `await api.delete_document(mid)` | `None` |
| `await api.get_document_pdf(mid)` | PDF `bytes` |
| `api.list_archives()` | Async iterator of `DocumentArchive` |
| `await api.get_archive_pdf(mid)` | PDF `bytes` |

Date bounds are inclusive (`date__gte` and `date__lte`). They accept `datetime.date` or ISO date strings. Other filters pass through using the API's parameter names and values. To repeat a parameter, supply pairs, for example `filters=[("document_number", "EXAMPLE-A"), ("document_number", "EXAMPLE-B")]`. MakeLeaps may silently ignore unsupported filters; consult its API documentation.

List operations yield every matching record in API order. Pages contain up to 100 records, and up to eight subsequent pages are fetched together when iteration advances beyond the current batch. Stopping iteration avoids subsequent batches; pages already requested in the current batch may have completed. Use `await iterator.aclose()` when explicitly holding an iterator and ending it early.

Documents include full client and client contact resources. Getting, creating, or updating one document fetches only its referenced client and contact. A document list fetches the first document's references immediately, then loads the client and contact lists once for subsequent documents. Those lists are not cached across operations. An unavailable referenced resource causes `MakeLeapsError` rather than returning a partially expanded document.

Models are permissive pydantic models: all fields are optional, unknown fields are retained, and enum-like values remain strings. Known monetary and numeric fields become `Decimal`, dates become `datetime.date`, and timestamps become `datetime.datetime`. Nested line items and tax totals are also typed. `model_dump()` defaults to JSON-compatible API values, retaining unknown fields and omitting fields absent from the response. Explicit `mode="python"` produces Python values instead.

The default response language is Japanese. Set `MakeLeapsClient(client_id, client_secret, language="en")` to change `Accept-Language` on all requests.

Create and update accept a mapping in the API's JSON shape or a `Document` model. Expanded `client` and `client_contact` objects become their URLs, and read-only document fields are removed before sending. When `tax_rounding` is present, `round_tax_down` is omitted. Other fields, including unknown fields, pass through for the API to validate. Updates use PATCH; omitted fields stay unchanged, and explicit `None` values are sent as JSON null.

For creation and updates that include `lineitems`, makeleaps-client sets `autocalculate` to `true`, overriding any input value. MakeLeaps calculates normal line item amounts, subtotal line amounts, subtotals, taxes, and totals from unit costs and quantities. The request omits the input's `subtotal`, `tax`, `total`, `jp_withholding_tax`, `subtotal_sales_tax`, `subtotal_withholding_tax`, `mixed_tax_rate_totals`, `tax_group_totals`, and `price` on normal and subtotal line items (`kind: normal` and `kind: subtotal`). Prices on other line item kinds, such as `simple`, are preserved. For updates without `lineitems`, total fields and `autocalculate` are sent as supplied, and neither is added when absent. MakeLeaps does not recalculate totals for these updates.

If a create request loses its response or receives a server error, `MakeLeapsError` explains that the document may already exist. Check MakeLeaps before retrying. If creation succeeds but fetching its client or contact fails, the error states that the document was created and includes its returned `mid`.

Documents created through the API are not linked to other documents. A created document has no source document, and the document it was copied from does not list it as a converted document; the API provides no fields for these links. The sender contact selection shown in the MakeLeaps web app also remains empty (`<連絡先なし>` in Japanese). Your company's contact appears only in the `sender_*` text fields, such as `sender_contact_name`.

```python
async def change_documents(api: MakeLeapsClient):
    created = await api.create_document({
        "document_type": "invoice",
        "document_template": "example-template-mid",
        "document_number": "EXAMPLE-INV-001",
        "currency": "JPY",
        "date": "2025-04-12",
        "client": "https://api.makeleaps.com/api/partner/example-company/client/example-client/",
        "client_contact": "https://api.makeleaps.com/api/partner/example-company/contact/example-contact/",
        "lineitems": [{
            "kind": "normal",
            "description": "Example service",
            "unit_cost": "1000",
            "quantity": "1",
            "tax_rate": "10",
            "is_taxable": True,
            "is_tax_inclusive": False,
            "has_jp_withholding_tax": False,
        }],
    })
    updated = await api.update_document(created.mid, {"title": "Revised invoice"})
    updated.note = "Reviewed"
    await api.update_document(updated.mid, updated)
    await api.delete_document(created.mid)
```

Each line item requires `has_jp_withholding_tax`, including when its value is `False`. Set `document_number` explicitly according to your company's numbering scheme, using at most 20 characters.

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

Create and update read a YAML mapping from `--file`; `-` reads stdin. They output the resulting document as YAML. To create a new document, export an existing document with `makeleaps documents get <mid>`, edit the YAML, and pass it to `makeleaps documents create --file <file>`.

For creation and updates that include `lineitems`, makeleaps-client sets `autocalculate` to `true`. MakeLeaps calculates normal line item amounts, subtotal line amounts, subtotals, taxes, and totals from unit costs and quantities, so the input's values for these calculated fields are omitted from requests. Prices on other line item kinds, such as `simple`, are preserved. For updates without `lineitems`, total fields and `autocalculate` are sent as supplied; MakeLeaps does not recalculate totals.

Always change the document number (`document_number`), dates, and line items (`lineitems`) before creating. Otherwise, the new document may have the same number or content as the original.

The MakeLeaps API and makeleaps-client do not fill recipient fields (`recipient_*`, such as `recipient_contact_name` and `recipient_postal_code`) or sender fields (`sender_*`, such as `sender_contact_name` and `sender_postal_code`) from `client`, `client_contact`, or your company information. When writing YAML from scratch, include these fields in the input. When reusing an existing document, its recipient and sender fields are copied to the new document.

The created document is not linked to the document it was copied from: it has no source document, and the original does not list it as a converted document. The sender contact selection in the MakeLeaps web app remains empty (`<連絡先なし>` in Japanese); your company's contact appears only in the `sender_*` text fields.

```sh
makeleaps documents get example-document-mid > document.yaml
# Change document_number, dates, and lineitems in document.yaml before creating.
makeleaps documents create --file document.yaml
# To update an existing document, edit the desired fields and use its mid.
makeleaps documents update example-document-mid --file document.yaml
cat document.yaml | makeleaps documents update example-document-mid --file -
makeleaps documents delete example-document-mid --yes
```

Deletion requires `--yes` and produces no stdout output. Without `--yes`, the CLI fails before sending any request. Create and update reject invalid YAML or a top-level value other than a mapping before sending any request.

Company, document, and archive results are written as complete YAML values to stdout. Each list is a single YAML sequence, including when empty. Monetary values remain strings in YAML. PDF commands write the original bytes to files. Failures produce an explanatory message on stderr and a nonzero exit status; completed files from an interrupted or failed bulk export remain on disk.

Bulk PDF downloads create the destination directory if needed:

```sh
makeleaps documents pdf --dir ./current-pdfs --type invoice --date-from 2025-01-01 --filter sent=true
makeleaps archives pdf --dir ./sent-pdfs --limit 100
```

Documents accept the same filters for `list` and bulk `pdf`. Archives support `--limit` and `--language`. Without `--limit`, list and bulk PDF commands process every matching record; `--limit 0` processes none. `--language` is accepted on every command, and defaults to `ja`.

Bulk filenames use `<date>_<document_type>_<document_number>_<mid>.pdf`. Characters that cannot be used in portable filenames are replaced with `_`. A file with the same `mid` suffix already in the destination directory is skipped without fetching that PDF, even if its document number changed. The current document PDF and its archive PDF can differ; use separate directories for each source.

## Authentication and requests

Requests use HTTPS exclusively on `api.makeleaps.com`, with redirects disabled. Access tokens remain in process memory. Expired tokens and a 401 response cause reauthentication; a second 401 fails. API validation failures include the HTTP status, field names, and their error messages in `MakeLeapsError` and on CLI stderr, including `non_field_errors` for errors unrelated to one field. Nested errors include field names and zero-based row numbers separated by dots, such as `lineitems.0.has_jp_withholding_tax: Example withholding flag is required.` Empty objects for rows without errors are skipped. Unexpected error response shapes or responses without error messages produce a generic HTTP failure. Credentials and access tokens are redacted from the entire error message; raw response bodies are not included.

All requests on one client share a rolling limiter of 250 requests per 60 seconds and 20,000 per day, based on observed API response headers. Concurrent clients or other applications using the same credentials can consume the same server quota. A 429 response delays subsequent requests according to `Retry-After` (seconds or an HTTP date). OAuth token requests, GET, PATCH, and DELETE retry transport errors and server errors with exponential backoff. Document creation retries only 429 responses and failures to establish a connection (`ConnectError` or `ConnectTimeout`); other transport failures and server errors fail immediately to avoid repeating document creation. A request has at most four attempts, and reauthentication after a 401 is attempted once. The HTTP timeout is 60 seconds.

No third-party MakeLeaps client is used. Tests replace the HTTP boundary with synthetic responses and never contact MakeLeaps:

```sh
uv run --python 3.12 pytest
uv run --python 3.13 pytest
uv build
```
