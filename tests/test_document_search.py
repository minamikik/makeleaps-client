"""Document searches using synthetic client and document list pages."""
import asyncio

import httpx
import pytest
import yaml

from makeleaps_client import MakeLeapsClient, MakeLeapsError
from makeleaps_client.cli import main
from conftest import BASE, PDF, envelope, resource


def search_backend(backend, *, page_size=100, search_results=True):
    clients = [
        {**resource('client'), 'mid': mid, 'url': BASE + f'client/{mid}/', 'display_name': name}
        for mid, name in [('example-alpha', 'Example Alpha'), ('example-beta', 'Example Beta'),
                          ('example-unrelated', 'Unrelated company')]
    ]
    documents = []
    for mid, owner, kind, project, day, sent in [
        ('example-a', 0, 'invoice', 'Example migration', '2025-05-05', True),
        ('example-b', 0, 'quote', 'Example migration', '2025-05-04', True),
        ('example-c', 1, 'invoice', 'Example audit', '2025-05-06', False),
        ('example-d', 1, 'invoice', 'Example migration phase', '2025-05-07', True),
        ('example-e', 2, 'invoice', None, '2025-05-08', False),
    ]:
        documents.append({
            **resource('document'), 'mid': mid, 'document_number': mid.upper(),
            'document_type': kind, 'project_name': project, 'date': day,
            'date_sent': day + 'T00:00:00+09:00' if sent else None,
            'client': clients[owner], 'client_contact': resource('contact'),
            'pdf_content_url': BASE + f'document/{mid}/pdf/',
        })
    def page(request, values):
        number = int(request.url.params.get('page', '1'))
        end = number * page_size
        next_url = str(request.url.copy_set_param('page', number + 1)) if end < len(values) else None
        return envelope(values[end - page_size:end], page=number, next=next_url)
    def override(request):
        if request.url.path.endswith('/client/'):
            return page(request, clients if 'search' not in request.url.params or search_results else [])
        if request.url.path.endswith('/document/'):
            query = request.url.params
            values = documents
            for key in ('document_type', 'client', 'document_number'):
                if key in query:
                    options = query.get_list(key)
                    values = [v for v in values if (v['client']['mid'] if key == 'client' else v[key]) in options]
            if 'date__gte' in query:
                values = [v for v in values if v['date'] >= query['date__gte']]
            if 'date__lte' in query:
                values = [v for v in values if v['date'] <= query['date__lte']]
            if 'sent' in query:
                values = [v for v in values if bool(v['date_sent']) == (query['sent'] == 'true')]
            return page(request, values)
        if request.url.path.endswith('/archive/'):
            return envelope([], next=None)
    backend.override = override


def client(backend):
    return MakeLeapsClient('example-id', 'example-secret', transport=httpx.MockTransport(backend))


@pytest.mark.req('SP-MLC-065')
@pytest.mark.parametrize('options,expected', [
    ({'client_name': 'Example'}, ['example-a', 'example-b', 'example-c', 'example-d']),
    ({'project_name': 'migration'}, ['example-a', 'example-b', 'example-d']),
    ({'client_name': 'Example', 'project_name': 'migration'}, ['example-a', 'example-b', 'example-d']),
])
def test_document_search_accepts_either_name_or_both(backend, options, expected):
    """取引先の名前と案件名は片方だけでも両方でも指定でき、両方の時は両方を満たす。"""
    search_backend(backend)
    async def scenario():
        async with client(backend) as api:
            rows = [d async for d in api.list_documents(**options)]
            assert [d.mid for d in rows] == expected
            assert all(d.client.display_name.startswith('Example') for d in rows)
    asyncio.run(scenario())
    searches = [r for r in backend.requests if 'search' in r.url.params]
    assert len(searches) == (1 if 'client_name' in options else 0)
    if searches:
        assert searches[0].url.path.endswith('/client/')
        assert searches[0].url.params['search'] == 'Example'
        queries = [r.url.params for r in backend.requests if r.url.path.endswith('/document/')]
        assert [q['client'] for q in queries] == ['example-alpha', 'example-beta']


@pytest.mark.req('SP-MLC-065')
def test_name_search_intersects_existing_filters_and_keeps_repeated_parameters(backend):
    """文字の指定は種類、日付、取引先、繰り返す番号、その他の条件のすべてと組み合わせる。"""
    search_backend(backend)
    async def scenario():
        async with client(backend) as api:
            rows = [d async for d in api.list_documents(
                client_name='Example', project_name='migration', document_type='invoice',
                date_from='2025-05-01', date_to='2025-05-06', filters=[
                    ('client', 'example-alpha'), ('sent', 'true'),
                    ('document_number', 'EXAMPLE-A'), ('document_number', 'EXAMPLE-D'),
                ],
            )]
            assert [d.mid for d in rows] == ['example-a']
    asyncio.run(scenario())
    [query] = [r.url.params for r in backend.requests if r.url.path.endswith('/document/')]
    assert query['client'] == 'example-alpha'
    assert query['document_type'] == 'invoice'
    assert query['date__gte'] == '2025-05-01' and query['date__lte'] == '2025-05-06'
    assert query['sent'] == 'true'
    assert query.get_list('document_number') == ['EXAMPLE-A', 'EXAMPLE-D']


@pytest.mark.req('SP-MLC-065')
def test_no_matching_client_returns_no_documents_without_reading_document_lists(backend):
    """取引先が見つからない時は書類の一覧を読まず空の結果を返す。"""
    search_backend(backend, search_results=False)
    async def scenario():
        async with client(backend) as api:
            assert [d async for d in api.list_documents(client_name='Example missing')] == []
    asyncio.run(scenario())
    assert not any('/document/' in r.url.path for r in backend.requests)


@pytest.mark.req('SP-MLC-065')
@pytest.mark.req('SP-MLC-007')
def test_search_stopping_after_one_document_does_not_read_more_clients_or_document_pages(backend):
    """検索した一覧の反復を止めた時は残りの取引先と書類のページを読まない。"""
    search_backend(backend, page_size=1)
    async def scenario():
        async with client(backend) as api:
            documents = api.list_documents(client_name='Example', project_name='migration')
            assert (await anext(documents)).mid == 'example-a'
            await documents.aclose()
            await asyncio.sleep(0)
    asyncio.run(scenario())
    lists = [r for r in backend.requests if r.url.path.endswith(('/client/', '/document/'))]
    assert len(lists) == 2
    assert all(r.url.params['page'] == '1' for r in lists)


@pytest.mark.req('SP-MLC-065')
def test_search_reads_subsequent_client_and_document_pages_when_needed(backend):
    """反復を続けると後の取引先と書類のページの一致も返す。"""
    search_backend(backend, page_size=1)
    async def scenario():
        async with client(backend) as api:
            assert [d.mid async for d in api.list_documents(client_name='Example', project_name='phase')] == ['example-d']
    asyncio.run(scenario())
    assert any(r.url.params.get('page') == '2' and r.url.params.get('client') == 'example-beta'
               for r in backend.requests)


@pytest.mark.req('SP-MLC-065')
def test_unmatched_projects_are_discarded_before_reading_their_references(backend):
    """案件名が一致しない書類の取引先や担当者は展開せず、空の結果を返す。"""
    backend.override = lambda r: envelope([
        {**resource('document'), 'project_name': name,
         'client': 'https://api.makeleaps.com/api/partner/example-company/client/unavailable/'}
        for name in (None, 'Other project')
    ], next=None) if r.url.path.endswith('/document/') else None
    async def scenario():
        async with client(backend) as api:
            assert [d async for d in api.list_documents(project_name='migration')] == []
    asyncio.run(scenario())
    assert not any('/client/' in r.url.path or '/contact/' in r.url.path for r in backend.requests)


@pytest.mark.req('SP-MLC-065')
@pytest.mark.parametrize('operation', ['list', 'sent-status', 'pdf'])
def test_cli_document_lists_status_and_bulk_pdf_share_name_filters(cli_transport, capsys, tmp_path, operation):
    """一覧、送付確認、PDF の一括保存は同じ取引先と案件名の文字の指定を受ける。"""
    search_backend(cli_transport, page_size=1)
    args = ['documents', operation, '--client-name', 'Example', '--project-name', 'migration',
            '--type', 'invoice', '--date-from', '2025-05-01', '--filter', 'sent=true', '--limit', '1']
    if operation == 'pdf':
        args += ['--dir', str(tmp_path)]
    assert main(args) == 0
    output = capsys.readouterr()
    assert output.err == ''
    if operation == 'pdf':
        [path] = tmp_path.iterdir()
        assert path.name.endswith('_example-a.pdf') and path.read_bytes() == PDF
        assert output.out == ''
    else:
        assert [v['mid'] for v in yaml.safe_load(output.out)] == ['example-a']
    assert not any(r.url.params.get('page', '1') != '1' for r in cli_transport.requests)


@pytest.mark.req('SP-MLC-065')
@pytest.mark.parametrize('option', ['--client-name', '--project-name'])
def test_single_pdf_rejects_name_filters_before_requests(cli_transport, capsys, tmp_path, option):
    """一件の PDF の取得は一覧だけに適用する文字の指定を request の前に拒否する。"""
    with pytest.raises(SystemExit) as caught:
        main(['documents', 'pdf', 'example-a', '--output', str(tmp_path / 'example.pdf'), option, 'Example'])
    assert caught.value.code == 2
    assert 'apply only to --dir' in capsys.readouterr().err
    assert cli_transport.requests == []


@pytest.mark.req('SP-MLC-065')
@pytest.mark.parametrize('stage', ['client', 'document'])
def test_search_api_failures_propagate_without_exposing_credentials(backend, stage):
    """取引先の検索と書類の一覧の失敗は、認証情報を除いて呼出元へ返す。"""
    search_backend(backend)
    search = backend.override
    def override(request):
        if request.url.path.endswith(f'/{stage}/'):
            return httpx.Response(400, json={'response': {'search': ['Example failure example-secret example-token-1']}})
        return search(request)
    backend.override = override
    async def scenario():
        async with client(backend) as api:
            with pytest.raises(MakeLeapsError) as caught:
                _ = [d async for d in api.list_documents(client_name='Example')]
            assert 'Example failure' in str(caught.value)
            assert 'example-secret' not in str(caught.value)
            assert 'example-token-1' not in str(caught.value)
    asyncio.run(scenario())
