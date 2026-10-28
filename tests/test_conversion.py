"""Document conversion through synthetic MakeLeaps HTTP responses."""
import asyncio
from copy import deepcopy
from datetime import date, datetime, timezone
import json

import httpx
import pytest
import yaml

from makeleaps_client import MakeLeapsClient, MakeLeapsError
from makeleaps_client.cli import main
import makeleaps_client.client as client_module
from conftest import BASE, envelope, resource


def template(code, kind, language='ja', advanced=False):
    return {
        'template_code': code, 'document_type': kind, 'display_name': 'Example template',
        'language_code': language, 'base_template_code': 'example-base',
        'extended_fields': {}, 'lineitem_extended_fields': {},
        'use_advanced_tax_settings': advanced, 'settings': {}, 'is_favourite_template': False,
    }


def conversion_backend(backend, *, kind='quote', templates=None):
    source = {**resource('document'), 'document_type': kind, 'document_template': 'example-shared'}
    if templates is None:
        templates = {
            kind: [template('example-shared', kind)],
            'invoice': [template('example-shared', 'invoice')],
            'deliveryslip': [template('example-shared', 'deliveryslip')],
        }
    def override(request):
        path = request.url.path
        if request.method == 'GET' and path.endswith('/document/example-source/'):
            return envelope(deepcopy(source))
        if request.method == 'GET' and '/template/' in path:
            return envelope(templates[path.rstrip('/').rsplit('/', 1)[-1]])
        if request.method == 'POST' and path.endswith('/document/'):
            return envelope({**json.loads(request.content), 'mid': 'example-created',
                             'url': BASE + 'document/example-created/', 'total': '1100'})
    backend.override = override
    return source


def client(backend):
    return MakeLeapsClient('example-id', 'example-secret', transport=httpx.MockTransport(backend))


def payloads(backend):
    return [json.loads(r.content) for r in backend.requests
            if r.method == 'POST' and r.url.path.endswith('/document/')]


@pytest.mark.req('SP-MLC-062')
@pytest.mark.parametrize('kind,target', [('quote', 'invoice'), ('invoice', 'deliveryslip')])
@pytest.mark.parametrize('expanded', [False, True])
def test_conversion_copies_content_and_returns_the_created_document_with_client_and_contact(backend, kind, target, expanded):
    """見積書から請求書と請求書から納品書を、宛先と発行元と明細を保って作る。"""
    source = conversion_backend(backend, kind=kind)
    source['lineitems'] = [
        {'kind': 'normal', 'description': 'Example service', 'quantity': '2', 'unit_cost': '500',
         'price': '1000', 'has_jp_withholding_tax': False, 'position': 0},
        {'kind': 'simple', 'description': 'Example adjustment', 'price': '100', 'position': 1},
        {'kind': 'subtotal', 'description': 'Example subtotal', 'price': '1100', 'position': 2},
    ]
    source['example_extra'] = {'example': 'preserved'}
    if expanded:
        source['client'] = resource('client')
        source['client_contact'] = resource('contact')
    before = deepcopy(source)
    async def scenario():
        async with client(backend) as api:
            result = await api.convert_document('example-source', target, 'EXAMPLE-NEW', date='2025-05-06')
            assert result.mid == 'example-created'
            assert result.document_type == target
            assert result.document_number == 'EXAMPLE-NEW'
            assert result.date == date(2025, 5, 6)
            assert result.client.model_dump() == resource('client')
            assert result.client_contact.model_dump() == resource('contact')
            assert str(result.total) == '1100'
    asyncio.run(scenario())
    assert source == before
    [sent] = payloads(backend)
    assert (sent['document_type'], sent['document_number'], sent['document_template'], sent['date']) == (
        target, 'EXAMPLE-NEW', 'example-shared', '2025-05-06',
    )
    for key in ('title', 'project_name', 'note', 'currency', 'bank_details', 'message', 'tags',
                'purchase_order_ids', 'example_extra'):
        assert sent[key] == before[key]
    for key in before:
        if key.startswith(('sender_', 'recipient_')):
            assert sent[key] == before[key]
    assert sent['client'] == resource('document')['client']
    assert sent['client_contact'] == resource('document')['client_contact']
    assert sent['autocalculate'] is True
    assert 'round_tax_down' not in sent
    assert sent['tax_rounding'] == source['tax_rounding']
    assert sent['lineitems'] == [
        {key: value for key, value in source['lineitems'][0].items() if key != 'price'},
        source['lineitems'][1],
        {key: value for key, value in source['lineitems'][2].items() if key != 'price'},
    ]
    for key in ('mid', 'url', 'modified', 'display_name', 'pdf_content_url', 'pdf_upload_url',
                'date_converted', 'date_removed', 'is_accepted', 'is_confirmed', 'is_converted',
                'approval_status', 'integrations', 'pickup_links', 'subtotal', 'tax', 'total',
                'jp_withholding_tax', 'subtotal_sales_tax', 'subtotal_withholding_tax',
                'mixed_tax_rate_totals', 'tax_group_totals'):
        assert key not in sent
    assert not any(r.method in ('PATCH', 'DELETE') for r in backend.requests)


@pytest.mark.req('SP-MLC-062')
def test_conversion_defaults_to_the_issue_date_in_japan_even_before_utc_midnight(backend, monkeypatch):
    """発行日の指定が無い時は UTC の日付ではなく作成時点の日本の日付を使う。"""
    conversion_backend(backend)
    moment = datetime(2025, 5, 6, 16, 30, tzinfo=timezone.utc)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz is not None else moment.replace(tzinfo=None)
    monkeypatch.setattr(client_module, 'datetime', Clock)
    async def scenario():
        async with client(backend) as api:
            await api.convert_document('example-source', 'invoice', 'EXAMPLE-TODAY')
    asyncio.run(scenario())
    assert payloads(backend)[0]['date'] == '2025-05-07'


@pytest.mark.req('SP-MLC-062')
@pytest.mark.parametrize('value', [date(2025, 5, 6), '2025-05-06'])
def test_conversion_accepts_a_date_object_or_iso_date(backend, value):
    """指定された発行日は日付の object と ISO の文字列のどちらでも使える。"""
    conversion_backend(backend)
    async def scenario():
        async with client(backend) as api:
            await api.convert_document('example-source', 'invoice', 'EXAMPLE-DATE', date=value)
    asyncio.run(scenario())
    assert payloads(backend)[0]['date'] == '2025-05-06'


@pytest.mark.req('SP-MLC-062')
@pytest.mark.parametrize('value', ['invalid', '2025-02-30', '2025-05-06T12:00:00', 123])
def test_invalid_issue_dates_fail_before_creating_a_document(backend, value):
    """発行日として使えない値の時は書類を作らず日付の形式を説明する。"""
    conversion_backend(backend)
    async def scenario():
        async with client(backend) as api:
            with pytest.raises(MakeLeapsError, match='date'):
                await api.convert_document('example-source', 'invoice', 'EXAMPLE-DATE', date=value)
    asyncio.run(scenario())
    assert payloads(backend) == []


@pytest.mark.req('SP-MLC-062')
def test_conversion_uses_the_same_template_code_before_any_other_target_template(backend):
    """同じ code の template が変換先にある時は、一覧の先頭より優先する。"""
    conversion_backend(backend, templates={'invoice': [
        template('example-other', 'invoice', 'en'), template('example-shared', 'invoice'),
    ]})
    async def scenario():
        async with client(backend) as api:
            await api.convert_document('example-source', 'invoice', 'EXAMPLE-TEMPLATE')
    asyncio.run(scenario())
    assert payloads(backend)[0]['document_template'] == 'example-shared'
    requests = [r for r in backend.requests if '/template/' in r.url.path]
    assert len(requests) == 1
    assert requests[0].url.path.endswith('/template/invoice/')
    assert requests[0].url.params['hide_archived'] == 'y'
    assert requests[0].url.params['is_archived'] == 'false'


@pytest.mark.req('SP-MLC-062')
@pytest.mark.parametrize('advanced', [False, True])
@pytest.mark.parametrize('match_tax_settings', [False, True])
def test_conversion_selects_the_first_template_with_matching_language_and_prefers_tax_settings(backend, advanced, match_tax_settings):
    """同じ code が無い時は言語を保ち、同じ税設定があればそれを優先する。"""
    targets = [template('example-wrong-language', 'deliveryslip', 'ja', advanced),
               template('example-first-language', 'deliveryslip', 'en', not advanced)]
    if match_tax_settings:
        targets += [template('example-matching', 'deliveryslip', 'en', advanced),
                    template('example-later', 'deliveryslip', 'en', advanced)]
    conversion_backend(backend, kind='invoice', templates={
        'invoice': [template('example-unrelated-source', 'invoice', 'ja', not advanced),
                    template('example-shared', 'invoice', 'en', advanced)],
        'deliveryslip': targets,
    })
    async def scenario():
        async with client(backend) as api:
            await api.convert_document('example-source', 'deliveryslip', 'EXAMPLE-TEMPLATE')
    asyncio.run(scenario())
    expected = 'example-matching' if match_tax_settings else 'example-first-language'
    assert payloads(backend)[0]['document_template'] == expected
    assert sorted(r.url.path for r in backend.requests if '/template/' in r.url.path) == [
        '/api/partner/example-company/template/deliveryslip/', '/api/partner/example-company/template/invoice/',
    ]


@pytest.mark.req('SP-MLC-062')
@pytest.mark.parametrize('target_templates', [[], [template('example-english', 'invoice', 'en')]])
def test_conversion_without_a_same_language_template_requests_an_explicit_template_without_creating(backend, target_templates):
    """同じ言語の template が無い時は作成せず template の指定を促す。"""
    conversion_backend(backend, templates={
        'quote': [template('example-shared', 'quote')], 'invoice': target_templates,
    })
    async def scenario():
        async with client(backend) as api:
            with pytest.raises(MakeLeapsError) as caught:
                await api.convert_document('example-source', 'invoice', 'EXAMPLE-NONE')
            assert 'template' in str(caught.value).lower()
            assert 'specify' in str(caught.value).lower()
    asyncio.run(scenario())
    assert payloads(backend) == []


@pytest.mark.req('SP-MLC-062')
@pytest.mark.parametrize('source_templates', [[], [template('example-other', 'quote')],
                                           [{**template('example-shared', 'quote'), 'language_code': None}]])
def test_conversion_with_unknown_source_template_language_requests_an_explicit_template(backend, source_templates):
    """元の template の言語を確かめられない時は、推測せず明示指定を促す。"""
    conversion_backend(backend, templates={
        'quote': source_templates, 'invoice': [template('example-other', 'invoice')],
    })
    async def scenario():
        async with client(backend) as api:
            with pytest.raises(MakeLeapsError, match='[Ss]pecify.*template'):
                await api.convert_document('example-source', 'invoice', 'EXAMPLE-UNKNOWN')
    asyncio.run(scenario())
    assert payloads(backend) == []


@pytest.mark.req('SP-MLC-062')
def test_conversion_sends_an_explicit_template_without_reading_template_lists(backend):
    """template の指定があれば元の言語にかかわらず指定値で作る。"""
    conversion_backend(backend, templates={})
    async def scenario():
        async with client(backend) as api:
            await api.convert_document('example-source', 'invoice', 'EXAMPLE-EXPLICIT', template='example-explicit')
    asyncio.run(scenario())
    assert payloads(backend)[0]['document_template'] == 'example-explicit'
    assert not any('/template/' in r.url.path for r in backend.requests)


@pytest.mark.req('SP-MLC-062')
@pytest.mark.parametrize('options', [[], ['--template', 'example-explicit', '--date', '2025-05-06', '--language', 'en']])
def test_cli_conversion_outputs_complete_created_yaml_and_passes_options(cli_transport, capsys, options):
    """変換の command は新しい番号と種類と任意指定を使い、作成結果の全体を YAML で出す。"""
    conversion_backend(cli_transport)
    assert main(['documents', 'convert', 'example-source', '--type', 'invoice', '--number', 'EXAMPLE-CLI', *options]) == 0
    output = capsys.readouterr()
    assert output.err == ''
    value = yaml.safe_load(output.out)
    assert value['mid'] == 'example-created'
    assert value['document_number'] == 'EXAMPLE-CLI'
    assert value['document_type'] == 'invoice'
    assert value['client'] == resource('client')
    assert value['client_contact'] == resource('contact')
    assert value['sender_name'] == resource('document')['sender_name']
    assert value['lineitems']
    assert value['total'] == '1100'
    [sent] = payloads(cli_transport)
    if options:
        assert sent['document_template'] == 'example-explicit'
        assert sent['date'] == '2025-05-06'
    assert all(r.headers['Accept-Language'] == ('en' if options else 'ja') for r in cli_transport.requests)


@pytest.mark.req('SP-MLC-062')
@pytest.mark.parametrize('args,reason', [
    (['--type', 'invoice'], 'required: --number'),
    (['--number', 'EXAMPLE-CLI'], 'required: --type'),
    (['--type', 'invoice', '--number', 'EXAMPLE-CLI', '--date', '2025-02-30'], 'YYYY-MM-DD'),
])
def test_cli_conversion_requires_type_number_and_valid_issue_date_before_requests(cli_transport, capsys, args, reason):
    """種類と番号が無い時と不正な発行日の時は request を送らずに失敗する。"""
    with pytest.raises(SystemExit) as caught:
        main(['documents', 'convert', 'example-source', *args])
    assert caught.value.code == 2
    assert cli_transport.requests == []
    assert reason in capsys.readouterr().err


@pytest.mark.req('SP-MLC-062')
def test_cli_conversion_reports_missing_template_on_stderr_without_creating(cli_transport, capsys):
    """言語の合う template が無い時は標準エラーで指定を促し、書類を作らない。"""
    conversion_backend(cli_transport, templates={'quote': [template('example-shared', 'quote')], 'invoice': []})
    assert main(['documents', 'convert', 'example-source', '--type', 'invoice', '--number', 'EXAMPLE-CLI']) == 1
    output = capsys.readouterr()
    assert output.out == ''
    assert 'template' in output.err.lower()
    assert 'specify' in output.err.lower()
    assert payloads(cli_transport) == []


@pytest.mark.req('SP-MLC-062')
@pytest.mark.req('SP-MLC-064')
@pytest.mark.parametrize('target', ['invoice', 'quote', 'order', 'deliveryslip', 'paymentnotice', 'receipt', 'example-future-type'])
def test_conversion_drops_status_dates_and_moves_only_compatible_deadlines(backend, target):
    """状態の日付を写さず、意味が同じ期限の日数と請求対象期間だけを保つ。"""
    source = conversion_backend(backend, kind='invoice' if target == 'quote' else 'quote')
    source.update(date_due='2025-06-06', date_valid='2025-07-07')
    async def scenario():
        async with client(backend) as api:
            await api.convert_document('example-source', target, 'EXAMPLE-DATES',
                                       template='example-explicit', date='2025-05-06')
    asyncio.run(scenario())
    [sent] = payloads(backend)
    for key in ('date_paid', 'date_sent', 'date_cancelled', 'date_accepted', 'date_confirmed',
                'date_shipped', 'payment_date'):
        assert key not in sent
    assert 'date_due' not in sent
    if target == 'order':
        assert sent['date_valid'] == '2025-07-31'
    else:
        assert 'date_valid' not in sent
    for key in ('invoicing_period_start_date', 'invoicing_period_end_date'):
        assert sent[key] == source[key]


@pytest.mark.req('SP-MLC-062')
@pytest.mark.parametrize('stage', ['source', 'template', 'create'])
def test_conversion_propagates_api_failures_without_exposing_credentials_or_modifying_source(backend, stage):
    """読み取りと template と作成の失敗は、認証情報を除いた API の説明で返す。"""
    conversion_backend(backend)
    conversion = backend.override
    def override(request):
        matches = {
            'source': request.url.path.endswith('/document/example-source/'),
            'template': '/template/' in request.url.path,
            'create': request.method == 'POST' and request.url.path.endswith('/document/'),
        }
        if matches[stage]:
            return httpx.Response(400, json={'response': {'document_template': [
                'Example rejection example-secret example-token-1',
            ]}})
        return conversion(request)
    backend.override = override
    async def scenario():
        async with client(backend) as api:
            with pytest.raises(MakeLeapsError) as caught:
                await api.convert_document('example-source', 'invoice', 'EXAMPLE-FAIL')
            message = str(caught.value)
            assert 'HTTP 400' in message
            assert 'document_template' in message
            assert 'Example rejection' in message
            assert 'example-secret' not in message
            assert 'example-token-1' not in message
    asyncio.run(scenario())
    assert len(payloads(backend)) == (1 if stage == 'create' else 0)
    assert not any(r.method in ('PATCH', 'DELETE') for r in backend.requests)


@pytest.mark.req('SP-MLC-062')
@pytest.mark.parametrize('value', [None, {}, ['example-invalid-template']])
def test_conversion_rejects_unusable_template_responses_before_creating(backend, value):
    """template 一覧の形が壊れている時は書類を作らず失敗を返す。"""
    conversion_backend(backend)
    conversion = backend.override
    def override(request):
        if '/template/' in request.url.path:
            return envelope(value)
        return conversion(request)
    backend.override = override
    async def scenario():
        async with client(backend) as api:
            with pytest.raises(MakeLeapsError):
                await api.convert_document('example-source', 'invoice', 'EXAMPLE-INVALID')
    asyncio.run(scenario())
    assert payloads(backend) == []


@pytest.mark.req('SP-MLC-063')
@pytest.mark.req('SP-MLC-064')
def test_duplicate_keeps_the_source_template_even_when_not_in_the_active_list(backend):
    """同じ種類の複製は元の template を使い、宛先と内容を保つ。"""
    source = conversion_backend(backend, templates={'quote': []})
    async def scenario():
        async with client(backend) as api:
            result = await api.convert_document('example-source', 'quote', 'EXAMPLE-COPY', date='2025-05-06')
            assert result.document_type == 'quote'
            assert result.document_number == 'EXAMPLE-COPY'
            assert result.project_name == source['project_name']
    asyncio.run(scenario())
    [sent] = payloads(backend)
    assert sent['document_template'] == source['document_template']
    assert sent['recipient_name'] == source['recipient_name']
    assert sent['sender_name'] == source['sender_name']
    assert sent['lineitems'] == source['lineitems']


@pytest.mark.req('SP-MLC-064')
@pytest.mark.parametrize('kind,target,field', [
    ('invoice', 'invoice', 'date_due'), ('quote', 'quote', 'date_valid'),
    ('order', 'quote', 'date_valid'), ('quote', 'order', 'date_valid'),
])
def test_conversion_moves_a_compatible_deadline_by_the_original_number_of_days(backend, kind, target, field):
    """期限の意味が同じ種類への変換は、発行日からの日数を保って期限を計算する。"""
    source = conversion_backend(backend, kind=kind)
    source.update(date='2024-02-20', **{field: '2024-03-05'})
    async def scenario():
        async with client(backend) as api:
            await api.convert_document('example-source', target, 'EXAMPLE-MOVED',
                                       template='example-explicit', date='2025-12-25')
    asyncio.run(scenario())
    assert payloads(backend)[0][field] == '2026-01-08'


@pytest.mark.req('SP-MLC-064')
@pytest.mark.parametrize('kind,target,source_deadline', [
    ('quote', 'invoice', '2025-06-01'), ('invoice', 'quote', '2025-06-01'),
    ('deliveryslip', 'invoice', '2025-06-01'), ('invoice', 'invoice', None),
])
def test_conversion_omits_deadlines_without_a_compatible_source_value(backend, kind, target, source_deadline):
    """期限の意味が異なる時と元の期限が無い時は期限を送らない。"""
    source = conversion_backend(backend, kind=kind)
    source.update(date_due=source_deadline, date_valid=source_deadline)
    async def scenario():
        async with client(backend) as api:
            await api.convert_document('example-source', target, 'EXAMPLE-NONE',
                                       template='example-explicit', date='2025-05-06')
    asyncio.run(scenario())
    assert not {'date_due', 'date_valid'} & payloads(backend)[0].keys()


@pytest.mark.req('SP-MLC-064')
@pytest.mark.parametrize('target,field', [('invoice', 'date_due'), ('quote', 'date_valid'), ('order', 'date_valid')])
@pytest.mark.parametrize('options,expected', [
    ({'deadline': '2025-06-01'}, '2025-06-01'),
    ({'deadline': date(2025, 6, 1)}, '2025-06-01'),
    ({'deadline_days': 14}, '2025-05-20'),
    ({'deadline_days': 0}, '2025-05-06'),
    ({'deadline_days': -3}, '2025-05-03'),
])
def test_conversion_accepts_an_explicit_deadline_or_days_from_the_new_issue_date(backend, target, field, options, expected):
    """期限を日付または新しい発行日からの日数で指定し、作る種類の期限へ送る。"""
    conversion_backend(backend)
    async def scenario():
        async with client(backend) as api:
            await api.convert_document('example-source', target, 'EXAMPLE-DEADLINE',
                                       template='example-explicit', date='2025-05-06', **options)
    asyncio.run(scenario())
    [sent] = payloads(backend)
    assert sent[field] == expected
    assert ('date_valid' if field == 'date_due' else 'date_due') not in sent


@pytest.mark.req('SP-MLC-064')
@pytest.mark.parametrize('target,options', [
    ('invoice', {'deadline': '2025-06-01', 'deadline_days': 14}),
    ('invoice', {'deadline': '2025-02-30'}),
    ('invoice', {'deadline_days': 1.5}), ('invoice', {'deadline_days': True}),
    ('invoice', {'deadline_days': 10**20}),
    ('deliveryslip', {'deadline': '2025-06-01'}),
    ('receipt', {'deadline_days': 14}),
])
def test_invalid_or_unsupported_deadlines_fail_before_requests(backend, target, options):
    """期限の二重指定、不正値、期限を持たない種類では request を送らず失敗する。"""
    async def scenario():
        async with client(backend) as api:
            with pytest.raises(MakeLeapsError, match='[Dd]eadline'):
                await api.convert_document('example-source', target, 'EXAMPLE-INVALID', **options)
    asyncio.run(scenario())
    assert backend.requests == []


@pytest.mark.req('SP-MLC-064')
@pytest.mark.parametrize('options,expected', [
    (['--deadline', '2025-06-01'], '2025-06-01'),
    (['--deadline-days', '14'], '2025-05-20'),
])
def test_cli_duplicate_accepts_deadline_options(cli_transport, capsys, options, expected):
    """複製の command は期限を日付または発行日からの日数で受ける。"""
    conversion_backend(cli_transport)
    assert main(['documents', 'convert', 'example-source', '--type', 'quote',
                 '--number', 'EXAMPLE-COPY', '--date', '2025-05-06', *options]) == 0
    assert yaml.safe_load(capsys.readouterr().out)['date_valid'] == expected
    assert payloads(cli_transport)[0]['date_valid'] == expected


@pytest.mark.req('SP-MLC-064')
@pytest.mark.parametrize('options,reason', [
    (['--deadline', '2025-06-01', '--deadline-days', '14'], 'not allowed with'),
    (['--deadline', '2025-02-30'], 'YYYY-MM-DD'),
    (['--deadline-days', '1.5'], 'invalid int'),
])
def test_cli_invalid_deadline_options_fail_without_requests(cli_transport, capsys, options, reason):
    """期限の二重指定と不正な日付や日数は request の前に拒否する。"""
    with pytest.raises(SystemExit) as caught:
        main(['documents', 'convert', 'example-source', '--type', 'invoice', '--number', 'EXAMPLE-INVALID', *options])
    assert caught.value.code == 2
    error = capsys.readouterr().err
    assert 'deadline' in error
    assert reason in error
    assert cli_transport.requests == []
