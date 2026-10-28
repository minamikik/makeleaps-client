"""Document number checks against synthetic MakeLeaps responses."""
import asyncio
import json

import httpx
import pytest
import yaml

from makeleaps_client import Document, MakeLeapsClient, MakeLeapsError
from makeleaps_client.cli import main
from conftest import BASE, envelope, resource
from test_conversion import conversion_backend, payloads


def numbers_backend(backend, pages, *, original_number='EXAMPLE-OLD'):
    previous = backend.override
    def override(request):
        path = request.url.path
        if request.method == 'GET' and path.endswith('/document/'):
            page = int(request.url.params.get('page', '1'))
            next_url = str(request.url.copy_set_param('page', page + 1)) if page < len(pages) else None
            return envelope(pages[page - 1], next=next_url)
        if request.method == 'GET' and path.endswith('/document/example-edit/'):
            return envelope({**resource('document'), 'mid': 'example-edit', 'document_number': original_number})
        if request.method in ('POST', 'PATCH') and '/document/' in path:
            return envelope({**resource('document'), **json.loads(request.content), 'mid': 'example-created'})
        if previous:
            return previous(request)
    backend.override = override


def queries(backend):
    return [r.url.params for r in backend.requests if r.method == 'GET' and r.url.path.endswith('/document/')]


def writes(backend):
    return [r for r in backend.requests if r.method in ('POST', 'PATCH') and '/document/' in r.url.path]


@pytest.mark.req('SP-MLC-067')
@pytest.mark.parametrize('as_model', [False, True])
@pytest.mark.parametrize('operation', ['create', 'update'])
def test_document_number_conflict_reports_the_other_mid_without_writing(backend, operation, as_model):
    """同じ種類と番号の別の書類があれば、その mid を返して作成と番号の変更を止める。"""
    numbers_backend(backend, [[{'mid': 'example-conflict', 'document_type': 'invoice', 'document_number': 'EXAMPLE-NEW'}]])
    data = {'document_type': 'invoice', 'document_number': 'EXAMPLE-NEW'}
    if as_model:
        data = Document.model_validate(data)
    async def scenario():
        async with MakeLeapsClient('example-id', 'example-secret', transport=httpx.MockTransport(backend)) as api:
            with pytest.raises(MakeLeapsError, match='example-conflict'):
                if operation == 'create':
                    await api.create_document(data)
                else:
                    await api.update_document('example-edit', data)
    asyncio.run(scenario())
    assert writes(backend) == []
    [query] = queries(backend)
    assert query['document_type'] == 'invoice'
    assert query['document_number'] == 'EXAMPLE-NEW'
    assert not any(r.url.path.endswith(('/client/', '/contact/')) for r in backend.requests)


@pytest.mark.req('SP-MLC-067')
def test_number_check_visits_later_pages_and_stops_at_the_first_conflict(backend):
    """番号の確認は後のページの重なりも検出し、発見後のページを読まない。"""
    numbers_backend(backend, [
        [{'mid': 'example-other-kind', 'document_type': 'quote', 'document_number': 'EXAMPLE-NEW'}],
        [{'mid': 'example-later-conflict', 'document_type': 'invoice', 'document_number': 'EXAMPLE-NEW'}],
        [{'mid': 'example-unneeded', 'document_type': 'invoice', 'document_number': 'EXAMPLE-NEW'}],
    ])
    async def scenario():
        async with MakeLeapsClient('example-id', 'example-secret', transport=httpx.MockTransport(backend)) as api:
            with pytest.raises(MakeLeapsError, match='example-later-conflict'):
                await api.create_document({'document_type': 'invoice', 'document_number': 'EXAMPLE-NEW'})
    asyncio.run(scenario())
    assert [q['page'] for q in queries(backend)] == ['1', '2']
    assert writes(backend) == []


@pytest.mark.req('SP-MLC-067')
def test_number_change_uses_the_current_type_and_ignores_the_document_itself(backend):
    """種類を含まない番号の変更は現在の種類で調べ、自分自身と別の種類や番号を重なりに数えない。"""
    numbers_backend(backend, [[
        {'mid': 'example-edit', 'document_type': 'invoice', 'document_number': 'EXAMPLE-NEW'},
        {'mid': 'example-quote', 'document_type': 'quote', 'document_number': 'EXAMPLE-NEW'},
        {'mid': 'example-other-number', 'document_type': 'invoice', 'document_number': 'EXAMPLE-OTHER'},
    ]])
    async def scenario():
        async with MakeLeapsClient('example-id', 'example-secret', transport=httpx.MockTransport(backend)) as api:
            document = await api.update_document('example-edit', {'document_number': 'EXAMPLE-NEW'})
            assert document.document_number == 'EXAMPLE-NEW'
    asyncio.run(scenario())
    [query] = queries(backend)
    assert query['document_type'] == 'invoice'
    assert query['document_number'] == 'EXAMPLE-NEW'
    assert json.loads(writes(backend)[0].content) == {'document_number': 'EXAMPLE-NEW'}


@pytest.mark.req('SP-MLC-067')
def test_number_check_applies_to_the_new_type_in_an_update(backend):
    """種類と番号を同時に変える時は、新しい種類の番号の重なりを調べる。"""
    numbers_backend(backend, [[{'mid': 'example-quote', 'document_type': 'quote', 'document_number': 'EXAMPLE-NEW'}]])
    async def scenario():
        async with MakeLeapsClient('example-id', 'example-secret', transport=httpx.MockTransport(backend)) as api:
            with pytest.raises(MakeLeapsError, match='example-quote'):
                await api.update_document('example-edit', {'document_type': 'quote', 'document_number': 'EXAMPLE-NEW'})
    asyncio.run(scenario())
    assert queries(backend)[0]['document_type'] == 'quote'
    assert writes(backend) == []


@pytest.mark.req('SP-MLC-067')
def test_unchanged_or_omitted_numbers_skip_the_list_after_a_checked_change(backend):
    """番号を変える編集だけが一覧を調べ、番号を保つ編集と番号を含まない編集は調べない。"""
    numbers_backend(backend, [[]])
    async def scenario():
        async with MakeLeapsClient('example-id', 'example-secret', transport=httpx.MockTransport(backend)) as api:
            await api.update_document('example-edit', {'document_number': 'EXAMPLE-NEW'})
            await api.update_document('example-edit', {'document_number': 'EXAMPLE-OLD'})
            await api.update_document('example-edit', {'title': 'Example revision'})
    asyncio.run(scenario())
    assert len(queries(backend)) == 1
    assert [json.loads(r.content) for r in writes(backend)] == [
        {'document_number': 'EXAMPLE-NEW'}, {'document_number': 'EXAMPLE-OLD'}, {'title': 'Example revision'},
    ]


@pytest.mark.req('SP-MLC-067')
def test_conversion_also_rejects_a_duplicate_number(backend):
    """変換で作る書類も同じ種類の番号が重なれば作成しない。"""
    conversion_backend(backend)
    numbers_backend(backend, [[{'mid': 'example-conflict', 'document_type': 'invoice', 'document_number': 'EXAMPLE-NEW'}]])
    async def scenario():
        async with MakeLeapsClient('example-id', 'example-secret', transport=httpx.MockTransport(backend)) as api:
            with pytest.raises(MakeLeapsError, match='example-conflict'):
                await api.convert_document('example-source', 'invoice', 'EXAMPLE-NEW')
    asyncio.run(scenario())
    assert payloads(backend) == []


@pytest.mark.req('SP-MLC-067')
@pytest.mark.parametrize('operation', ['create', 'update'])
def test_cli_duplicate_number_fails_with_the_conflicting_mid_on_stderr(cli_transport, capsys, tmp_path, operation):
    """cli は番号の重なりを標準エラーに mid とともに出し、書き込まない。"""
    numbers_backend(cli_transport, [[{'mid': 'example-conflict', 'document_type': 'invoice', 'document_number': 'EXAMPLE-NEW'}]])
    path = tmp_path / 'document.yaml'
    path.write_text(yaml.safe_dump({'document_type': 'invoice', 'document_number': 'EXAMPLE-NEW'}))
    args = ['documents', operation, *(['example-edit'] if operation == 'update' else []), '--file', str(path)]
    assert main(args) == 1
    output = capsys.readouterr()
    assert output.out == ''
    assert 'example-conflict' in output.err
    assert writes(cli_transport) == []


@pytest.mark.req('SP-MLC-067')
def test_number_check_failure_stops_creation_and_redacts_credentials(backend):
    """重なりを調べる API が失敗した時は作成せず、認証情報を除いた失敗を返す。"""
    backend.override = lambda r: httpx.Response(400, json={'response': {
        'document_number': ['Example check failed example-secret example-token-1'],
    }}) if r.method == 'GET' and r.url.path.endswith('/document/') else None
    async def scenario():
        async with MakeLeapsClient('example-id', 'example-secret', transport=httpx.MockTransport(backend)) as api:
            with pytest.raises(MakeLeapsError) as caught:
                await api.create_document({'document_type': 'invoice', 'document_number': 'EXAMPLE-NEW'})
            assert 'Example check failed' in str(caught.value)
            assert 'example-secret' not in str(caught.value)
            assert 'example-token-1' not in str(caught.value)
    asyncio.run(scenario())
    assert writes(backend) == []
