import asyncio
import base64
from datetime import date, datetime
from decimal import Decimal
import inspect

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
    """書類を順に全件返し、取引先と担当者の一覧を操作ごとに一度だけ取る。"""
    backend.document_count = 1005
    async def scenario():
        async with client(backend) as api:
            docs = [d async for d in api.list_documents()]
            assert [d.mid for d in docs] == [f'example-document-{i}' for i in range(1005)]
            assert all(d.client.mid == 'example-client' for d in docs)
            await api.get_document('one')
    run(scenario())
    assert backend.max_active_pages > 1
    paths = [r.url.path for r in backend.requests]
    assert paths.count('/api/partner/example-company/client/') == 2
    assert paths.count('/api/partner/example-company/contact/') == 2
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
def test_first_document_page_and_related_lists_overlap(backend):
    """最初の書類のページと取引先と担当者の一覧を同時に取る。"""
    async def scenario():
        backend.page_gate = asyncio.Event()
        async with client(backend) as api:
            async def collect():
                return [d async for d in api.list_documents()]
            docs = await asyncio.wait_for(collect(), timeout=1)
            assert len(docs) == 3
    run(scenario())
    assert backend.max_active_pages >= 3
