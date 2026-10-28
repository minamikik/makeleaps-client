"""Recording delivery outside MakeLeaps, using only synthetic HTTP responses."""
import asyncio
from datetime import date, datetime
import json

import httpx
import pytest
import yaml

from makeleaps_client import MakeLeapsClient, MakeLeapsError
from makeleaps_client.cli import main
from conftest import BASE, envelope, resource


def client(backend):
    return MakeLeapsClient('example-id', 'example-secret', transport=httpx.MockTransport(backend))


def sent_backend(backend, *, kind='invoice'):
    document = {**resource('document'), 'document_type': kind}
    archive = {**resource('archive'), 'legal_categories': ['electronically_outside_makeleaps']}
    def override(request):
        if request.method == 'PATCH' and '/document/' in request.url.path:
            document.update(json.loads(request.content))
            return envelope(document)
        if request.method == 'POST' and request.url.path.endswith('/archive/create/'):
            return envelope(archive)
    backend.override = override
    return document, archive


@pytest.mark.req('SP-MLC-059')
@pytest.mark.parametrize('kind', ['invoice', 'quote', 'order', 'deliveryslip', 'future-kind'])
@pytest.mark.parametrize('method,api_method', [
    ('electronic', 'manually_sent_electronically'),
    ('physical', 'manually_sent_physically'),
])
def test_mark_sent_updates_only_sent_date_before_creating_the_archive(backend, kind, method, api_method):
    """電子と紙の送付は、種類を限らず送付日を保存してから保存記録を作る。"""
    document, archive = sent_backend(backend, kind=kind)
    async def scenario():
        async with client(backend) as api:
            result = await api.mark_document_sent('example-document', '2025-05-06', method)
            assert result.mid == 'example-document'
            assert result.date_sent == datetime.fromisoformat('2025-05-06T00:00:00+09:00')
            assert result.archive_created is True
            assert result.archive.model_dump() == archive
    asyncio.run(scenario())
    writes = [r for r in backend.requests if r.method in ('PATCH', 'POST') and '/api/partner/' in r.url.path]
    assert [(r.method, str(r.url)) for r in writes] == [
        ('PATCH', BASE + 'document/example-document/'), ('POST', BASE + 'archive/create/'),
    ]
    assert json.loads(writes[0].content) == {'date_sent': '2025-05-06T00:00:00+09:00'}
    assert json.loads(writes[1].content) == {'document_mid': 'example-document', 'sending_method': api_method}
    assert document['date_sent'] != archive['date_sent']


@pytest.mark.req('SP-MLC-059')
@pytest.mark.parametrize('kind', ['orderslip', 'acceptance'])
def test_documents_without_archive_support_still_receive_a_sent_date(backend, kind):
    """発注請書と検収書は送付日だけを保存し、保存記録が無いことを返す。"""
    sent_backend(backend, kind=kind)
    async def scenario():
        async with client(backend) as api:
            result = await api.mark_document_sent('example-document', date(2025, 5, 6), 'physical')
            assert result.date_sent == datetime.fromisoformat('2025-05-06T00:00:00+09:00')
            assert result.archive_created is False
            assert result.archive is None
    asyncio.run(scenario())
    assert sum(r.method == 'PATCH' for r in backend.requests) == 1
    assert not any('/archive/' in r.url.path for r in backend.requests)


@pytest.mark.req('SP-MLC-059')
@pytest.mark.parametrize('value,expected', [
    (datetime.fromisoformat('2025-05-06T12:34:56+02:00'), '2025-05-06T12:34:56+02:00'),
    ('2025-05-06T12:34:56Z', '2025-05-06T12:34:56+00:00'),
    ('2025-05-06T12:34:56', '2025-05-06T12:34:56+09:00'),
])
def test_mark_sent_preserves_supplied_time_and_assumes_japan_for_naive_times(backend, value, expected):
    """日時の指定では時刻と offset を保ち、offset が無い時は日本時間にする。"""
    sent_backend(backend)
    async def scenario():
        async with client(backend) as api:
            await api.mark_document_sent('example-document', value, 'electronic')
    asyncio.run(scenario())
    assert json.loads(next(r for r in backend.requests if r.method == 'PATCH').content) == {'date_sent': expected}


@pytest.mark.req('SP-MLC-059')
@pytest.mark.parametrize('value,method', [('invalid', 'electronic'), ('2025-02-30', 'physical'), ('2025-05-06', 'fax')])
def test_invalid_delivery_inputs_cannot_partially_change_a_document(backend, value, method):
    """送付日と手段が不正な時は、書類を変える request を送らない。"""
    async def scenario():
        async with client(backend) as api:
            with pytest.raises(MakeLeapsError):
                await api.mark_document_sent('example-document', value, method)
    asyncio.run(scenario())
    assert backend.requests == []


@pytest.mark.req('SP-MLC-059')
@pytest.mark.parametrize('failure', ['validation', 'timeout', 'server', 'invalid_response', 'missing_archive_mid'])
def test_archive_failure_reports_the_saved_date_without_repeating_uncertain_creation(backend, failure):
    """保存記録の失敗は保存済みの送付日を知らせ、結果が不明な作成を繰り返さない。"""
    sent_backend(backend)
    original = backend.override
    def override(request):
        if request.url.path.endswith('/archive/create/'):
            if failure == 'validation':
                return httpx.Response(400, json={'response': {'document_mid': ['Example archive rejected. example-secret']}})
            if failure == 'timeout':
                raise httpx.ReadTimeout('synthetic timeout', request=request)
            if failure == 'invalid_response':
                return httpx.Response(200, content=b'not JSON')
            if failure == 'missing_archive_mid':
                return envelope({})
            return httpx.Response(503)
        return original(request)
    backend.override = override
    async def scenario():
        async with client(backend) as api:
            with pytest.raises(MakeLeapsError) as caught:
                await api.mark_document_sent('example-document', '2025-05-06', 'electronic')
            message = str(caught.value)
            assert 'example-document' in message and '2025-05-06' in message
            assert 'date_sent' in message and 'archive' in message.lower()
            assert 'example-secret' not in message
            if failure in ('timeout', 'server'):
                assert 'may have been created' in message
                assert 'the create may have' not in message
    asyncio.run(scenario())
    assert sum(r.method == 'PATCH' for r in backend.requests) == 1
    assert sum(r.url.path.endswith('/archive/create/') for r in backend.requests) == 1


@pytest.mark.req('SP-MLC-059')
def test_a_rejected_sent_date_does_not_create_an_archive(backend):
    """送付日の保存を API が拒んだ時は、保存記録を作らない。"""
    backend.override = lambda r: httpx.Response(400, json={'response': {'date_sent': ['Example date rejected.']}}) if r.method == 'PATCH' else None
    async def scenario():
        async with client(backend) as api:
            with pytest.raises(MakeLeapsError, match='date_sent.*Example date rejected'):
                await api.mark_document_sent('example-document', '2025-05-06', 'electronic')
    asyncio.run(scenario())
    assert not any('/archive/' in r.url.path for r in backend.requests)


@pytest.mark.req('SP-MLC-060')
def test_sent_status_matches_type_number_and_external_legal_category_across_pages(backend):
    """送付状態は種類と番号と外部送付の区分で照合し、保存記録の全ページを読む。"""
    documents = [
        {**resource('document'), 'mid': f'example-{i}', 'document_type': kind,
         'document_number': number, 'date_sent': sent}
        for i, (kind, number, sent) in enumerate([
            ('invoice', 'A', '2025-05-06T01:02:03Z'),
            ('quote', 'A', None), ('invoice', 'B', None),
            ('invoice', 'C', None), ('invoice', 'D', None), ('invoice', 'A', None),
        ])
    ]
    archives = [
        {**resource('archive'), 'document_type': kind, 'document_number': number,
         'legal_categories': categories, 'sending_methods': ['manually_sent_electronically']}
        for kind, number, categories in [
            ('invoice', 'A', ['electronically_outside_makeleaps']),
            ('quote', 'A', ['electronically_from_makeleaps']),
            ('quote', 'B', ['physically_outside_makeleaps']),
            ('invoice', 'C', ['physically_outside_makeleaps']),
        ]
    ]
    def override(request):
        if request.url.path.endswith('/document/'):
            return envelope(documents, count=len(documents), page=1, next=None)
        if request.url.path.endswith('/archive/'):
            page = int(request.url.params['page'])
            return envelope(archives[:3] if page == 1 else archives[3:], count=4, page=page,
                            next=f'{BASE}archive/?page=2&page_size=3' if page == 1 else None)
    backend.override = override
    async def scenario():
        async with client(backend) as api:
            rows = [row async for row in api.list_sent_status(document_type='invoice', date_from='2025-01-01',
                date_to=date(2025, 12, 31), filters=[('sent', 'true'), ('document_number', 'A'), ('document_number', 'B')])]
            assert [row.has_external_archive for row in rows] == [True, False, False, True, False, True]
            assert rows[0].date_sent == datetime.fromisoformat('2025-05-06T01:02:03+00:00')
            assert rows[1].date_sent is None
            assert rows[0].model_dump() == {'mid': 'example-0', 'document_type': 'invoice', 'document_number': 'A',
                'date_sent': '2025-05-06T01:02:03Z', 'has_external_archive': True}
    asyncio.run(scenario())
    query = next(r for r in backend.requests if r.url.path.endswith('/document/')).url.params
    assert query['document_type'] == 'invoice'
    assert query['date__gte'] == '2025-01-01' and query['date__lte'] == '2025-12-31'
    assert query.get_list('document_number') == ['A', 'B'] and query['sent'] == 'true'
    assert sum(r.url.path.endswith('/archive/') for r in backend.requests) == 2


@pytest.mark.req('SP-MLC-060')
def test_empty_document_status_does_not_read_archives(backend):
    """条件に合う書類が無い時は、空の状態を返し保存記録を読まない。"""
    backend.document_count = 0
    async def scenario():
        async with client(backend) as api:
            assert [row async for row in api.list_sent_status()] == []
    asyncio.run(scenario())
    assert not any('/archive/' in r.url.path for r in backend.requests)


@pytest.mark.req('SP-MLC-061')
@pytest.mark.parametrize('kind,created', [('quote', True), ('orderslip', False)])
def test_cli_mark_sent_outputs_the_saved_date_and_archive_result(cli_transport, capsys, kind, created):
    """cli は保存した送付日と、保存記録を作ったかどうかを YAML で返す。"""
    sent_backend(cli_transport, kind=kind)
    assert main(['documents', 'mark-sent', 'example-document', '--date', '2025-05-06', '--method', 'physical', '--language', 'en']) == 0
    output = capsys.readouterr()
    row = yaml.safe_load(output.out)
    assert output.err == ''
    assert row['mid'] == 'example-document' and row['date_sent'] == '2025-05-06T00:00:00+09:00'
    assert row['archive_created'] is created
    assert row['archive'] is not None if created else row['archive'] is None
    assert all(r.headers['accept-language'] == 'en' for r in cli_transport.requests)


@pytest.mark.req('SP-MLC-061')
def test_cli_sent_status_uses_list_filters_limit_and_yaml(cli_transport, capsys):
    """cli は書類の一覧と同じ絞り込みと言語と上限を送付状態に使う。"""
    cli_transport.document_count = 201
    assert main(['documents', 'sent-status', '--type', 'quote', '--date-from', '2025-01-01', '--date-to', '2025-12-31',
                 '--filter', 'sent=true', '--filter', 'document_number=A', '--filter', 'document_number=B', '--limit', '2', '--language', 'en']) == 0
    output = capsys.readouterr()
    assert output.err == ''
    rows = yaml.safe_load(output.out)
    assert len(rows) == 2 and all(row['has_external_archive'] is False for row in rows)
    query = next(r for r in cli_transport.requests if r.url.path.endswith('/document/')).url.params
    assert query['document_type'] == 'quote' and query.get_list('document_number') == ['A', 'B']
    assert query['date__gte'] == '2025-01-01' and query['date__lte'] == '2025-12-31' and query['sent'] == 'true'
    assert not any(r.url.params.get('page', '1') != '1' for r in cli_transport.requests)
    assert all(r.headers['accept-language'] == 'en' for r in cli_transport.requests)


@pytest.mark.req('SP-MLC-061')
def test_cli_zero_status_limit_outputs_empty_yaml_without_requests(cli_transport, capsys):
    """送付状態の上限が零なら、request を送らず空の YAML の列を返す。"""
    assert main(['documents', 'sent-status', '--limit', '0']) == 0
    assert yaml.safe_load(capsys.readouterr().out) == []
    assert cli_transport.requests == []


@pytest.mark.req('SP-MLC-061')
@pytest.mark.parametrize('args,reason', [
    (['mark-sent', 'example-document', '--method', 'electronic'], 'required: --date'),
    (['mark-sent', 'example-document', '--date', '2025-05-06'], 'required: --method'),
    (['mark-sent', 'example-document', '--date', '2025-05-06', '--method', 'fax'], "argument --method: invalid choice: 'fax'"),
    (['sent-status', '--date-from', '2025-05-06', '--date-to', '2025-01-01'], '--date-from must not be later than --date-to'),
])
def test_cli_rejects_incomplete_delivery_arguments_before_requests(cli_transport, capsys, args, reason):
    """cli は送付の必須引数と手段と日付範囲の誤りを request の前に知らせる。"""
    with pytest.raises(SystemExit) as caught:
        main(['documents', *args])
    assert caught.value.code == 2
    assert reason in capsys.readouterr().err
    assert cli_transport.requests == []


@pytest.mark.req('SP-MLC-061')
def test_cli_reports_archive_failure_with_saved_date_on_stderr(cli_transport, capsys):
    """cli は保存記録の作成に失敗した時、書類と保存済みの送付日を標準エラーへ出す。"""
    sent_backend(cli_transport)
    original = cli_transport.override
    cli_transport.override = lambda r: httpx.Response(400, json={'response': {'document_mid': ['Example failure.']}}) if '/archive/create/' in r.url.path else original(r)
    assert main(['documents', 'mark-sent', 'example-document', '--date', '2025-05-06', '--method', 'electronic']) == 1
    output = capsys.readouterr()
    assert output.out == '' and 'example-document' in output.err and '2025-05-06' in output.err
