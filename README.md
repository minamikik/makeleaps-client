# makeleaps-client

Unofficial Python client library and CLI for the MakeLeaps REST API. This project is not affiliated with or endorsed by MakeLeaps, Inc.

The client targets API keys using **MakeLeaps API version `v2023.06.26`**. Response shapes and behavior were checked against that version. MakeLeaps assigns the API version to your API key; the client does not select a version in requests. Other API key versions have not been verified.

Requires Python 3.12 or newer. Distributed under the MIT license.

```sh
pip install makeleaps-client
```

The package provides the `makeleaps_client` library and the `makeleaps` command. Both read your company, documents of every type, current document PDFs, Document Archives, and PDFs preserved at the time of sending. Both also create, convert, edit, and delete documents, list/get/create/update clients, and list/get/create/update/delete contacts. Document creation can copy a selected recipient address. Delivery outside MakeLeaps can be recorded with a sent date and Document Archive, and its status can be checked. Sending documents is not supported. Choose the PDF source appropriate for your records; an archive may not exist for every sent document.

Client deletion and archiving are outside the scope of makeleaps-client. Use the MakeLeaps web app to delete or archive clients.

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
            client_name="Example",
            project_name="migration",
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
| `api.list_documents(document_type=None, date_from=None, date_to=None, client_name=None, project_name=None, filters=None)` | Async iterator of `Document` |
| `await api.get_document(mid)` | `Document` |
| `await api.create_document(data, recipient_address=None)` | Created `Document` |
| `await api.convert_document(mid, document_type, document_number, *, template=None, date=None, deadline=None, deadline_days=None)` | Created `Document` |
| `await api.update_document(mid, data)` | Updated `Document` |
| `await api.delete_document(mid)` | `None` |
| `await api.mark_document_sent(mid, date_sent, method)` | `MarkSentResult` with `mid`, `date_sent`, `archive_created`, and `archive` |
| `api.list_sent_status(document_type=None, date_from=None, date_to=None, client_name=None, project_name=None, filters=None)` | Async iterator of `SentStatus` |
| `await api.get_document_pdf(mid)` | PDF `bytes` |
| `api.list_archives()` | Async iterator of `DocumentArchive` |
| `await api.get_archive_pdf(mid)` | PDF `bytes` |
| `api.list_clients(filters=None)` | Async iterator of `Client` |
| `await api.get_client(mid)` | `Client` |
| `await api.create_client(data)` | Created `Client` |
| `await api.update_client(mid, data)` | Updated `Client` |
| `api.list_contacts(client_mid=None, filters=None)` | Async iterator of `Contact` |
| `await api.get_contact(mid)` | `Contact` |
| `await api.create_contact(client_mid, data)` | Created `Contact`, added to the client |
| `await api.update_contact(mid, data)` | Updated `Contact` |
| `await api.delete_contact(mid)` | `None` |

Date bounds are inclusive (`date__gte` and `date__lte`). They accept `datetime.date` or ISO date strings. Other filters pass through using the API's parameter names and values. To repeat a parameter, supply pairs, for example `filters=[("document_number", "EXAMPLE-A"), ("document_number", "EXAMPLE-B")]`. MakeLeaps may silently ignore unsupported filters; consult its API documentation.

`client_name` and `project_name` match case-sensitive substrings and may be used separately or together. A client-name search uses the client list's `search` parameter, keeps clients whose `display_name` contains the text, and reads documents with each matching client's MID in the `client` filter. Results are grouped by matching clients in API order. `project_name` filters the returned documents locally. Both intersect type, date, and other API filters, including an existing `client` filter. `list_sent_status` accepts the same arguments. Searches fetch pages on demand; stopping iteration avoids reading remaining clients and document batches.

List operations yield every matching record in API order. Pages contain up to 100 records, and up to eight subsequent pages are fetched together when iteration advances beyond the current batch. Stopping iteration avoids subsequent batches; pages already requested in the current batch may have completed. Use `await iterator.aclose()` when explicitly holding an iterator and ending it early.

Documents include full client and client contact resources. Expanding the result of getting, creating, converting, or updating one document fetches only its referenced client and contact. A document list fetches the first document's references immediately, then loads the client and contact lists once for subsequent documents. Those lists are not cached across operations. An unavailable referenced resource causes `MakeLeapsError` rather than returning a partially expanded document.

API resource models are permissive pydantic models: all fields are optional, unknown fields are retained, and enum-like values remain strings. Known monetary and numeric fields become `Decimal`, dates become `datetime.date`, and timestamps become `datetime.datetime`. Nested line items and tax totals are also typed. `model_dump()` defaults to JSON-compatible API values, retaining unknown fields and omitting fields absent from the response. Explicit `mode="python"` produces Python values instead.

The default response language is Japanese. Set `MakeLeapsClient(client_id, client_secret, language="en")` to change `Accept-Language` on all requests.

Create and update accept a mapping in the API's JSON shape or a `Document` model. Expanded `client` and `client_contact` objects become their URLs, and read-only document fields are removed before sending. When `tax_rounding` is present, `round_tax_down` is omitted. Other fields, including unknown fields, pass through for the API to validate. Updates use PATCH; omitted fields stay unchanged, and explicit `None` values are sent as JSON null.

Before creating a document or changing its `document_number`, the library checks the document list for another document with the same type and number. A conflict raises `MakeLeapsError` containing the existing document's `mid`, without sending the create or update request. Updating an unchanged number skips the list check, and the document being edited does not count as a conflict. Updates without `document_number` skip the check entirely. Conversion and duplication use the same creation check. The check and write are separate requests; the API still validates concurrent writes.

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

### Recipient addresses

Pass `recipient_address="client"` to `create_document` to copy the address of the client's contact whose `contact_type` is `organization`. Pass `recipient_address="contact"` to copy the address of the document's `client_contact`. Both read the first entry in the selected contact's `addresses` list and replace `recipient_address_format`, `recipient_country`, `recipient_postal_code`, `recipient_region`, `recipient_locality`, `recipient_street_address`, and `recipient_extended_address`. Missing address parts become empty strings, so an old building name is not retained. Recipient names and phone numbers and all sender fields stay as supplied.

The selection uses the input's client or client contact URL, including URLs from expanded resources. If the client has no organization contact, the selected contact has no address, or the required reference is missing, creation fails with `MakeLeapsError` before a document is created. Without `recipient_address`, recipient fields are sent as supplied. Updates continue to send supplied fields without selecting an address.

```python
async def create_with_address(api: MakeLeapsClient, data: dict):
    return await api.create_document(data, recipient_address="client")
```

### Converting and duplicating documents

`convert_document(mid, document_type, document_number, *, template=None, date=None, deadline=None, deadline_days=None)` reads the source document and creates a new document with the requested type and number. Use the source's type to duplicate it; without an explicit template, a same-type copy uses the source template. It copies the client, client contact, recipient and sender fields, title, project, line items, and other content. It returns the created `Document` with its client and contact expanded. Creation uses the same read-only field removal, reference URLs, and automatic amount calculation described above. The source document is unchanged, and the API does not link the new document to its source or mark the source as converted.

Pass a template code with `template` to select it explicitly. Without it, conversion first uses the source template code if that code is available for the new type. Otherwise, it reads the source template's `language_code` and prefers the first target template with both the same language and `use_advanced_tax_settings`; if none matches both, it uses the first with the same language. If the source language is unavailable or no target template has that language, conversion fails before creation and asks you to specify `template`. `--language` and the client's `language` option control API response language, independently of template selection. Content unsupported by the selected template is left for the API to validate.

The issue date defaults to the creation day's date in Japan. `date` and `deadline` accept `datetime.date` or an ISO date string (`YYYY-MM-DD`). Set either `deadline` or integer `deadline_days` to choose the new deadline; days are counted from the new issue date, including zero or negative values. Invoices use `date_due`; quotes and orders use `date_valid`. Other types reject explicit deadlines before sending requests. Without either argument, if the source type uses the same deadline field and has a value, the new deadline preserves the number of days from the source issue date to its deadline. Otherwise, the deadline is omitted. Deadlines are never copied unchanged or mapped between `date_valid` and `date_due`. Conversion omits `date_sent`, `date_paid`, `date_cancelled`, `date_accepted`, `date_confirmed`, `date_shipped`, and `payment_date`. Invoice period start and end dates are copied as content.

```python
async def convert_documents(api: MakeLeapsClient):
    invoice = await api.convert_document("example-quote-mid", "invoice", "EXAMPLE-INV-002")
    delivery = await api.convert_document(
        invoice.mid, "deliveryslip", "EXAMPLE-DEL-002",
        template="example-template-code", date="2025-05-06",
    )
    duplicate = await api.convert_document(
        "example-quote-mid", "quote", "EXAMPLE-QUOTE-003",
        date="2025-05-06", deadline_days=14,
    )
    return delivery, duplicate
```

### Recording delivery outside MakeLeaps

`mark_document_sent(mid, date_sent, method)` first saves only the document's `date_sent`, then creates a Document Archive for `method="electronic"` or `method="physical"` (API values `manually_sent_electronically` and `manually_sent_physically`). This operation accepts every document type. Order slips (`orderslip`) and acceptance certificates (`acceptance`) receive only the sent date because the API cannot archive them. The returned `MarkSentResult` has `archive_created=False` and `archive=None` for those two types; otherwise it contains the created `DocumentArchive`. `MarkSentResult` and `SentStatus` are available from `makeleaps_client`.

`date_sent` accepts an ISO date or datetime string, `datetime.date`, or `datetime.datetime`. A date alone becomes midnight in Japan (`+09:00`). A datetime without a timezone also uses Japan time; an explicit timezone is preserved. The archive's `date_sent` is the API's request receipt time and can differ from the document's sent date.

If the archive fails after the sent date is saved, `MakeLeapsError` identifies the document and saved date. The date is kept. An uncertain archive creation is not repeated automatically; check MakeLeaps before retrying to avoid duplicate archives.

`list_sent_status` accepts the same filters as `list_documents`. Each `SentStatus` contains `mid`, `document_type`, `document_number`, `date_sent`, and `has_external_archive`. A match requires the same document type and number and at least one `legal_categories` value of `electronically_outside_makeleaps` or `physically_outside_makeleaps`. A sent date alone does not prove that an archive exists. Archives have no link to the original document, so documents sharing a type and number cannot be distinguished. All archive pages are read once per nonempty status operation.

```python
async def record_delivery(api: MakeLeapsClient):
    result = await api.mark_document_sent("example-document-mid", "2025-04-12", "electronic")
    print(result.date_sent, result.archive_created)
    async for status in api.list_sent_status(document_type="invoice", filters={"sent": "true"}):
        print(status.mid, status.date_sent, status.has_external_archive)
```

### Clients and contacts

Client and contact writes accept mappings in the API's JSON shape or the corresponding `Client` or `Contact` model. Read-only fields are removed, unknown fields pass through, and updates use PATCH to change only supplied fields. Client creation accepts either contact URLs or inline contact mappings. An expanded `default_contact` becomes its URL. List operations accept API query parameters through `filters`, including repeated pairs; `list_contacts(client_mid=...)` uses that client's contact list.

```python
async def change_clients_and_contacts(api: MakeLeapsClient):
    async for client in api.list_clients(filters={"archived": "false"}):
        print(client.mid, client.display_name)

    client = await api.create_client({
        "client_external_id": "EXAMPLE-CLIENT-001",
        "contacts": [{
            "contact_type": "organization",
            "name": "Example Customer",
            "email": None,
            "addresses": [],
        }],
    })
    await api.update_client(client.mid, {"client_external_id": "EXAMPLE-CLIENT-002"})

    contact = await api.create_contact(client.mid, {
        "contact_type": "person",
        "family_name": "Example",
        "given_name": "Person",
        "email": {"address": "person@example.invalid"},
        "addresses": [],
    })
    await api.update_contact(contact.mid, {"department": "Example Department"})
    async for current in api.list_contacts(client_mid=client.mid):
        print(current.mid, current.display_name)
```

Contact creation requires a client's `mid`. `create_contact(client_mid, data)` first reads the client, creates the contact, and adds its URL alongside every existing contact URL. It returns the created `Contact`. If adding fails after creation, `MakeLeapsError` states that the contact was created and includes its `mid` and the reason it could not be added. The contact is kept; check it before retrying to avoid creating a duplicate. Avoid concurrent changes to the same client's contacts during this operation, since the PATCH uses the URLs read before creation.

For `update_client`, `contacts` must be a list of contact URLs; inline contact objects are rejected before any request. Include every URL you want to keep: MakeLeaps deletes existing contacts omitted from the list. Edit a contact's details with `update_contact` instead. The API rejects deletion of a contact referenced by a document; its error is returned through `MakeLeapsError`. Each contact currently supports one address, supplied in the `addresses` list.

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

Always change the document number (`document_number`), dates, and line items (`lineitems`) before creating. The same type and number fail the duplicate check; unchanged dates and line items repeat the original content.

Use `makeleaps documents create --file document.yaml --recipient-address client` to copy the client's organization contact address, or `--recipient-address contact` to copy the document's `client_contact` address. A missing selected address fails before creation. The seven recipient address fields described above are replaced; recipient names, phone numbers, and sender fields stay as supplied. Without `--recipient-address`, recipient and sender fields are sent as supplied. When writing YAML from scratch, include the recipient names and sender fields in the input, as the API does not fill them from `client`, `client_contact`, or company information. When reusing an existing document without an address selection, its recipient and sender fields are copied to the new document.

The created document is not linked to the document it was copied from: it has no source document, and the original does not list it as a converted document. The sender contact selection in the MakeLeaps web app remains empty (`<連絡先なし>` in Japanese); your company's contact appears only in the `sender_*` text fields.

```sh
makeleaps documents get example-document-mid > document.yaml
# Change document_number, dates, and lineitems in document.yaml before creating.
makeleaps documents create --file document.yaml
# Select the client's organization address, replacing the YAML recipient address.
makeleaps documents create --file document.yaml --recipient-address client
# To update an existing document, edit the desired fields and use its mid.
makeleaps documents update example-document-mid --file document.yaml
cat document.yaml | makeleaps documents update example-document-mid --file -
makeleaps documents delete example-document-mid --yes
```

Deletion requires `--yes` and produces no stdout output. Without `--yes`, the CLI fails before sending any request. Create and update reject invalid YAML or a top-level value other than a mapping before sending any request.

Convert a source document into a new type and number, or duplicate it using the same type; the command outputs the complete created document as YAML:

```sh
makeleaps documents convert example-quote-mid --type invoice --number EXAMPLE-INV-002
makeleaps documents convert example-invoice-mid --type deliveryslip --number EXAMPLE-DEL-002 --template example-template-code --date 2025-05-06 --language en
makeleaps documents convert example-quote-mid --type quote --number EXAMPLE-QUOTE-003 --date 2025-05-06 --deadline-days 14
makeleaps documents convert example-invoice-mid --type invoice --number EXAMPLE-INV-003 --deadline 2025-06-01
```

`--template` and `--date` override the template and issue date. Without them, conversion selects a template in the source template's language (the original template for a same-type copy) and uses today's date in Japan. `--deadline YYYY-MM-DD` and `--deadline-days INTEGER` are mutually exclusive and follow the library deadline rules above. Omitting them recalculates a compatible source deadline relative to the new issue date. Status dates are omitted. The new document and its source are not linked in MakeLeaps.

Use `--summary` on `documents get`, `create`, or `update` for compact YAML:

```sh
makeleaps documents get example-document-mid --summary
makeleaps documents create --file document.yaml --summary
makeleaps documents update example-document-mid --file changes.yaml --summary
```

The summary contains only `document_number`, `document_type`, `date`, the type's deadline (`date_due` or `date_valid`), `project_name`, `lineitems`, `subtotal`, `tax`, and `total`. Each line item contains only `kind`, `description`, `quantity`, `unit_cost`, and `price`. API field names and monetary strings are preserved; absent fields stay absent. Without `--summary`, these commands output the complete document. A summary is for reviewing content and totals; use the complete output when exporting YAML for reuse. Create and update report number conflicts with the existing document's MID on stderr and exit nonzero before writing.

Record a delivery made outside MakeLeaps and inspect sent status:

```sh
makeleaps documents mark-sent example-document-mid --date 2025-04-12 --method electronic
makeleaps documents mark-sent example-document-mid --date 2025-04-12T15:30:00+09:00 --method physical
makeleaps documents sent-status --type invoice --date-from 2025-01-01 --date-to 2025-12-31
makeleaps documents sent-status --filter sent=true --filter document_number=EXAMPLE-A --limit 10 --language en
```

`mark-sent` outputs the `mid`, saved `date_sent`, `archive_created`, and `archive` as YAML. For `orderslip` and `acceptance`, it reports `archive_created: false` and `archive: null`. If archive creation fails after the date is saved, the command exits nonzero and reports the document and saved date on stderr. Date-only and timezone-naive inputs use Japan time. `sent-status` outputs a YAML sequence with the `SentStatus` fields described above and accepts the same type, issue date, repeated `--filter`, `--limit`, and `--language` options as `documents list`. A limit of zero sends no requests.

Company, document, and archive results are written as complete YAML values to stdout unless a document summary is requested. Each list is a single YAML sequence, including when empty. Monetary values remain strings in YAML. PDF commands write the original bytes to files. Failures produce an explanatory message on stderr and a nonzero exit status; completed files from an interrupted or failed bulk export remain on disk.

Bulk PDF downloads create the destination directory if needed:

```sh
makeleaps documents pdf --dir ./current-pdfs --type invoice --date-from 2025-01-01 --filter sent=true
makeleaps documents list --client-name Example --project-name migration --type invoice
makeleaps documents sent-status --client-name Example --project-name migration
makeleaps documents pdf --dir ./project-pdfs --client-name Example --project-name migration
makeleaps archives pdf --dir ./sent-pdfs --limit 100
```

Documents accept the same filters for `list`, `sent-status`, and bulk `pdf`, including `--client-name` and `--project-name` for case-sensitive substrings. Either name option may be used alone; together they require both matches, alongside type, issue date, and repeated `--filter` conditions. These filters apply only to bulk PDF downloads with `--dir`. Archives support `--limit` and `--language`. Without `--limit`, list, sent status, and bulk PDF commands process every matching record; `--limit 0` processes none. `--language` is accepted on every command, and defaults to `ja`.

Bulk filenames use `<date>_<document_type>_<document_number>_<mid>.pdf`. Characters that cannot be used in portable filenames are replaced with `_`. A file with the same `mid` suffix already in the destination directory is skipped without fetching that PDF, even if its document number changed. The current document PDF and its archive PDF can differ; use separate directories for each source.

Client and contact commands use the same YAML input/output conventions. List commands support repeated `--filter KEY=VALUE` options and `--limit`. `contacts list --client MID` limits the results to one client. Creating a contact requires `--client MID`: `contacts create --client MID` adds the new contact to that client while keeping its existing contacts. Omitting `--client` fails before any request is sent.

```sh
makeleaps clients list --filter archived=false --limit 10
makeleaps clients get example-client-mid
# client.yaml contains contact URLs or inline contact data for client creation.
makeleaps clients create --file client.yaml
# client-update.yaml contains only fields to change; contacts must use URLs.
makeleaps clients update example-client-mid --file client-update.yaml
makeleaps contacts list --client example-client-mid --filter search=Example
makeleaps contacts get example-contact-mid
makeleaps contacts create --file contact.yaml --client example-client-mid
cat contact-update.yaml | makeleaps contacts update example-contact-mid --file -
makeleaps contacts delete example-contact-mid --yes
```

Contact deletion requires `--yes` and produces no stdout output. An attachment failure after contact creation produces a nonzero exit status and reports the created contact's `mid` on stderr. To delete or archive a client, use the MakeLeaps web app; there are no client deletion or archiving commands.

## Authentication and requests

Requests use HTTPS exclusively on `api.makeleaps.com`, with redirects disabled. Access tokens remain in process memory. Expired tokens and a 401 response cause reauthentication; a second 401 fails. API validation failures include the HTTP status, field names, and their error messages in `MakeLeapsError` and on CLI stderr, including `non_field_errors` for errors unrelated to one field. Nested errors include field names and zero-based row numbers separated by dots, such as `lineitems.0.has_jp_withholding_tax: Example withholding flag is required.` Empty objects for rows without errors are skipped. Unexpected error response shapes or responses without error messages produce a generic HTTP failure. Credentials and access tokens are redacted from the entire error message; raw response bodies are not included.

All requests on one client share a rolling limiter of 250 requests per 60 seconds and 20,000 per day, based on observed API response headers. Concurrent clients or other applications using the same credentials can consume the same server quota. A 429 response delays subsequent requests according to `Retry-After` (seconds or an HTTP date). OAuth token requests, GET, PATCH, and DELETE retry transport errors and server errors with exponential backoff. Document, client, contact, and Document Archive creation retry only 429 responses and failures to establish a connection (`ConnectError` or `ConnectTimeout`); other transport failures and server errors fail immediately to avoid repeating creation. A request has at most four attempts, and reauthentication after a 401 is attempted once. The HTTP timeout is 60 seconds.

No third-party MakeLeaps client is used. Tests replace the HTTP boundary with synthetic responses and never contact MakeLeaps:

```sh
uv run --python 3.12 pytest
uv run --python 3.13 pytest
uv build
```
