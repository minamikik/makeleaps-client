import asyncio
import base64
from datetime import date, datetime
from decimal import Decimal
import inspect
import json
from copy import deepcopy
from types import MappingProxyType

import httpx
import pytest

from makeleaps_client import MakeLeapsClient, MakeLeapsError, Document, DocumentArchive, Partner, Client, Contact
from conftest import BASE, PDF, envelope, resource


def run(coro):
    return asyncio.run(coro)


def client(backend, **kwargs):
    return MakeLeapsClient('example-id', 'example-secret', transport=httpx.MockTransport(backend), **kwargs)


@pytest.mark.req('SP-MLC-009')
@pytest.mark.req('SP-MLC-024')
def test_models_preserve_api_values_and_convert_money_and_dates():
    """書類の全 field を保ち、金額と日付を Python の型で扱える。"""
    raw = resource('document')
    raw['future_field'] = {'untouched': ['value']}
    raw['document_type'] = 'future-kind'
    doc = Document.model_validate(raw)
    assert doc.total == Decimal('123.40')
    assert doc.date == date(2025, 4, 12)
    assert doc.modified == datetime.fromisoformat('2025-04-12T01:02:03+00:00')
    assert doc.lineitems[0].unit_cost == Decimal('123.40')
    assert doc.tax_group_totals[0].tax == Decimal('12.34')
    assert doc.mixed_tax_rate_totals['10'].tax.amount == Decimal('12.34')
    assert doc.model_dump() == raw
    assert Document().model_dump() == {}
    for model, name in [(Partner, 'partner'), (Client, 'client'), (Contact, 'contact'), (DocumentArchive, 'archive')]:
        data = resource(name)
        assert model.model_validate(data).model_dump() == data
        assert model().model_dump() == {}


@pytest.mark.req('SP-MLC-009')
@pytest.mark.req('SP-MLC-024')
@pytest.mark.parametrize('address', [
    {'formatted_html': '<p>Example address</p>'},
    None,
    'Example address',
])
def test_contact_default_address_preserves_objects_null_and_strings(address):
    """担当者の既定の住所を object と null と文字列のまま返す。"""
    raw = {**resource('contact'), 'default_address': address}
    contact = Contact.model_validate(raw)
    assert contact.default_address == address
    assert contact.model_dump() == raw


@pytest.mark.req('SP-MLC-009')
def test_contact_keeps_expanded_owner_resource():
    """担当者の所有者が resource の全体でも、受けた形を保つ。"""
    raw = {'owner': resource('client')}
    assert Contact.model_validate(raw).model_dump() == raw


@pytest.mark.req('SP-MLC-009')
def test_read_operations_are_async():
    """呼出元は読み取りの操作を async だけで利用する。"""
    for name in ('get_partner', 'get_document', 'get_document_pdf', 'get_archive_pdf'):
        assert inspect.iscoroutinefunction(getattr(MakeLeapsClient, name))
    for name in ('list_documents', 'list_archives'):
        assert inspect.isasyncgenfunction(getattr(MakeLeapsClient, name))


@pytest.mark.req('SP-MLC-006')
@pytest.mark.req('SP-MLC-008')
@pytest.mark.req('SP-MLC-015')
@pytest.mark.req('SP-MLC-027')
def test_company_documents_archives_and_pdf_use_read_endpoints(backend, monkeypatch):
    """API キーの会社の各種書類と保存記録を取り、PDF の bytes を保つ。"""
    monkeypatch.setenv('MAKELEAPS_PARTNER_ID', 'ignored-company')
    backend.document_count = 6
    async def scenario():
        async with client(backend) as api:
            assert (await api.get_partner()).name == 'Example Company'
            docs = [d async for d in api.list_documents()]
            assert {d.document_type for d in docs} == {'invoice', 'quote', 'receipt', 'order', 'deliveryslip', 'future-kind'}
            assert docs[0].client.model_dump() == resource('client')
            assert docs[0].client_contact.model_dump() == resource('contact')
            assert (await api.get_document('one')).client_contact.display_name == 'example-display_name'
            assert await api.get_document_pdf('one') == PDF
            archives = [a async for a in api.list_archives()]
            assert len(archives) == 3
            assert await api.get_archive_pdf('one') == PDF
    run(scenario())
    assert all(r.method == 'GET' or (r.method == 'POST' and r.url.path == '/user/oauth2/token/') for r in backend.requests)
    assert 'partner' not in inspect.signature(MakeLeapsClient).parameters


@pytest.mark.req('SP-MLC-010')
@pytest.mark.req('SP-MLC-028')
@pytest.mark.parametrize('language', ['ja', 'en'])
def test_authentication_uses_arguments_and_all_requests_use_language(backend, monkeypatch, tmp_path, language):
    """library は引数で認証し、全 request に指定された言語を付ける。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('MAKELEAPS_CLIENT_ID', 'ignored-env-id')
    monkeypatch.setenv('MAKELEAPS_CLIENT_SECRET', 'ignored-env-secret')
    (tmp_path / 'makeleaps.yaml').write_text('invalid: config')
    async def scenario():
        async with client(backend, **({} if language == 'ja' else {'language': language})) as api:
            await api.get_partner()
    run(scenario())
    token_request = backend.requests[0]
    assert token_request.headers['authorization'] == 'Basic ' + base64.b64encode(b'example-id:example-secret').decode()
    assert token_request.content == b'grant_type=client_credentials'
    assert all(r.headers['accept-language'] == language for r in backend.requests)


@pytest.mark.req('SP-MLC-010')
def test_expired_tokens_and_401_are_refreshed_once(backend, monkeypatch):
    """期限切れと 401 では token を取り直し、同時の取得でも重複を抑える。"""
    import makeleaps_client.client as module
    now = [0.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    rejected = [False]
    def override(request):
        if request.url.path == '/api/' and not rejected[0]:
            rejected[0] = True
            return httpx.Response(401)
    backend.override = override
    async def scenario():
        async with client(backend) as api:
            await api.get_partner()
            assert backend.tokens == 2
            now[0] += 36001
            await asyncio.gather(api.get_partner(), api.get_partner())
            assert backend.tokens == 3
    run(scenario())


@pytest.mark.req('SP-MLC-010')
@pytest.mark.req('SP-MLC-011')
@pytest.mark.parametrize('failure', ['bad_credentials', 'repeated_401', 'transport', 'invalid_json', 'invalid_model'])
def test_failures_are_bounded_and_secrets_are_not_exposed(backend, tmp_path, monkeypatch, capsys, caplog, failure):
    """認証と通信の失敗は有限に終わり、認証情報を出力や file に残さない。"""
    import makeleaps_client.client as module
    import logging
    caplog.set_level(logging.DEBUG)
    async def sleep(delay):
        pass
    monkeypatch.setattr(module.asyncio, 'sleep', sleep)
    monkeypatch.chdir(tmp_path)
    secret = 'example-id example-secret example-token-1'
    def override(request):
        if failure == 'bad_credentials':
            return httpx.Response(400, json={'error': secret})
        if request.url.path == '/api/':
            if failure == 'repeated_401':
                return httpx.Response(401, text=secret)
            if failure == 'transport':
                raise httpx.ConnectError(secret, request=request)
            if failure == 'invalid_json':
                return httpx.Response(200, text=secret)
            return envelope({'partners': [{'url': BASE}]})
        if failure == 'invalid_model' and str(request.url) == BASE:
            return envelope({'integrations': secret})
    backend.override = override
    async def scenario():
        async with client(backend) as api:
            with pytest.raises(MakeLeapsError) as caught:
                await api.get_partner()
            import traceback
            rendered = ''.join(traceback.format_exception(caught.value))
            assert all(s not in rendered for s in secret.split())
    run(scenario())
    assert len(backend.requests) < 12
    output = capsys.readouterr()
    assert all(s not in output.out + output.err + caplog.text for s in secret.split())
    assert list(tmp_path.iterdir()) == []


@pytest.mark.req('SP-MLC-012')
@pytest.mark.parametrize('url', ['http://api.makeleaps.com/api/x/', 'https://elsewhere.invalid/api/x/', 'https://api.makeleaps.com.evil.invalid/', '//elsewhere.invalid/x/', 'https://api.makeleaps.com:444/api/', 'https://user:pass@api.makeleaps.com/api/'])
def test_untrusted_resource_urls_are_rejected_before_request(backend, url):
    """API の応答に別の宛先が含まれても、認証付き request を送らない。"""
    def override(request):
        if request.url.path == '/api/':
            return envelope({'partners': [{'url': url}]})
    backend.override = override
    async def scenario():
        async with client(backend) as api:
            with pytest.raises(MakeLeapsError):
                await api.get_partner()
    run(scenario())
    assert all(r.url.host == 'api.makeleaps.com' and r.url.scheme == 'https' for r in backend.requests)


@pytest.mark.req('SP-MLC-012')
def test_redirects_are_not_followed(backend):
    """redirect の先へ認証付き request を送らない。"""
    backend.override = lambda r: httpx.Response(302, headers={'location': 'https://elsewhere.invalid/'}) if r.url.path == '/api/' else None
    async def scenario():
        async with client(backend) as api:
            with pytest.raises(MakeLeapsError):
                await api.get_partner()
    run(scenario())
    assert len(backend.requests) == 2


@pytest.mark.req('SP-MLC-013')
@pytest.mark.parametrize('keep_failing', [False, True])
def test_retry_after_is_waited_and_retries_are_finite(backend, monkeypatch, keep_failing):
    """429 の待機時間を守って再送し、上限の応答が続けば失敗を返す。"""
    import makeleaps_client.client as module
    waits = []
    now = [0.0]
    async def sleep(delay):
        waits.append(delay)
        now[0] += delay
    monkeypatch.setattr(module.asyncio, 'sleep', sleep)
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    attempts = [0]
    def override(request):
        if request.url.path == '/api/':
            attempts[0] += 1
            if keep_failing or attempts[0] == 1:
                return httpx.Response(429, headers={'Retry-After': '7'})
    backend.override = override
    async def scenario():
        async with client(backend) as api:
            if keep_failing:
                with pytest.raises(MakeLeapsError):
                    await api.get_partner()
            else:
                assert (await api.get_partner()).name == 'Example Company'
    run(scenario())
    assert waits and all(delay >= 7 for delay in waits)
    assert attempts[0] < 10


@pytest.mark.req('SP-MLC-007')
@pytest.mark.req('SP-MLC-027')
def test_all_pages_are_concurrent_ordered_and_expanded_once_per_operation(backend):
    """大量の書類を順に返し、取引先と担当者の一覧は一度だけ取る。"""
    backend.document_count = 6503
    async def scenario():
        async with client(backend) as api:
            docs = [d async for d in api.list_documents()]
            assert [d.mid for d in docs] == [f'example-document-{i}' for i in range(6503)]
            assert all(d.client.mid == 'example-client' for d in docs)
            assert len(backend.requests) == 72
            await api.get_document('one')
    run(scenario())
    assert backend.max_active_pages > 1
    paths = [r.url.path for r in backend.requests]
    assert paths.count('/api/partner/example-company/client/') == 1
    assert paths.count('/api/partner/example-company/contact/') == 1
    assert len(backend.requests) == 75
    assert all('expand' not in r.url.params for r in backend.requests)


@pytest.mark.req('SP-MLC-007')
@pytest.mark.parametrize('resource_name', ['documents', 'archives'])
def test_stopping_iteration_does_not_fetch_remaining_pages(backend, resource_name):
    """呼出元が反復を止めると、一覧の残りのページを取得しない。"""
    backend.document_count = backend.archive_count = 2001
    async def scenario():
        async with client(backend) as api:
            iterator = getattr(api, 'list_' + resource_name)()
            async for item in iterator:
                assert item.mid.endswith('-0')
                break
            await iterator.aclose()
    run(scenario())
    assert not any(int(r.url.params.get('page', '1')) > 1 for r in backend.requests)


@pytest.mark.req('SP-MLC-007')
@pytest.mark.req('SP-MLC-013')
def test_concurrent_operations_share_the_rate_limit(backend, monkeypatch):
    """同時に取るページと再送も、同じ回数の上限を守る。"""
    import makeleaps_client.client as module
    now = [0.0]
    times = []
    async def sleep(delay):
        now[0] += delay
        # Do not use patched asyncio.sleep to yield.
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(module.asyncio, 'sleep', sleep)
    def override(request):
        times.append(now[0])
    backend.override = override
    backend.archive_count = 25100
    async def scenario():
        async with client(backend) as api:
            archives = [a async for a in api.list_archives()]
            assert [a.mid for a in archives] == [f'example-archive-{i}' for i in range(25100)]
    run(scenario())
    assert now[0] >= 60
    assert all(sum(t <= stamp < t + 60 for stamp in times) <= 250 for t in times)


@pytest.mark.req('SP-MLC-026')
def test_named_filters_and_repeated_api_parameters_are_forwarded(backend):
    """種類と発行日の両端を送り、そのほかの条件の名前と値を保つ。"""
    async def scenario():
        async with client(backend) as api:
            _ = [d async for d in api.list_documents(document_type='quote', date_from=date(2025, 1, 1), date_to='2025-12-31', filters=[('document_number', 'EXAMPLE-A'), ('document_number', 'EXAMPLE-B'), ('sent', 'true')])]
    run(scenario())
    params = next(r.url.params for r in backend.requests if r.url.path.endswith('/document/'))
    assert params['document_type'] == 'quote'
    assert params['date__gte'] == '2025-01-01'
    assert params['date__lte'] == '2025-12-31'
    assert params.get_list('document_number') == ['EXAMPLE-A', 'EXAMPLE-B']
    assert params['sent'] == 'true'


@pytest.mark.req('SP-MLC-006')
@pytest.mark.parametrize('name', ['document', 'archive'])
def test_missing_pdf_is_a_failure(backend, name):
    """PDF の URL が無い書類と保存記録は失敗として返す。"""
    def override(request):
        if request.url.path.endswith(f'/{name}/one/'):
            return envelope({**resource(name), 'pdf_content_url': None})
    backend.override = override
    async def scenario():
        async with client(backend) as api:
            with pytest.raises(MakeLeapsError):
                await getattr(api, f'get_{name}_pdf')('one')
    run(scenario())
    assert not any(r.url.path.endswith('/pdf/') for r in backend.requests)


@pytest.mark.req('SP-MLC-007')
@pytest.mark.req('SP-MLC-027')
def test_first_document_precedes_related_lists_and_references_overlap(backend):
    """最初の書類は全件の取引先を待たず、参照先を同時に取って返す。"""
    first_delivered = False
    active = 0
    max_active = 0
    async def override(request):
        nonlocal active, max_active
        if request.url.path.endswith(('/client/', '/contact/')):
            assert first_delivered, 'The first document must precede the related lists'
        if request.url.path.endswith(('/client/example-client/', '/contact/example-contact/')):
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0)
            active -= 1
    backend.override = override
    async def scenario():
        nonlocal first_delivered
        async with client(backend) as api:
            docs = api.list_documents()
            first = await anext(docs)
            first_delivered = True
            assert first.client.model_dump() == resource('client')
            assert first.client_contact.model_dump() == resource('contact')
            assert len([d async for d in docs]) == 2
    run(scenario())
    assert max_active == 2


@pytest.mark.req('SP-MLC-027')
def test_get_document_fetches_only_referenced_client_and_contact(backend):
    """書類の詳細は参照先だけを取得し、取引先と担当者の全 field を返す。"""
    async def scenario():
        async with client(backend) as api:
            doc = await api.get_document('one')
            assert doc.client.model_dump() == resource('client')
            assert doc.client_contact.model_dump() == resource('contact')
    run(scenario())
    assert [r.url.path for r in backend.requests] == [
        '/user/oauth2/token/', '/api/',
        '/api/partner/example-company/document/one/',
        '/api/partner/example-company/client/example-client/',
        '/api/partner/example-company/contact/example-contact/',
    ]


@pytest.mark.req('SP-MLC-027')
@pytest.mark.parametrize('references', [
    {}, {'client': None, 'client_contact': None},
    {'client': resource('client'), 'client_contact': resource('contact')},
])
def test_get_document_does_not_fetch_absent_or_expanded_references(backend, references):
    """参照先が未指定か展開済みなら、取引先と担当者の取得を省く。"""
    raw = {k: v for k, v in resource('document').items() if k not in ('client', 'client_contact')}
    raw.update(references)
    backend.override = lambda r: envelope(raw) if r.url.path.endswith('/document/one/') else None
    async def scenario():
        async with client(backend) as api:
            assert (await api.get_document('one')).model_dump() == raw
    run(scenario())
    assert len(backend.requests) == 3


@pytest.mark.req('SP-MLC-040')
@pytest.mark.req('SP-MLC-041')
@pytest.mark.req('SP-MLC-042')
@pytest.mark.req('SP-MLC-027')
@pytest.mark.parametrize('as_model', [False, True])
@pytest.mark.parametrize('operation,method', [('create_document', 'POST'), ('update_document', 'PATCH')])
@pytest.mark.parametrize('autocalculate', [{}, {'autocalculate': False}], ids=['absent', 'false'])
def test_document_writes_autocalculate_lineitems_without_changing_input(backend, as_model, operation, method, autocalculate):
    """読み取りの書類から作成・編集し、入力を変えずに自動計算を有効にして合計と通常・小計の明細の金額を除く。"""
    readonly = {
        'mid', 'url', 'display_name', 'pdf_content_url', 'pdf_upload_url',
        'pickup_links', 'modified', 'date_removed', 'date_converted',
        'is_accepted', 'is_confirmed', 'is_converted', 'approval_status', 'integrations',
    }
    calculated = {
        'subtotal', 'tax', 'total', 'mixed_tax_rate_totals', 'tax_group_totals',
        'subtotal_sales_tax', 'subtotal_withholding_tax', 'jp_withholding_tax',
    }
    raw = resource('document')
    raw.update(client=resource('client'), client_contact=resource('contact'))
    raw.update(autocalculate)
    raw['lineitems'] = [{**resource('lineitem'), 'kind': kind}
                        for kind in ('normal', 'simple', 'text', 'subtotal', 'future-kind')]
    raw['lineitems'].append({k: v for k, v in resource('lineitem').items() if k != 'kind'})
    raw['future_field'] = {'mid': 'keep-nested-mid', 'url': 'keep-nested-url', 'total': '7.89'}
    original = deepcopy(raw)
    def override(request):
        if request.method == 'GET' and request.url.path.endswith('/document/one/'):
            return envelope(raw)
        if request.method == method and '/document/' in request.url.path:
            return httpx.Response(201 if method == 'POST' else 200, json={'response': resource('document')})
    backend.override = override
    async def scenario():
        async with client(backend, language='en') as api:
            data = await api.get_document('one') if as_model else MappingProxyType(raw)
            args = (data,) if operation == 'create_document' else ('one', data)
            doc = await getattr(api, operation)(*args)
            expected = {**resource('document'), 'client': resource('client'), 'client_contact': resource('contact')}
            assert doc.model_dump() == expected
            if as_model:
                assert data.model_dump() == original
    run(scenario())
    writes = [r for r in backend.requests if r.method == method and '/document/' in r.url.path]
    assert len(writes) == 1
    assert str(writes[0].url) == BASE + 'document/' + ('one/' if operation == 'update_document' else '')
    expected = {k: v for k, v in raw.items() if k not in readonly | calculated | {'round_tax_down'}}
    lineitems = deepcopy(raw['lineitems'])
    del lineitems[0]['price']
    del lineitems[3]['price']
    expected.update(client=raw['client']['url'], client_contact=raw['client_contact']['url'],
                    autocalculate=True, lineitems=lineitems)
    assert json.loads(writes[0].content) == expected
    assert raw == original
    assert all(r.headers['accept-language'] == 'en' for r in backend.requests)
    assert not any(r.url.path.endswith(('/client/', '/contact/')) for r in backend.requests)


@pytest.mark.req('SP-MLC-040')
@pytest.mark.parametrize('as_model', [False, True])
@pytest.mark.parametrize('operation,method', [('create_document', 'POST'), ('update_document', 'PATCH')])
@pytest.mark.parametrize('raw,expected', [
    ({'tax_rounding': 'round_down', 'round_tax_down': True}, {'tax_rounding': 'round_down'}),
    ({'tax_rounding': None, 'round_tax_down': False}, {'tax_rounding': None}),
    ({'round_tax_down': True}, {'round_tax_down': True}),
    ({'round_tax_down': False}, {'round_tax_down': False}),
], ids=['tax-rounding', 'explicit-null', 'legacy-true', 'legacy-false'])
def test_document_writes_prefer_tax_rounding_only_when_present(backend, as_model, operation, method, raw, expected):
    """作成と編集は tax_rounding がある時だけ round_tax_down を除き、単独の真偽値は保つ。"""
    original = deepcopy(raw)
    data = Document.model_validate(raw) if as_model else MappingProxyType(raw)
    backend.override = lambda r: envelope(resource('document')) if r.method == method and '/document/' in r.url.path else None
    async def scenario():
        async with client(backend) as api:
            args = (data,) if operation == 'create_document' else ('one', data)
            await getattr(api, operation)(*args)
    run(scenario())
    writes = [r for r in backend.requests if r.method == method and '/document/' in r.url.path]
    assert len(writes) == 1
    assert json.loads(writes[0].content) == ({**expected, 'autocalculate': True} if method == 'POST' else expected)
    assert raw == original
    if as_model:
        assert data.model_dump() == original


@pytest.mark.req('SP-MLC-040')
@pytest.mark.req('SP-MLC-042')
@pytest.mark.parametrize('autocalculate', [{}, {'autocalculate': False}, {'autocalculate': True}, {'autocalculate': None}],
                         ids=['absent', 'false', 'true', 'null'])
@pytest.mark.parametrize('totals', [{}, {k: v for k, v in resource('document').items()
                                      if k in ('subtotal', 'tax', 'total', 'jp_withholding_tax',
                                               'subtotal_sales_tax', 'subtotal_withholding_tax',
                                               'mixed_tax_rate_totals', 'tax_group_totals')}],
                         ids=['no-totals', 'all-totals'])
@pytest.mark.parametrize('as_model', [False, True])
def test_update_without_lineitems_preserves_supplied_totals_and_autocalculate(backend, as_model, totals, autocalculate):
    """明細を含まない編集は合計と autocalculate を受けたとおりに送り、省略された値を足さない。"""
    raw = {'title': 'Revised example', **totals, **autocalculate}
    original = deepcopy(raw)
    data = Document.model_validate(raw) if as_model else MappingProxyType(raw)
    backend.override = lambda r: envelope(resource('document')) if r.method == 'PATCH' else None
    async def scenario():
        async with client(backend) as api:
            await api.update_document('one', data)
    run(scenario())
    writes = [r for r in backend.requests if r.method == 'PATCH']
    assert len(writes) == 1
    assert json.loads(writes[0].content) == original
    assert raw == original
    if as_model:
        assert data.model_dump() == original


@pytest.mark.req('SP-MLC-040')
@pytest.mark.req('SP-MLC-046')
@pytest.mark.parametrize('operation,method', [('create_document', 'POST'), ('update_document', 'PATCH')])
@pytest.mark.parametrize('lineitems', [None, 'invalid-lineitems', {'kind': 'normal', 'price': '7.89'}, [None, 'invalid-lineitem']])
def test_document_writes_leave_invalid_lineitems_for_api_validation(backend, operation, method, lineitems):
    """不正な明細でも自動計算を有効にして API へ送り、API が示した入力の誤りを返す。"""
    raw = {'lineitems': lineitems, 'total': '7.89'}
    original = deepcopy(raw)
    backend.override = lambda r: httpx.Response(400, json={'response': {
        'lineitems': ['Example line items are invalid.'],
    }}) if r.method == method and '/document/' in r.url.path else None
    async def scenario():
        async with client(backend) as api:
            args = (raw,) if operation == 'create_document' else ('one', raw)
            with pytest.raises(MakeLeapsError, match='lineitems: Example line items are invalid.'):
                await getattr(api, operation)(*args)
    run(scenario())
    writes = [r for r in backend.requests if r.method == method and '/document/' in r.url.path]
    assert len(writes) == 1
    assert json.loads(writes[0].content) == {'lineitems': lineitems, 'autocalculate': True}
    assert raw == original


@pytest.mark.req('SP-MLC-040')
@pytest.mark.req('SP-MLC-042')
@pytest.mark.parametrize('as_model', [False, True])
def test_update_document_patches_only_supplied_fields_and_returns_expanded_resources(backend, as_model):
    """編集は指定した field と明示的な null だけを PATCH し、展開した書類を返す。"""
    raw = {'title': 'Revised example', 'date_due': None, 'has_hanko': False,
           'client': resource('client')['url'], 'client_contact': None,
           'mid': 'do-not-send', 'lineitems': []}
    expected = {k: v for k, v in raw.items() if k != 'mid'}
    expected['autocalculate'] = True
    data = Document.model_validate(raw) if as_model else raw
    backend.override = lambda r: envelope({**resource('document'), 'title': raw['title']}) if r.method == 'PATCH' else None
    async def scenario():
        async with client(backend) as api:
            doc = await api.update_document('example/write mid', data)
            assert doc.title == raw['title']
            assert doc.client.model_dump() == resource('client')
            assert doc.client_contact.model_dump() == resource('contact')
    run(scenario())
    writes = [r for r in backend.requests if r.method == 'PATCH']
    assert len(writes) == 1
    assert str(writes[0].url) == BASE + 'document/example%2Fwrite%20mid/'
    assert json.loads(writes[0].content) == expected
    assert not any(r.method == 'GET' and '/document/' in r.url.path for r in backend.requests)


@pytest.mark.req('SP-MLC-043')
def test_delete_document_accepts_empty_success_response(backend):
    """指定した書類を DELETE し、本文のない 204 で None を返す。"""
    backend.override = lambda r: httpx.Response(204) if r.method == 'DELETE' else None
    async def scenario():
        async with client(backend) as api:
            assert await api.delete_document('example/write mid') is None
    run(scenario())
    writes = [r for r in backend.requests if r.method == 'DELETE']
    assert len(writes) == 1
    assert str(writes[0].url) == BASE + 'document/example%2Fwrite%20mid/'
    assert writes[0].content == b''


@pytest.mark.req('SP-MLC-042')
@pytest.mark.req('SP-MLC-043')
@pytest.mark.parametrize('operation', ['update_document', 'delete_document'])
def test_document_changes_require_mid_before_http(backend, operation):
    """編集と削除は mid が無ければ request を送らずに失敗する。"""
    async def scenario():
        async with client(backend) as api:
            args = ('', {}) if operation == 'update_document' else ('',)
            with pytest.raises(MakeLeapsError, match='mid'):
                await getattr(api, operation)(*args)
    run(scenario())
    assert backend.requests == []


@pytest.mark.req('SP-MLC-041')
@pytest.mark.req('SP-MLC-042')
@pytest.mark.req('SP-MLC-010')
@pytest.mark.req('SP-MLC-040')
@pytest.mark.parametrize('operation,method', [('create_document', 'POST'), ('update_document', 'PATCH')])
def test_document_writes_refresh_rejected_token_and_preserve_body(backend, operation, method):
    """作成と編集で 401 を受けたら一度再認証し、同じ内容を再送する。"""
    attempts = []
    def override(request):
        if request.method == method and '/document/' in request.url.path:
            attempts.append(request)
            return httpx.Response(401) if len(attempts) == 1 else envelope(resource('document'))
    backend.override = override
    async def scenario():
        async with client(backend) as api:
            args = ({'title': 'Example'},) if operation == 'create_document' else ('one', {'title': 'Example'})
            assert isinstance(await getattr(api, operation)(*args), Document)
    run(scenario())
    assert backend.tokens == 2 and len(attempts) == 2
    assert [r.headers['authorization'] for r in attempts] == ['Bearer example-token-1', 'Bearer example-token-2']
    expected = {'title': 'Example', 'autocalculate': True} if method == 'POST' else {'title': 'Example'}
    assert all(json.loads(r.content) == expected for r in attempts)


@pytest.mark.req('SP-MLC-046')
@pytest.mark.req('SP-MLC-011')
@pytest.mark.parametrize('operation', ['create_document', 'update_document'])
def test_document_input_errors_include_fields_and_messages_without_secrets(backend, capsys, caplog, operation):
    """入力の誤りは field と内容を示し、API が返した認証情報と token を除く。"""
    backend.override = lambda r: httpx.Response(400, json={'response': {
        'currency': ['Choose an example currency.', 'A second explanation.'],
        'non_field_errors': ['Example document is invalid.'],
        'example-secret': ['example-id example-secret example-token-1'],
    }}) if '/document/' in r.url.path else None
    async def scenario():
        async with client(backend) as api:
            args = ({},) if operation == 'create_document' else ('one', {})
            with pytest.raises(MakeLeapsError) as caught:
                await getattr(api, operation)(*args)
            message = str(caught.value)
            assert 'HTTP 400' in message
            for text in ('currency', 'Choose an example currency.', 'A second explanation.',
                         'non_field_errors', 'Example document is invalid.'):
                assert text in message
            import traceback
            rendered = ''.join(traceback.format_exception(caught.value))
            assert not any(secret in rendered for secret in ('example-id', 'example-secret', 'example-token-1'))
    run(scenario())
    output = capsys.readouterr()
    assert not any(secret in output.out + output.err + caplog.text for secret in ('example-id', 'example-secret', 'example-token-1'))


@pytest.mark.req('SP-MLC-046')
@pytest.mark.parametrize('operation', ['create_document', 'update_document'])
@pytest.mark.parametrize('errors,details', [
    ({'lineitems': [{'has_jp_withholding_tax': ['Example withholding flag is required.']}]},
     'lineitems.0.has_jp_withholding_tax: Example withholding flag is required.'),
    ({'lineitems': [{}, {'quantity': ['Example quantity is invalid.', 'Use a positive quantity.']}, {}]},
     'lineitems.1.quantity: Example quantity is invalid.; Use a positive quantity.'),
    ({'client': {'address': {'postcode': ['Example postcode is invalid.']}}},
     'client.address.postcode: Example postcode is invalid.'),
    ({'lineitems': [{'description': ['Example description is required.']},
                    ['Example row is invalid.', 'Check the example row.']]},
     'lineitems.0.description: Example description is required.; '
     'lineitems.1: Example row is invalid.; Check the example row.'),
    ({'currency': ['Choose an example currency.', 'Check the example currency.'],
      'lineitems': [{'non_field_errors': ['Example row cannot be saved.']}],
      'non_field_errors': ['Example document cannot be saved.']},
     'currency: Choose an example currency.; Check the example currency.; '
     'lineitems.0.non_field_errors: Example row cannot be saved.; '
     'non_field_errors: Example document cannot be saved.'),
])
def test_nested_document_input_errors_identify_fields_and_row_numbers(backend, operation, errors, details):
    """入れ子の入力エラーは field と明細の番号を示し、空の明細を飛ばして全メッセージを返す。"""
    backend.override = lambda r: httpx.Response(400, json={
        'meta': {'status': 400, 'status_code': 400}, 'response': errors,
    }) if '/document/' in r.url.path else None
    async def scenario():
        async with client(backend) as api:
            args = ({},) if operation == 'create_document' else ('one', {})
            with pytest.raises(MakeLeapsError) as caught:
                await getattr(api, operation)(*args)
            assert str(caught.value) == f'MakeLeaps API request failed (HTTP 400). {details}'
    run(scenario())


@pytest.mark.req('SP-MLC-046')
@pytest.mark.req('SP-MLC-011')
@pytest.mark.parametrize('client_id', ['example-id', 'HTTP'])
def test_nested_input_errors_redact_credentials_from_the_entire_message(backend, capsys, caplog, client_id):
    """入れ子の位置と本文と HTTP status の説明に含まれる認証情報を例外全体から除く。"""
    basic = base64.b64encode(f'{client_id}:example-secret'.encode()).decode()
    backend.override = lambda r: httpx.Response(400, json={'response': {
        'lineitems': [{'example-secret': [f'Example value is invalid. {client_id} {basic} example-token-1']}],
    }}) if '/document/' in r.url.path else None
    async def scenario():
        async with MakeLeapsClient(client_id, 'example-secret', transport=httpx.MockTransport(backend)) as api:
            with pytest.raises(MakeLeapsError) as caught:
                await api.create_document({})
            assert 'lineitems.0.[redacted]: Example value is invalid.' in str(caught.value)
            import traceback
            rendered = ''.join(traceback.format_exception(caught.value))
            assert not any(secret in rendered for secret in (client_id, 'example-secret', basic, 'example-token-1'))
    run(scenario())
    output = capsys.readouterr()
    assert not any(secret in output.out + output.err + caplog.text
                   for secret in (client_id, 'example-secret', basic, 'example-token-1'))


@pytest.mark.req('SP-MLC-046')
@pytest.mark.parametrize('body', [
    b'not JSON', b'[]', b'{"response": []}', b'{"response": {}}',
    b'{"response": {"title": "not a list"}}',
    b'{"response": {"title": [42]}}',
    b'{"response": {"lineitems": [{}, {}]}}',
    b'{"response": {"lineitems": {"quantity": []}}}',
    *[json.dumps({'response': {
        'currency': ['Choose an example currency.'],
        'lineitems': [{'quantity': ['Example quantity is invalid.', invalid]}],
    }}).encode() for invalid in (42, None, False, {})],
    b'{"response": {"lineitems": [{"quantity": ["Example error."]}, null]}}',
    b'{"response": {"lineitems": [{"quantity": ["Example error."]}, {"description": "not a list"}]}}',
])
def test_unexpected_write_error_responses_use_generic_http_failure(backend, body):
    """入力エラーの応答が規定の形でなければ HTTP status だけで失敗を示す。"""
    backend.override = lambda r: httpx.Response(400, content=body) if '/document/' in r.url.path else None
    async def scenario():
        async with client(backend) as api:
            with pytest.raises(MakeLeapsError) as caught:
                await api.create_document({})
            assert str(caught.value) == 'MakeLeaps API request failed (HTTP 400).'
    run(scenario())


@pytest.mark.req('SP-MLC-041')
@pytest.mark.req('SP-MLC-042')
@pytest.mark.parametrize('operation', ['create_document', 'update_document'])
def test_document_writes_reject_success_response_without_document(backend, operation):
    """作成と編集の成功応答が書類の object でなければ、library の失敗として返す。"""
    backend.override = lambda r: envelope([]) if '/document/' in r.url.path else None
    async def scenario():
        async with client(backend) as api:
            args = ({},) if operation == 'create_document' else ('one', {})
            with pytest.raises(MakeLeapsError, match='invalid resource'):
                await getattr(api, operation)(*args)
    run(scenario())


@pytest.fixture
def retry_clock(monkeypatch):
    import makeleaps_client.client as module
    now = [0.0]
    waits = []
    async def sleep(delay):
        waits.append(delay)
        now[0] += delay
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(module.asyncio, 'sleep', sleep)
    return now, waits


@pytest.mark.req('SP-MLC-013')
@pytest.mark.req('SP-MLC-041')
@pytest.mark.parametrize('failure', ['read_timeout', 'connection_lost', 'server_error'])
def test_create_document_does_not_repeat_requests_with_unknown_result(backend, retry_clock, failure):
    """作成の応答が途切れるか 5xx になったら、再送せず結果の確認を求める。"""
    attempts = []
    def override(request):
        if request.method == 'POST' and request.url.path.endswith('/document/'):
            attempts.append(request)
            if len(attempts) == 1:
                if failure == 'read_timeout':
                    raise httpx.ReadTimeout('synthetic read timeout', request=request)
                if failure == 'connection_lost':
                    raise httpx.RemoteProtocolError('synthetic connection loss', request=request)
                return httpx.Response(503)
            return envelope(resource('document'))
    backend.override = override
    async def scenario():
        async with client(backend) as api:
            with pytest.raises(MakeLeapsError) as caught:
                await api.create_document({'title': 'Example'})
            message = str(caught.value)
            assert 'may have been created' in message
            assert 'Check MakeLeaps before retrying' in message
    run(scenario())
    assert len(attempts) == 1
    assert retry_clock[1] == []


@pytest.mark.req('SP-MLC-013')
@pytest.mark.req('SP-MLC-041')
@pytest.mark.req('SP-MLC-040')
@pytest.mark.parametrize('failure', ['rate_limit', 'connect_error', 'connect_timeout'])
@pytest.mark.parametrize('keep_failing', [False, True])
def test_create_document_retries_only_unaccepted_requests_with_finite_attempts(backend, retry_clock, failure, keep_failing):
    """作成は 429 と接続確立の失敗だけを再送し、失敗が続けば有限回で止める。"""
    attempts = []
    def override(request):
        if request.method == 'POST' and request.url.path.endswith('/document/'):
            attempts.append(request)
            if len(attempts) == 1 or keep_failing:
                if failure == 'connect_error':
                    raise httpx.ConnectError('synthetic connection failure', request=request)
                if failure == 'connect_timeout':
                    raise httpx.ConnectTimeout('synthetic connect timeout', request=request)
                return httpx.Response(429, headers={'Retry-After': '7'})
            return envelope(resource('document'))
    backend.override = override
    async def scenario():
        async with client(backend) as api:
            if keep_failing:
                with pytest.raises(MakeLeapsError):
                    await api.create_document({'title': 'Example'})
            else:
                doc = await api.create_document({'title': 'Example'})
                assert doc.mid == 'example-document'
                assert doc.client.model_dump() == resource('client')
    run(scenario())
    assert len(attempts) == (4 if keep_failing else 2)
    assert all(json.loads(request.content) == {'title': 'Example', 'autocalculate': True} for request in attempts)
    assert retry_clock[1]
    if failure == 'rate_limit':
        assert all(delay >= 7 for delay in retry_clock[1])


@pytest.mark.req('SP-MLC-041')
@pytest.mark.req('SP-MLC-027')
@pytest.mark.parametrize('field', ['client', 'client_contact'])
def test_created_document_mid_is_reported_when_reference_fetch_fails(backend, field):
    """作成後に取引先か担当者を取れなくても、作成済みの書類の mid を知らせる。"""
    created = {**resource('document'), 'mid': 'example-created-mid'}
    def override(request):
        if request.method == 'POST' and request.url.path.endswith('/document/'):
            return envelope(created)
        if str(request.url) == created[field]:
            return httpx.Response(403)
    backend.override = override
    async def scenario():
        async with client(backend) as api:
            with pytest.raises(MakeLeapsError) as caught:
                await api.create_document({'title': 'Example'})
            message = str(caught.value)
            assert 'example-created-mid' in message
            assert 'was created' in message
            assert '403' in message
    run(scenario())
    assert sum(r.method == 'POST' and r.url.path.endswith('/document/') for r in backend.requests) == 1


@pytest.mark.req('SP-MLC-013')
@pytest.mark.req('SP-MLC-010')
@pytest.mark.parametrize('failure', ['read_timeout', 'server_error'])
def test_authentication_post_retries_transient_failures_before_creating_document(backend, retry_clock, failure):
    """認証 POST は一時的な通信の失敗と 5xx を再送し、認証後に書類を作成する。"""
    attempts = []
    def override(request):
        if request.url.path == '/user/oauth2/token/':
            attempts.append(request)
            if len(attempts) == 1:
                if failure == 'read_timeout':
                    raise httpx.ReadTimeout('synthetic token timeout', request=request)
                return httpx.Response(503)
        if request.method == 'POST' and request.url.path.endswith('/document/'):
            return envelope(resource('document'))
    backend.override = override
    async def scenario():
        async with client(backend) as api:
            assert (await api.create_document({})).mid == 'example-document'
    run(scenario())
    assert len(attempts) == 2
    assert sum(r.method == 'POST' and r.url.path.endswith('/document/') for r in backend.requests) == 1


@pytest.mark.req('SP-MLC-013')
@pytest.mark.parametrize('operation,method', [('get_document', 'GET'), ('update_document', 'PATCH'), ('delete_document', 'DELETE')])
@pytest.mark.parametrize('failure', ['read_timeout', 'server_error'])
def test_existing_document_operations_keep_retrying_transient_failures(backend, retry_clock, operation, method, failure):
    """読み取り、同じ内容の編集、削除は従来どおり通信の失敗と 5xx を再送する。"""
    attempts = []
    def override(request):
        if request.method == method and request.url.path.endswith('/document/one/'):
            attempts.append(request)
            if len(attempts) == 1:
                if failure == 'read_timeout':
                    raise httpx.ReadTimeout('synthetic read timeout', request=request)
                return httpx.Response(503)
            return httpx.Response(204) if method == 'DELETE' else envelope(resource('document'))
    backend.override = override
    async def scenario():
        async with client(backend) as api:
            args = ('one', {'title': 'Example'}) if method == 'PATCH' else ('one',)
            result = await getattr(api, operation)(*args)
            assert result is None if method == 'DELETE' else isinstance(result, Document)
    run(scenario())
    assert len(attempts) == 2
