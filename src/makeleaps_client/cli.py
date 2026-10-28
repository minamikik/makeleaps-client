"""Command-line interface built on the async library."""
from __future__ import annotations

import argparse
import asyncio
from datetime import date
import os
from pathlib import Path
import re
import sys
from typing import Any

import yaml

from .client import MakeLeapsClient, MakeLeapsError, _DOCUMENT_DEADLINES

_PDF_CONCURRENCY = 8


def _write_yaml(value):
    yaml.dump(value, sys.stdout, Dumper=getattr(yaml, 'CSafeDumper', yaml.SafeDumper),
              allow_unicode=True, sort_keys=False, default_flow_style=False)
    sys.stdout.flush()


def _record_output(record, *, summary=False):
    value = record.model_dump()
    if not summary:
        return value
    fields = ('document_number', 'document_type', 'date',
              _DOCUMENT_DEADLINES.get(value.get('document_type')), 'project_name',
              'lineitems', 'subtotal', 'tax', 'total')
    result = {key: value[key] for key in fields if key in value}
    if isinstance(result.get('lineitems'), list):
        result['lineitems'] = [
            {key: item[key] for key in ('kind', 'description', 'quantity', 'unit_cost', 'price') if key in item}
            for item in result['lineitems']
        ]
    return result


def _read_yaml(filename: str) -> dict[str, Any]:
    try:
        if filename == '-':
            value = yaml.safe_load(sys.stdin)
        else:
            with Path(filename).open(encoding='utf-8') as stream:
                value = yaml.safe_load(stream)
    except (yaml.YAMLError, UnicodeError):
        raise MakeLeapsError('Unable to read the input as valid YAML.') from None
    if not isinstance(value, dict):
        raise MakeLeapsError('The input YAML must contain a mapping at the top level.')
    return value


def _nonnegative(value: str) -> int:
    try:
        number = int(value)
        if number < 0:
            raise ValueError
        return number
    except ValueError:
        raise argparse.ArgumentTypeError('limit must be a nonnegative integer') from None


def _date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError('dates must use YYYY-MM-DD') from None


def _filter(value: str) -> tuple[str, str]:
    key, separator, val = value.partition('=')
    if not separator or not key:
        raise argparse.ArgumentTypeError('filters must use key=value')
    return key, val


def _list_options(parser, *, documents=False, filters=False):
    parser.add_argument('--limit', type=_nonnegative, help='Maximum number of records (default: all)')
    if documents:
        parser.add_argument('--type', dest='document_type', help='API document_type value')
        parser.add_argument('--date-from', type=_date, help='Inclusive issue date, YYYY-MM-DD')
        parser.add_argument('--date-to', type=_date, help='Inclusive issue date, YYYY-MM-DD')
        parser.add_argument('--client-name', help='Case-sensitive substring of the client name; searches clients first')
        parser.add_argument('--project-name', help='Case-sensitive substring of project_name; intersects all other filters')
    if documents or filters:
        parser.add_argument('--filter', action='append', type=_filter, default=[], metavar='KEY=VALUE', help='Pass an API query parameter; may be repeated')


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='makeleaps', description='Read MakeLeaps data and manage documents, clients, and contacts.')
    parser.add_argument('--language', default='ja', help='Response language (default: ja)')
    commands = parser.add_subparsers(dest='resource', required=True)
    partner = commands.add_parser('partner', help='Get the company associated with the API key')
    partner.add_argument('--language', default=argparse.SUPPRESS)
    for name in ('clients', 'contacts'):
        singular = name[:-1]
        group = commands.add_parser(name, help='List, get, create, and update clients' if name == 'clients'
                                    else 'List, get, create, update, and delete contacts')
        group.add_argument('--language', default=argparse.SUPPRESS)
        actions = group.add_subparsers(dest='operation', required=True)
        listing = actions.add_parser('list', help=f'List all matching {name}')
        listing.add_argument('--language', default=argparse.SUPPRESS)
        _list_options(listing, filters=True)
        if name == 'contacts':
            listing.add_argument('--client', metavar='MID', help='List only this client\'s contacts')
        get = actions.add_parser('get', help=f'Get a {singular}')
        get.add_argument('mid')
        get.add_argument('--language', default=argparse.SUPPRESS)
        for operation in ('create', 'update'):
            write = actions.add_parser(operation, help=f'{operation.capitalize()} a {singular} from YAML')
            if operation == 'update':
                write.add_argument('mid')
                if name == 'clients':
                    write.epilog = ('The contacts field accepts only contact URLs. Include every existing '
                                    'contact URL to keep; MakeLeaps deletes omitted contacts. '
                                    'Use contacts update to edit contact details.')
            elif name == 'contacts':
                write.description = 'Create a contact for the specified client, keeping existing contacts.'
                write.add_argument('--client', required=True, metavar='MID', help='Required client for the new contact')
            write.add_argument('--file', required=True, metavar='FILE', help='YAML input file, or - for stdin')
            write.add_argument('--language', default=argparse.SUPPRESS)
        if name == 'contacts':
            delete = actions.add_parser('delete', help='Delete a contact (requires --yes)')
            delete.add_argument('mid')
            delete.add_argument('--yes', action='store_true', help='Confirm contact deletion')
            delete.add_argument('--language', default=argparse.SUPPRESS)
    for name in ('documents', 'archives'):
        group = commands.add_parser(name, help='Read, create, convert, edit, or delete documents' if name == 'documents' else 'Read records saved at the time of sending')
        group.add_argument('--language', default=argparse.SUPPRESS)
        actions = group.add_subparsers(dest='operation', required=True)
        listing = actions.add_parser('list', help='List all matching records')
        listing.add_argument('--language', default=argparse.SUPPRESS)
        _list_options(listing, documents=name == 'documents')
        if name == 'documents':
            convert = actions.add_parser('convert', help='Copy a document into a new number and any type, including the same type')
            convert.description = (
                'Copy client, contact, recipient and sender fields, title, and line items '
                'into a new document; output the complete created document as YAML. '
                'Without --template, keep the source template language, preferring the same '
                'template code and then the same tax settings. If none matches the language, '
                'specify --template. The issue date defaults to today in Japan. Status dates '
                '(sent, paid, cancelled, accepted, confirmed, shipped, and payment dates) '
                'are not copied. For a same-type copy, keep the source template. '
                'Set a deadline with --deadline or --deadline-days. Without either, '
                'preserve the days from issue date to deadline only when both types use '
                'the same deadline field (date_due for invoices; date_valid for quotes '
                'and orders); otherwise omit it. Types without a deadline reject these '
                'options. The API does not link the new '
                'document to its source or mark the source as converted.'
            )
            convert.add_argument('mid', help='Source document MID')
            convert.add_argument('--type', required=True, dest='document_type', help='New API document_type value')
            convert.add_argument('--number', required=True, dest='document_number', help='New document number')
            convert.add_argument('--template', help='Template code for the new document')
            convert.add_argument('--date', type=_date, help='Issue date, YYYY-MM-DD (default: today in Japan)')
            deadline = convert.add_mutually_exclusive_group()
            deadline.add_argument('--deadline', type=_date, help='New deadline, YYYY-MM-DD')
            deadline.add_argument('--deadline-days', type=int, help='Days from the new issue date to the deadline')
            convert.add_argument('--language', default=argparse.SUPPRESS)
            mark_sent = actions.add_parser('mark-sent', help='Record delivery outside MakeLeaps and create its archive')
            mark_sent.description = (
                'Save the sent date, then create an archive of delivery outside MakeLeaps. '
                'Order slips (orderslip) and acceptance certificates (acceptance) receive '
                'only the date and report archive_created: false. The archive timestamp '
                'is the API request receipt time. If archiving fails after saving, the '
                'error identifies the document and saved date; check MakeLeaps before retrying.'
            )
            mark_sent.add_argument('mid')
            mark_sent.add_argument('--date', required=True, dest='date_sent', help='ISO sent date or datetime; dates and naive times use Japan time')
            mark_sent.add_argument('--method', required=True, choices=('electronic', 'physical'), help='How the document was delivered outside MakeLeaps')
            mark_sent.add_argument('--language', default=argparse.SUPPRESS)
            status = actions.add_parser('sent-status', help='List sent dates and matching archives of delivery outside MakeLeaps')
            status.description = (
                'Output mid, document_type, document_number, date_sent, and has_external_archive '
                'as YAML. Archives match by document type and number and must have an outside '
                'MakeLeaps legal category. Duplicate type/number pairs cannot be distinguished.'
            )
            status.add_argument('--language', default=argparse.SUPPRESS)
            _list_options(status, documents=True)
            get = actions.add_parser('get', help='Get a document with its client and contact')
            get.add_argument('mid')
            get.add_argument('--language', default=argparse.SUPPRESS)
            get.add_argument('--summary', action='store_true', help='Output only number, type, issue/deadline dates, project, line items, subtotal, tax, and total as YAML')
            for operation in ('create', 'update'):
                write = actions.add_parser(operation, help=f'{operation.capitalize()} a document from YAML')
                write.description = (
                    'Create a document from YAML. Fail with the existing document MID if '
                    'the same document type and number already exist.' if operation == 'create' else
                    'Update supplied fields from YAML. Before changing document_number, '
                    'check for another document with the same type and new number; '
                    'fail with its MID if found. An unchanged or omitted number skips this check.'
                )
                write.add_argument('--summary', action='store_true', help='Output only number, type, issue/deadline dates, project, line items, subtotal, tax, and total as YAML')
                if operation == 'create':
                    write.add_argument('--recipient-address', choices=('client', 'contact'),
                                       help='Copy the client organization address or client_contact address; fail if unavailable')
                    write.formatter_class = argparse.RawDescriptionHelpFormatter
                    write.epilog = (
                        'Create a new document by exporting an existing document and editing the YAML:\n\n'
                        '  makeleaps documents get example-document-mid > document.yaml\n'
                        '  # Change document_number, dates, and lineitems before creating.\n'
                        '  makeleaps documents create --file document.yaml\n\n'
                        'Always change the document number (document_number), dates, and line items\n'
                        '(lineitems) before creating. The same type and number fail the duplicate\n'
                        'check; unchanged dates and line items repeat the original content.\n\n'
                        'For creation and updates with line items, makeleaps-client sets\n'
                        'autocalculate to true. MakeLeaps calculates normal and subtotal line item\n'
                        'amounts, subtotals, taxes, and totals from unit costs and quantities. Input\n'
                        'values for these calculated fields are omitted from requests; prices on\n'
                        'other line item kinds, such as simple, are preserved. For updates without\n'
                        'line items, total fields and autocalculate are sent as supplied; MakeLeaps\n'
                        'does not recalculate totals.\n\n'
                        'Use --recipient-address client to copy the address of the client\n'
                        'organization contact, or --recipient-address contact to copy the\n'
                        'client_contact address. A missing selected address fails before creation.\n'
                        'Only recipient address fields are replaced; names and phone numbers stay\n'
                        'as supplied. Without --recipient-address, recipient address fields stay\n'
                        'as supplied too.\n\n'
                        'Without that option, the MakeLeaps API and makeleaps-client do not fill recipient_* fields\n'
                        '(e.g. recipient_contact_name, recipient_postal_code) or sender_* fields\n'
                        '(e.g. sender_contact_name, sender_postal_code) from client, client_contact,\n'
                        'or your company information. When writing YAML from scratch, include these\n'
                        'fields in the input. When reusing an existing document, its recipient and\n'
                        'sender fields are copied to the new document.'
                    )
                if operation == 'update':
                    write.add_argument('mid')
                write.add_argument('--file', required=True, metavar='FILE', help='YAML input file, or - for stdin')
                write.add_argument('--language', default=argparse.SUPPRESS)
            delete = actions.add_parser('delete', help='Delete a document (requires --yes)')
            delete.add_argument('mid')
            delete.add_argument('--yes', action='store_true', help='Confirm document deletion')
            delete.add_argument('--language', default=argparse.SUPPRESS)
        pdf = actions.add_parser('pdf', help='Save one PDF, or all matching PDFs with --dir')
        pdf.add_argument('mid', nargs='?')
        pdf.add_argument('--language', default=argparse.SUPPRESS)
        destinations = pdf.add_mutually_exclusive_group(required=True)
        destinations.add_argument('--output', type=Path, metavar='FILE')
        destinations.add_argument('--dir', type=Path, metavar='DIRECTORY')
        _list_options(pdf, documents=name == 'documents')
    return parser


def _safe_component(value: Any) -> str:
    # Include Windows filename restrictions so exported names remain portable.
    return re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '_', str(value)).rstrip(' .')


def _filename(record) -> str:
    if not record.mid:
        raise MakeLeapsError('A PDF record is missing its mid.')
    values = (record.date or 'unknown', record.document_type or 'unknown',
              record.document_number or '', record.mid)
    return '_'.join(_safe_component(value) for value in values) + '.pdf'


async def _records(api, args):
    if args.resource == 'documents':
        listing = api.list_sent_status if args.operation == 'sent-status' else api.list_documents
        iterator = listing(document_type=args.document_type,
                           date_from=args.date_from, date_to=args.date_to,
                           client_name=args.client_name, project_name=args.project_name,
                           filters=args.filter)
    elif args.resource == 'clients':
        iterator = api.list_clients(filters=args.filter)
    elif args.resource == 'contacts':
        iterator = api.list_contacts(client_mid=args.client, filters=args.filter)
    else:
        iterator = api.list_archives()
    count = 0
    try:
        if args.limit == 0:
            return
        async for record in iterator:
            yield record
            count += 1
            if args.limit is not None and count >= args.limit:
                return
    finally:
        await iterator.aclose()


async def _execute(args, client_id, client_secret):
    if getattr(args, 'operation', None) in ('create', 'update'):
        data = _read_yaml(args.file)
    async with MakeLeapsClient(client_id, client_secret, language=args.language) as api:
        if args.resource == 'partner':
            return (await api.get_partner()).model_dump()
        singular = args.resource[:-1]
        if args.operation == 'convert':
            return (await api.convert_document(args.mid, args.document_type, args.document_number,
                                               template=args.template, date=args.date,
                                               deadline=args.deadline, deadline_days=args.deadline_days)).model_dump()
        if args.operation == 'mark-sent':
            return (await api.mark_document_sent(args.mid, args.date_sent, args.method)).model_dump()
        if args.operation == 'get':
            return _record_output(await getattr(api, f'get_{singular}')(args.mid),
                                  summary=getattr(args, 'summary', False))
        if args.operation == 'create':
            if args.resource == 'contacts':
                return (await api.create_contact(args.client, data)).model_dump()
            if args.resource == 'documents':
                return _record_output(await api.create_document(data, recipient_address=args.recipient_address),
                                      summary=args.summary)
            return (await getattr(api, f'create_{singular}')(data)).model_dump()
        if args.operation == 'update':
            return _record_output(await getattr(api, f'update_{singular}')(args.mid, data),
                                  summary=getattr(args, 'summary', False))
        if args.operation == 'delete':
            await getattr(api, f'delete_{singular}')(args.mid)
            return None
        if args.operation in ('list', 'sent-status'):
            empty = True
            records = _records(api, args)
            try:
                async for record in records:
                    _write_yaml([record.model_dump()])
                    empty = False
            finally:
                await records.aclose()
            if empty:
                _write_yaml([])
            return None
        fetch = api.get_document_pdf if args.resource == 'documents' else api.get_archive_pdf
        if args.mid:
            args.output.write_bytes(await fetch(args.mid))
            return None
        directory = args.dir
        directory.mkdir(parents=True, exist_ok=True)
        existing = {path.name for path in directory.iterdir() if path.is_file()}
        async def save(record, filename):
            if record.pdf_content_url:
                content = (await api._request(record.pdf_content_url)).content
            else:
                content = await fetch(record.mid)
            (directory / filename).write_bytes(content)

        pending = set()
        records = _records(api, args)
        try:
            async for record in records:
                filename = _filename(record)
                suffix = '_' + _safe_component(record.mid) + '.pdf'
                if any(name.endswith(suffix) for name in existing):
                    continue
                pending.add(asyncio.create_task(save(record, filename)))
                # Reserve the mid before another row can schedule the same PDF.
                existing.add(filename)
                if len(pending) >= _PDF_CONCURRENCY:
                    done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    await asyncio.gather(*done)
            await asyncio.gather(*pending)
        finally:
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await records.aclose()
        return None


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if getattr(args, 'operation', None) == 'delete' and not args.yes:
        print(f'{args.resource[:-1].capitalize()} deletion requires --yes.', file=sys.stderr)
        return 1
    if getattr(args, 'operation', None) == 'pdf':
        if args.mid and args.dir:
            parser.error('use either MID with --output, or --dir without MID')
        if not args.mid and args.output:
            parser.error('--output requires MID')
        document_filters = args.resource == 'documents' and (
            bool(args.filter) or any(value is not None for value in (
                args.document_type, args.date_from, args.date_to, args.client_name, args.project_name,
            ))
        )
        if args.mid and (args.limit is not None or document_filters):
            parser.error('filters and --limit apply only to --dir')
    if args.resource == 'documents' and getattr(args, 'date_from', None) and getattr(args, 'date_to', None):
        if args.date_from > args.date_to:
            parser.error('--date-from must not be later than --date-to')
    client_id = os.environ.get('MAKELEAPS_CLIENT_ID')
    client_secret = os.environ.get('MAKELEAPS_CLIENT_SECRET')
    if not client_id or not client_secret:
        print('Set both MAKELEAPS_CLIENT_ID and MAKELEAPS_CLIENT_SECRET.', file=sys.stderr)
        return 1
    try:
        result = asyncio.run(_execute(args, client_id, client_secret))
        if result is not None:
            _write_yaml(result)
        return 0
    except MakeLeapsError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError:
        print('Unable to read or write the requested file.', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Interrupted.', file=sys.stderr)
        return 130
