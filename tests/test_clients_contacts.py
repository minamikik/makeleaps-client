"""Client and contact operations against synthetic HTTP responses only."""
import asyncio
from copy import deepcopy
import io
import json
from types import MappingProxyType

import httpx
import pytest
import yaml

from makeleaps_client import Client, Contact, MakeLeapsClient, MakeLeapsError
from makeleaps_client.cli import main
from conftest import BASE, envelope, resource


@pytest.fixture
def parties(backend):
    records = {kind: [resource(kind)] for kind in ('client', 'contact')}

    def handle(request):
        path = request.url.path.removeprefix('/api/partner/example-company/').strip('/').split('/')
        kind = path[0]
        if kind not in records:
            return None
        if request.method == 'DELETE':
            return httpx.Response(204)
        if request.method in ('POST', 'PATCH'):
            value = {**resource(kind), **json.loads(request.content)}
            value.update(mid=f'example-saved-{kind}', url=f'{BASE}{kind}/example-saved-{kind}/')
            return envelope(value)
        if len(path) == 2:
            return envelope({**records[kind][0], 'mid': path[1], 'url': f'{BASE}{kind}/{path[1]}/'})
        if len(path) == 3 and path[2] == 'contact':
            kind = 'contact'
        page = int(request.url.params.get('page', 1))
        size = int(request.url.params.get('page_size', 100))
        start = (page - 1) * size
        next_url = str(request.url.copy_set_param('page', page + 1)) if start + size < len(records[kind]) else None
        return envelope(records[kind][start:start + size], count=len(records[kind]), page=page, next=next_url)

    backend.override = handle
    return records


def api(backend):
    return MakeLeapsClient('example-id', 'example-secret', transport=httpx.MockTransport(backend))


@pytest.mark.req('SP-MLC-053')
@pytest.mark.req('SP-MLC-055')
@pytest.mark.req('SP-MLC-009')
@pytest.mark.parametrize('kind,model', [('client', Client), ('contact', Contact)])
def test_get_returns_complete_permissive_resource(backend, parties, kind, model):
    """取引先と連絡先を mid で取得し、未知の field も model に残す。"""
    parties[kind][0]['future_field'] = {'example': True}
    async def scenario():
        async with api(backend) as connection:
            result = await getattr(connection, f'get_{kind}')(f'example-{kind}')
            assert isinstance(result, model)
            assert result.model_dump() == parties[kind][0]
    asyncio.run(scenario())
    assert backend.requests[-1].method == 'GET'
    assert str(backend.requests[-1].url) == f'{BASE}{kind}/example-{kind}/'


@pytest.mark.req('SP-MLC-053')
@pytest.mark.req('SP-MLC-055')
@pytest.mark.req('SP-MLC-007')
@pytest.mark.parametrize('kind', ['client', 'contact'])
@pytest.mark.parametrize('stop_early', [False, True])
def test_lists_follow_pages_and_stop_without_fetching_remaining_pages(backend, parties, kind, stop_early):
    """一覧は全ページを順に返し、途中で反復を閉じた時は後続ページを取らない。"""
    parties[kind] = [{**resource(kind), 'mid': f'example-{kind}-{i}'} for i in range(205)]
    async def scenario():
        async with api(backend) as connection:
            rows = getattr(connection, f'list_{kind}s')(filters=[('search', 'example-a'), ('search', 'example-b')])
            if stop_early:
                assert (await anext(rows)).mid == f'example-{kind}-0'
                await rows.aclose()
            else:
                assert [row.mid async for row in rows] == [f'example-{kind}-{i}' for i in range(205)]
    asyncio.run(scenario())
    requests = [r for r in backend.requests if r.url.path.endswith(f'/{kind}/')]
    assert [int(r.url.params['page']) for r in requests] == ([1] if stop_early else [1, 2, 3])
    assert all(r.url.params.get_list('search') == ['example-a', 'example-b'] for r in requests)


@pytest.mark.req('SP-MLC-055')
def test_contact_list_uses_client_contact_endpoint(backend, parties):
    """取引先の mid を受けた一覧は、その取引先の連絡先の endpoint を読む。"""
    async def scenario():
        async with api(backend) as connection:
            result = [c async for c in connection.list_contacts(client_mid='example-client', filters={'search': 'example'})]
            assert [c.model_dump() for c in result] == parties['contact']
    asyncio.run(scenario())
    request = backend.requests[-1]
    assert request.url.path.endswith('/client/example-client/contact/')
    assert request.url.params['search'] == 'example'


@pytest.mark.req('SP-MLC-053')
@pytest.mark.req('SP-MLC-055')
@pytest.mark.parametrize('kind,model,excluded', [
    ('client', Client, {'url', 'mid', 'display_name', 'organization_type', 'is_organization', 'date_archived',
                       'managing_teams', 'default_presets', 'integrations', 'settings', 'organization_contact', 'virtual_bank_accounts'}),
    ('contact', Contact, {'url', 'mid', 'owner', 'owner_type', 'display_name', 'format', 'is_default', 'default_address', 'integrations'}),
])
@pytest.mark.parametrize('operation,method', [('create', 'POST'), ('update', 'PATCH')])
@pytest.mark.parametrize('as_model', [False, True])
def test_writes_omit_readonly_fields_and_return_resource_without_mutating_input(backend, parties, kind, model, excluded, operation, method, as_model):
    """mapping と model から読み取り専用の field だけを除き、API が返す取引先と連絡先を返す。"""
    raw = {**resource(kind), 'future_field': {'enabled': False, 'value': None}}
    if kind == 'client':
        raw['organization_contact'] = BASE + 'contact/example-contact/'
    data = model.model_validate(raw) if as_model else MappingProxyType(deepcopy(raw))
    expected = {key: val for key, val in raw.items() if key not in excluded}
    async def scenario():
        async with api(backend) as connection:
            args = (data,) if operation == 'create' else ('example-target', data)
            if kind == 'contact' and operation == 'create':
                args = ('example-client', data)
            result = await getattr(connection, f'{operation}_{kind}')(*args)
            assert isinstance(result, model)
            assert result.mid == f'example-saved-{kind}'
            assert result.future_field == raw['future_field']
            assert data.model_dump() == raw if as_model else data == raw
    asyncio.run(scenario())
    request = next(r for r in backend.requests if r.method == method and f'/{kind}/' in r.url.path)
    assert request.method == method
    assert str(request.url) == f'{BASE}{kind}/' + ('example-target/' if operation == 'update' else '')
    assert json.loads(request.content) == expected


@pytest.mark.req('SP-MLC-053')
@pytest.mark.req('SP-MLC-055')
@pytest.mark.parametrize('kind,model', [('client', Client), ('contact', Contact)])
def test_update_changes_only_supplied_fields_including_explicit_null(backend, parties, kind, model):
    """編集は指定した field と明示した null だけを PATCH し、省略した field を加えない。"""
    data = model.model_validate({'client_external_id': None} if kind == 'client' else {'email': None})
    async def scenario():
        async with api(backend) as connection:
            await getattr(connection, f'update_{kind}')('example-target', data)
    asyncio.run(scenario())
    assert backend.requests[-1].method == 'PATCH'
    assert json.loads(backend.requests[-1].content) == data.model_dump()


@pytest.mark.req('SP-MLC-053')
def test_create_client_accepts_inline_contacts(backend, parties):
    """取引先の作成は連絡先の内容を入れ子で受け、読み取り専用の連絡先 field を除く。"""
    contact = resource('contact')
    async def scenario():
        async with api(backend) as connection:
            await connection.create_client({'contacts': [contact]})
    asyncio.run(scenario())
    payload = json.loads(backend.requests[-1].content)
    assert payload['contacts'] == [{
        key: contact[key] for key in ('contact_type', 'name', 'family_name', 'given_name', 'title', 'department',
                                     'phone_number', 'fax_number', 'email', 'addresses')
    }]
    assert contact == resource('contact')


@pytest.mark.req('SP-MLC-054')
@pytest.mark.parametrize('contacts', [None, 'not-a-list', {}, [None], [17], [{'name': 'Example'}],
                                      [resource('contact')], ['not-a-url'], [''],
                                      [BASE + 'contact/example-contact/', {'name': 'Example'}]])
def test_client_update_rejects_non_url_contacts_before_any_request(backend, contacts):
    """連絡先の URL でない値を含む取引先の編集は、認証も含め request を送らない。"""
    async def scenario():
        async with api(backend) as connection:
            with pytest.raises(MakeLeapsError, match='contacts'):
                await connection.update_client('example-client', {'contacts': contacts})
    asyncio.run(scenario())
    assert backend.requests == []


@pytest.mark.req('SP-MLC-055')
def test_delete_contact_accepts_empty_response(backend, parties):
    """連絡先の削除は DELETE を送り、内容の無い成功応答で None を返す。"""
    async def scenario():
        async with api(backend) as connection:
            assert await connection.delete_contact('example-target') is None
    asyncio.run(scenario())
    assert backend.requests[-1].method == 'DELETE'
    assert str(backend.requests[-1].url) == BASE + 'contact/example-target/'


@pytest.mark.req('SP-MLC-053')
@pytest.mark.req('SP-MLC-055')
@pytest.mark.parametrize('operation', ['get_client', 'update_client', 'get_contact', 'create_contact', 'update_contact', 'delete_contact'])
def test_resource_changes_require_mid_before_http(backend, operation):
    """mid の無い取得、連絡先の作成、編集、削除は request を送らず失敗を返す。"""
    async def scenario():
        async with api(backend) as connection:
            with pytest.raises(MakeLeapsError, match='mid'):
                await getattr(connection, operation)(*(['', {}] if operation.startswith(('create', 'update')) else ['']))
    asyncio.run(scenario())
    assert backend.requests == []


@pytest.mark.req('SP-MLC-057')
@pytest.mark.parametrize('kind', ['client', 'contact'])
def test_cli_get_outputs_complete_resource_yaml(cli_transport, parties, capsys, kind):
    """取引先と連絡先の get は model 全体を YAML で返す。"""
    assert main([kind + 's', 'get', f'example-{kind}', '--language', 'en']) == 0
    output = capsys.readouterr()
    assert list(yaml.safe_load_all(output.out)) == [resource(kind)]
    assert output.err == ''
    assert all(r.headers['accept-language'] == 'en' for r in cli_transport.requests)


@pytest.mark.req('SP-MLC-057')
@pytest.mark.parametrize('kind', ['client', 'contact'])
@pytest.mark.parametrize('limit', [None, 0, 2])
def test_cli_lists_forward_filters_and_limit_as_one_yaml_sequence(cli_transport, parties, capsys, kind, limit):
    """一覧は絞り込みを API に渡し、件数の上限を守って一つの YAML の列を返す。"""
    parties[kind] = [{**resource(kind), 'mid': f'example-{kind}-{i}'} for i in range(3)]
    args = [kind + 's', 'list', '--filter', 'search=example-a', '--filter', 'search=example-b']
    if limit is not None:
        args += ['--limit', str(limit)]
    assert main(args) == 0
    output = capsys.readouterr()
    assert list(yaml.safe_load_all(output.out)) == [parties[kind][:limit]]
    assert output.err == ''
    if limit == 0:
        assert cli_transport.requests == []
    else:
        request = cli_transport.requests[-1]
        assert request.url.path.endswith(f'/{kind}/')
        assert request.url.params.get_list('search') == ['example-a', 'example-b']


@pytest.mark.req('SP-MLC-057')
def test_cli_contacts_list_filters_by_client(cli_transport, parties, capsys):
    """contacts list の --client は取引先の連絡先だけを読む。"""
    assert main(['contacts', 'list', '--client', 'example-client']) == 0
    assert yaml.safe_load(capsys.readouterr().out) == parties['contact']
    assert cli_transport.requests[-1].url.path.endswith('/client/example-client/contact/')


@pytest.mark.req('SP-MLC-057')
@pytest.mark.parametrize('kind', ['client', 'contact'])
@pytest.mark.parametrize('operation,method', [('create', 'POST'), ('update', 'PATCH')])
@pytest.mark.parametrize('source', ['file', 'stdin'])
def test_cli_writes_read_yaml_and_output_result(cli_transport, parties, capsys, tmp_path, monkeypatch, kind, operation, method, source):
    """取引先と連絡先の作成と編集は file と stdin の YAML を読み、結果を YAML で返す。"""
    text = 'client_external_id: null\n' if kind == 'client' else 'email: null\n'
    filename = '-'
    if source == 'file':
        path = tmp_path / 'input.yaml'
        path.write_text(text)
        filename = str(path)
    else:
        monkeypatch.setattr('sys.stdin', io.StringIO(text))
    args = [kind + 's', operation] + (['example-target'] if operation == 'update' else [])
    if kind == 'contact' and operation == 'create':
        args += ['--client', 'example-client']
    assert main([*args, '--file', filename]) == 0
    output = capsys.readouterr()
    value = yaml.safe_load(output.out)
    assert value['mid'] == f'example-saved-{kind}'
    assert output.err == ''
    request = next(r for r in cli_transport.requests if r.method == method and f'/{kind}/' in r.url.path)
    assert json.loads(request.content) == yaml.safe_load(text)


@pytest.mark.req('SP-MLC-057')
def test_cli_contact_delete_requires_yes_and_outputs_nothing(cli_transport, parties, capsys):
    """連絡先の削除は --yes が無ければ request を送らず、削除後は標準出力に何も出さない。"""
    assert main(['contacts', 'delete', 'example-target']) != 0
    output = capsys.readouterr()
    assert output.out == '' and '--yes' in output.err
    assert cli_transport.requests == []
    assert main(['contacts', 'delete', 'example-target', '--yes']) == 0
    output = capsys.readouterr()
    assert output.out == output.err == ''
    assert cli_transport.requests[-1].method == 'DELETE'
    assert cli_transport.requests[-1].url.path.endswith('/contact/example-target/')


@pytest.mark.req('SP-MLC-057')
@pytest.mark.parametrize('kind', ['clients', 'contacts'])
@pytest.mark.parametrize('operation', ['create', 'update'])
@pytest.mark.parametrize('text', ['[]', 'value: ['])
def test_cli_rejects_invalid_yaml_before_http(cli_transport, capsys, tmp_path, kind, operation, text):
    """作成と編集は不正な YAML と mapping 以外の値を request の前に拒否する。"""
    path = tmp_path / 'input.yaml'
    path.write_text(text)
    args = [kind, operation] + (['example-target'] if operation == 'update' else [])
    if kind == 'contacts' and operation == 'create':
        args += ['--client', 'example-client']
    assert main([*args, '--file', str(path)]) != 0
    output = capsys.readouterr()
    assert output.out == '' and 'YAML' in output.err
    assert cli_transport.requests == []


@pytest.mark.req('SP-MLC-055')
def test_contact_creation_requires_client_before_http(backend, parties):
    """取引先の mid を指定しない連絡先の作成は request を送らず失敗する。"""
    async def scenario():
        async with api(backend) as connection:
            with pytest.raises(TypeError, match='client_mid'):
                await connection.create_contact(data={})
    asyncio.run(scenario())
    assert backend.requests == []


@pytest.mark.req('SP-MLC-055')
@pytest.mark.req('SP-MLC-056')
def test_create_contact_keeps_all_existing_contact_urls(backend, parties):
    """連絡先を一つ作り、既存のすべての連絡先を残して取引先に加える。"""
    existing = [BASE + 'contact/example-organization/', BASE + 'contact/example-person/']
    parties['client'][0]['contacts'] = existing[:]
    data = {'contact_type': 'person', 'family_name': 'Example', 'email': None, 'addresses': []}
    async def scenario():
        async with api(backend) as connection:
            result = await connection.create_contact('example-client', data)
            assert isinstance(result, Contact)
            assert result.mid == 'example-saved-contact'
    asyncio.run(scenario())
    requests = [r for r in backend.requests if '/client/' in r.url.path or '/contact/' in r.url.path]
    assert [(r.method, str(r.url)) for r in requests] == [
        ('GET', BASE + 'client/example-client/'),
        ('POST', BASE + 'contact/'),
        ('PATCH', BASE + 'client/example-client/'),
    ]
    assert json.loads(requests[1].content) == data
    assert json.loads(requests[2].content) == {'contacts': [*existing, BASE + 'contact/example-saved-contact/']}
    assert existing == parties['client'][0]['contacts']


@pytest.mark.req('SP-MLC-056')
@pytest.mark.req('SP-MLC-011')
@pytest.mark.parametrize('failure', ['patch', 'missing_url'])
def test_failed_contact_attachment_reports_created_mid_and_keeps_contact(backend, parties, failure):
    """作成後に追加できなければ mid と理由を失敗に含め、連絡先を削除せず認証情報を除く。"""
    original = backend.override
    def handle(request):
        if request.method == 'PATCH':
            return httpx.Response(400, json={'response': {'contacts': [
                'Example attachment rejected. example-id example-secret example-token-1'
            ]}})
        if failure == 'missing_url' and request.method == 'POST' and '/contact/' in request.url.path:
            return envelope({'mid': 'example-saved-contact'})
        return original(request)
    backend.override = handle
    async def scenario():
        async with api(backend) as connection:
            with pytest.raises(MakeLeapsError) as caught:
                await connection.create_contact('example-client', {})
            message = str(caught.value)
            assert 'was created' in message
            assert 'example-saved-contact' in message
            assert 'could not be added' in message
            if failure == 'patch':
                assert 'contacts' in message and 'Example attachment rejected.' in message
            else:
                assert 'URL' in message
            assert not any(s in message for s in ('example-id', 'example-secret', 'example-token-1'))
    asyncio.run(scenario())
    assert not any(r.method == 'DELETE' for r in backend.requests)
    assert sum(r.method == 'POST' and '/contact/' in r.url.path for r in backend.requests) == 1


@pytest.mark.req('SP-MLC-056')
def test_client_lookup_failure_does_not_create_contact(backend, parties):
    """追加先の取引先を取得できなければ、連絡先を作らず失敗を返す。"""
    backend.override = lambda r: httpx.Response(403) if '/client/' in r.url.path else None
    async def scenario():
        async with api(backend) as connection:
            with pytest.raises(MakeLeapsError, match='403'):
                await connection.create_contact('example-client', {})
    asyncio.run(scenario())
    assert not any('/contact/' in r.url.path for r in backend.requests)


@pytest.mark.req('SP-MLC-057')
def test_cli_contact_creation_requires_client_before_http(cli_transport, parties, capsys, tmp_path):
    """--client の無い連絡先の作成は request を送らず、標準エラーに理由を出して失敗する。"""
    path = tmp_path / 'contact.yaml'
    path.write_text('{}')
    with pytest.raises(SystemExit) as caught:
        main(['contacts', 'create', '--file', str(path)])
    assert caught.value.code != 0
    output = capsys.readouterr()
    assert output.out == ''
    assert '--client' in output.err and 'required' in output.err
    assert cli_transport.requests == []


@pytest.mark.req('SP-MLC-057')
def test_cli_contact_creation_attaches_to_selected_client(cli_transport, parties, capsys, tmp_path):
    """contacts create の --client は既存の連絡先を残して追加し、作成した連絡先を YAML で返す。"""
    path = tmp_path / 'contact.yaml'
    path.write_text('contact_type: person\nfamily_name: Example\nemail: null\naddresses: []\n')
    assert main(['contacts', 'create', '--file', str(path), '--client', 'example-client']) == 0
    output = capsys.readouterr()
    assert output.err == ''
    assert yaml.safe_load(output.out)['mid'] == 'example-saved-contact'
    patch = next(r for r in cli_transport.requests if r.method == 'PATCH')
    assert json.loads(patch.content) == {'contacts': [
        BASE + 'contact/example-contact/', BASE + 'contact/example-saved-contact/'
    ]}


@pytest.mark.req('SP-MLC-057')
@pytest.mark.req('SP-MLC-056')
def test_cli_attachment_failure_explains_partial_success(cli_transport, parties, capsys, tmp_path):
    """追加に失敗した cli は作成済みの連絡先の mid と理由を標準エラーへ出す。"""
    original = cli_transport.override
    cli_transport.override = lambda r: httpx.Response(403) if r.method == 'PATCH' else original(r)
    path = tmp_path / 'contact.yaml'
    path.write_text('{}')
    assert main(['contacts', 'create', '--file', str(path), '--client', 'example-client']) != 0
    output = capsys.readouterr()
    assert output.out == ''
    assert 'example-saved-contact' in output.err and 'was created' in output.err and '403' in output.err


@pytest.mark.req('SP-MLC-053')
@pytest.mark.req('SP-MLC-055')
@pytest.mark.req('SP-MLC-011')
@pytest.mark.parametrize('kind', ['client', 'contact'])
def test_api_rejections_include_fields_and_redact_secrets(backend, parties, kind):
    """API の拒否は field と理由を返し、認証情報と access token を例外から除く。"""
    backend.override = lambda r: httpx.Response(400, json={'response': {'email': [
        'Example invalid email. example-id example-secret example-token-1'
    ]}}) if f'/{kind}/' in r.url.path else None
    async def scenario():
        async with api(backend) as connection:
            with pytest.raises(MakeLeapsError) as caught:
                args = ('example-client', {}) if kind == 'contact' else ({},)
                await getattr(connection, f'create_{kind}')(*args)
            message = str(caught.value)
            assert 'HTTP 400' in message and 'email' in message and 'Example invalid email.' in message
            assert not any(s in message for s in ('example-id', 'example-secret', 'example-token-1'))
    asyncio.run(scenario())


@pytest.mark.req('SP-MLC-053')
@pytest.mark.req('SP-MLC-055')
@pytest.mark.req('SP-MLC-013')
@pytest.mark.parametrize('kind', ['client', 'contact'])
@pytest.mark.parametrize('failure', ['timeout', 'server_error'])
def test_creates_do_not_repeat_unknown_results_or_call_them_documents(backend, parties, kind, failure):
    """作成の応答が不明なら再送せず、取引先か連絡先が作成された可能性を知らせる。"""
    def handle(request):
        if request.method == 'POST' and f'/{kind}/' in request.url.path:
            if failure == 'timeout':
                raise httpx.ReadTimeout('Example response lost.', request=request)
            return httpx.Response(503)
    backend.override = handle
    async def scenario():
        async with api(backend) as connection:
            with pytest.raises(MakeLeapsError) as caught:
                args = ('example-client', {}) if kind == 'contact' else ({},)
                await getattr(connection, f'create_{kind}')(*args)
            message = str(caught.value)
            assert f'{kind} may have been created' in message
            assert 'document' not in message
    asyncio.run(scenario())
    assert sum(r.method == 'POST' and f'/{kind}/' in r.url.path for r in backend.requests) == 1
