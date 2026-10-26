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

from .client import MakeLeapsClient, MakeLeapsError


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


def _list_options(parser, *, documents=False):
    parser.add_argument('--limit', type=_nonnegative, help='Maximum number of records (default: all)')
    if documents:
        parser.add_argument('--type', dest='document_type', help='API document_type value')
        parser.add_argument('--date-from', type=_date, help='Inclusive issue date, YYYY-MM-DD')
        parser.add_argument('--date-to', type=_date, help='Inclusive issue date, YYYY-MM-DD')
        parser.add_argument('--filter', action='append', type=_filter, default=[], metavar='KEY=VALUE', help='Pass an API query parameter; may be repeated')


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='makeleaps', description='Read MakeLeaps company data, documents, archives, and PDFs.')
    parser.add_argument('--language', default='ja', help='Response language (default: ja)')
    commands = parser.add_subparsers(dest='resource', required=True)
    partner = commands.add_parser('partner', help='Get the company associated with the API key')
    partner.add_argument('--language', default=argparse.SUPPRESS)
    for name in ('documents', 'archives'):
        group = commands.add_parser(name, help='Read documents' if name == 'documents' else 'Read records saved at the time of sending')
        group.add_argument('--language', default=argparse.SUPPRESS)
        actions = group.add_subparsers(dest='operation', required=True)
        listing = actions.add_parser('list', help='List all matching records')
        listing.add_argument('--language', default=argparse.SUPPRESS)
        _list_options(listing, documents=name == 'documents')
        if name == 'documents':
            get = actions.add_parser('get', help='Get a document with its client and contact')
            get.add_argument('mid')
            get.add_argument('--language', default=argparse.SUPPRESS)
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
        iterator = api.list_documents(document_type=args.document_type,
                                      date_from=args.date_from, date_to=args.date_to,
                                      filters=args.filter)
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
    async with MakeLeapsClient(client_id, client_secret, language=args.language) as api:
        if args.resource == 'partner':
            return (await api.get_partner()).model_dump()
        if args.operation == 'get':
            return (await api.get_document(args.mid)).model_dump()
        if args.operation == 'list':
            return [record.model_dump() async for record in _records(api, args)]
        fetch = api.get_document_pdf if args.resource == 'documents' else api.get_archive_pdf
        if args.mid:
            args.output.write_bytes(await fetch(args.mid))
            return None
        directory = args.dir
        directory.mkdir(parents=True, exist_ok=True)
        existing = {path.name for path in directory.iterdir() if path.is_file()}
        records = _records(api, args)
        try:
            async for record in records:
                filename = _filename(record)
                suffix = '_' + _safe_component(record.mid) + '.pdf'
                if any(name.endswith(suffix) for name in existing):
                    continue
                (directory / filename).write_bytes(await fetch(record.mid))
                existing.add(filename)
        finally:
            await records.aclose()
        return None


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if getattr(args, 'operation', None) == 'pdf':
        if args.mid and args.dir:
            parser.error('use either MID with --output, or --dir without MID')
        if not args.mid and args.output:
            parser.error('--output requires MID')
        if args.mid and (args.limit is not None or (args.resource == 'documents' and any((args.document_type, args.date_from, args.date_to, args.filter)))):
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
            print(yaml.safe_dump(result, allow_unicode=True, sort_keys=False), end='')
        return 0
    except MakeLeapsError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError:
        print('Unable to read or write the requested output file.', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Interrupted.', file=sys.stderr)
        return 130
