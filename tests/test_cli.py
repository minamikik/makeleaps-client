import asyncio
import io
import json

import httpx
import pytest
import yaml

from makeleaps_client.cli import main
from conftest import BASE, PDF, envelope, resource


@pytest.mark.req('SP-MLC-014')
@pytest.mark.req('SP-MLC-035')
@pytest.mark.req('SP-MLC-036')
@pytest.mark.req('SP-MLC-029')
@pytest.mark.parametrize('args,kind', [(['partner'], 'partner'), (['documents', 'get', 'example-document'], 'document'), (['documents', 'list'], 'documents'), (['archives', 'list'], 'archives')])
def test_commands_output_complete_models_as_one_yaml_value(cli_transport, capsys, args, kind):
    """会社と書類と保存記録の全体を、一つの YAML として読める。"""
    assert main(args) == 0
    output = capsys.readouterr()
    assert output.err == ''
    values = list(yaml.safe_load_all(output.out))
    assert len(values) == 1
    if kind in ('documents', 'archives'):
        assert len(values[0]) == 3
        value = values[0][0]
        assert value['mid'] == f'example-{kind[:-1]}-0'
    else:
        value = values[0]
    expected = resource('archive' if kind == 'archives' else 'document' if kind in ('document', 'documents') else 'partner')
    if kind in ('document', 'documents'):
        expected.update(client=resource('client'), client_contact=resource('contact'))
    # List rows have synthetic mids and types distinct from the detail fixture.
    for key in ('mid', 'url', 'pdf_content_url', 'document_type'):
        if key in expected:
            expected[key] = value[key]
    assert value == expected


@pytest.mark.req('SP-MLC-010')
@pytest.mark.req('SP-MLC-036')
@pytest.mark.parametrize('missing', ['MAKELEAPS_CLIENT_ID', 'MAKELEAPS_CLIENT_SECRET'])
def test_missing_credentials_fail_before_requests(cli_transport, capsys, monkeypatch, missing):
    """必要な認証情報が無ければ、request を送らず標準エラーで知らせる。"""
    monkeypatch.delenv(missing)
    assert main(['partner']) != 0
    output = capsys.readouterr()
    assert output.out == ''
    assert missing in output.err
    assert cli_transport.requests == []


@pytest.mark.req('SP-MLC-029')
@pytest.mark.req('SP-MLC-026')
def test_cli_filters_language_and_limit_reach_the_library(cli_transport, capsys):
    """cli の絞り込みと言語を送り、件数の上限を超えて返さない。"""
    cli_transport.document_count = 201
    assert main(['documents', 'list', '--type', 'quote', '--date-from', '2025-01-01', '--date-to', '2025-12-31', '--filter', 'sent=true', '--filter', 'document_number=A', '--filter', 'document_number=B', '--limit', '2', '--language', 'en']) == 0
    assert len(yaml.safe_load(capsys.readouterr().out)) == 2
    req = next(r for r in cli_transport.requests if r.url.path.endswith('/document/'))
    assert req.url.params['document_type'] == 'quote'
    assert req.url.params['date__gte'] == '2025-01-01'
    assert req.url.params['date__lte'] == '2025-12-31'
    assert req.url.params.get_list('document_number') == ['A', 'B']
    assert req.url.params['sent'] == 'true'
    assert all(r.headers['accept-language'] == 'en' for r in cli_transport.requests)
    assert all(int(r.url.params.get('page', '1')) == 1 for r in cli_transport.requests)


@pytest.mark.req('SP-MLC-036')
@pytest.mark.req('SP-MLC-029')
@pytest.mark.parametrize('kind', ['documents', 'archives'])
def test_single_pdf_writes_exact_bytes(cli_transport, capsys, tmp_path, kind):
    """書類と保存記録の PDF を指定した file に bytes のまま保存する。"""
    output = tmp_path / 'example.pdf'
    assert main([kind, 'pdf', 'one', '--output', str(output), '--language', 'en']) == 0
    assert output.read_bytes() == PDF
    assert all(r.headers['accept-language'] == 'en' for r in cli_transport.requests)
    assert capsys.readouterr().err == ''


@pytest.mark.req('SP-MLC-037')
@pytest.mark.req('SP-MLC-029')
@pytest.mark.parametrize('kind', ['documents', 'archives'])
def test_bulk_pdf_names_sanitization_limit_and_skip(cli_transport, capsys, tmp_path, kind):
    """PDF を規定の名前で保存し、同じ mid の file は取り直さない。"""
    cli_transport.document_count = cli_transport.archive_count = 205
    cli_transport.document_number = 'EX/A:B*?"<>|\\\x00'
    target = tmp_path / 'new' / 'pdfs'
    assert main([kind, 'pdf', '--dir', str(target), '--limit', '2']) == 0
    files = sorted(target.iterdir())
    assert len(files) == 2
    singular = kind[:-1]
    assert files[0].name == f'2025-04-12_invoice_EX_A_B_________example-{singular}-0.pdf'
    assert all(f.read_bytes() == PDF for f in files)
    calls = sum(r.url.path.endswith('/pdf/') for r in cli_transport.requests)
    # An earlier export's number can differ; matching is by mid.
    files[0].rename(target / f'old_number_example-{singular}-0.pdf')
    assert main([kind, 'pdf', '--dir', str(target), '--limit', '2']) == 0
    assert sum(r.url.path.endswith('/pdf/') for r in cli_transport.requests) == calls
    assert capsys.readouterr().err == ''


@pytest.mark.req('SP-MLC-036')
@pytest.mark.req('SP-MLC-011')
def test_cli_api_failure_has_nonzero_status_and_safe_stderr(cli_transport, capsys):
    """取得の失敗を終了 code と標準エラーで知らせ、認証情報を出さない。"""
    import httpx
    cli_transport.override = lambda r: httpx.Response(403, text='example-id example-secret example-token-1') if r.url.path == '/api/' else None
    assert main(['partner']) != 0
    output = capsys.readouterr()
    assert output.out == ''
    assert '403' in output.err
    assert not any(s in output.err for s in ('example-id', 'example-secret', 'example-token-1'))


@pytest.mark.req('SP-MLC-029')
@pytest.mark.parametrize('args', [ ['documents', 'list', '--limit', '-1'], ['documents', 'list', '--filter', 'missing-equals'], ['documents', 'list', '--date-from', 'bad-date'], ['documents', 'pdf', 'one', '--dir', 'out'], ['archives', 'pdf', '--output', 'one.pdf'], ['documents', 'pdf', 'one'], ['archives', 'pdf', 'one', '--output', 'one.pdf', '--dir', 'out'] ])
def test_invalid_options_fail_before_http(cli_transport, args):
    """不正な引数の組合せは、request を送る前に拒否する。"""
    with pytest.raises(SystemExit) as caught:
        main(args)
    assert caught.value.code != 0
    assert cli_transport.requests == []


@pytest.mark.req('SP-MLC-036')
@pytest.mark.parametrize('kind', ['documents', 'archives'])
def test_list_flushes_first_record_before_later_pages(cli_transport, monkeypatch, kind):
    """最初の要素を後続ページより先に flush し、全件を一つの YAML の列で返す。"""
    class Output(io.StringIO):
        flushes = 0
        def flush(self):
            self.flushes += 1
            super().flush()
    output = Output()
    monkeypatch.setattr('sys.stdout', output)
    cli_transport.document_count = cli_transport.archive_count = 201
    singular = kind[:-1]
    def override(request):
        if request.url.path.endswith(f'/{singular}/') and int(request.url.params.get('page', '1')) > 1:
            assert output.flushes > 0, 'The first record must be flushed before later pages'
            assert yaml.safe_load(output.getvalue())[0]['mid'] == f'example-{singular}-0'
    cli_transport.override = override
    assert main([kind, 'list']) == 0
    values = list(yaml.safe_load_all(output.getvalue()))
    assert len(values) == 1
    assert [v['mid'] for v in values[0]] == [f'example-{singular}-{i}' for i in range(201)]


@pytest.mark.req('SP-MLC-036')
@pytest.mark.parametrize('kind', ['documents', 'archives'])
def test_list_keeps_completed_rows_when_a_later_page_fails(cli_transport, capsys, kind):
    """後続ページに失敗しても取得済みの要素を残し、標準エラーと終了 code で知らせる。"""
    cli_transport.document_count = cli_transport.archive_count = 101
    singular = kind[:-1]
    cli_transport.override = lambda r: httpx.Response(403) if r.url.path.endswith(f'/{singular}/') and r.url.params.get('page') == '2' else None
    assert main([kind, 'list']) != 0
    output = capsys.readouterr()
    values = yaml.safe_load(output.out)
    assert isinstance(values, list) and len(values) == 100
    assert values[-1]['mid'] == f'example-{singular}-99'
    assert '403' in output.err


@pytest.mark.req('SP-MLC-036')
@pytest.mark.req('SP-MLC-029')
@pytest.mark.parametrize('kind', ['documents', 'archives'])
@pytest.mark.parametrize('limit', [None, '0'])
def test_empty_list_is_one_empty_yaml_sequence(cli_transport, capsys, kind, limit):
    """結果が無い場合と件数の上限がゼロの場合は、空の YAML の列を返す。"""
    cli_transport.document_count = cli_transport.archive_count = 0
    args = [kind, 'list'] + (['--limit', limit] if limit is not None else [])
    assert main(args) == 0
    output = capsys.readouterr()
    assert list(yaml.safe_load_all(output.out)) == [[]]
    assert output.err == ''
    if limit == '0':
        assert cli_transport.requests == []


@pytest.mark.req('SP-MLC-036')
@pytest.mark.parametrize('c_available', [False, True])
def test_list_uses_available_safe_yaml_dumper(cli_transport, capsys, monkeypatch, c_available):
    """libyaml があれば C の dumper を使い、無ければ Python の安全な dumper で返す。"""
    if c_available and not hasattr(yaml, 'CSafeDumper'):
        pytest.skip('libyaml is not installed')
    base = yaml.CSafeDumper if c_available else yaml.SafeDumper
    serialized = []
    class Dumper(base):
        def represent_data(self, data):
            if isinstance(data, dict) and 'mid' in data:
                serialized.append(data['mid'])
            return super().represent_data(data)
    if c_available:
        monkeypatch.setattr(yaml, 'CSafeDumper', Dumper)
    else:
        monkeypatch.delattr(yaml, 'CSafeDumper', raising=False)
        monkeypatch.setattr(yaml, 'SafeDumper', Dumper)
    assert main(['archives', 'list']) == 0
    assert serialized == [f'example-archive-{i}' for i in range(3)]
    assert len(yaml.safe_load(capsys.readouterr().out)) == 3


@pytest.mark.req('SP-MLC-007')
@pytest.mark.req('SP-MLC-013')
@pytest.mark.parametrize('kind', ['documents', 'archives'])
def test_bulk_pdf_fetches_overlap_and_skip_detail_requests(cli_transport, capsys, tmp_path, kind):
    """一覧に PDF URL があれば詳細を取り直さず、複数の PDF を同時に保存する。"""
    active = 0
    max_active = 0
    cli_transport.document_count = cli_transport.archive_count = 26
    async def override(request):
        nonlocal active, max_active
        if request.url.path.endswith('/pdf/'):
            active += 1
            max_active = max(active, max_active)
            await asyncio.sleep(0)
            active -= 1
            return httpx.Response(200, content=PDF)
    cli_transport.override = override
    assert main([kind, 'pdf', '--dir', str(tmp_path)]) == 0
    assert 1 < max_active <= 8
    assert len(list(tmp_path.iterdir())) == 26
    assert all(p.read_bytes() == PDF for p in tmp_path.iterdir())
    singular = kind[:-1]
    assert not any(r.url.path.startswith(f'/api/partner/example-company/{singular}/example-{singular}-') and not r.url.path.endswith('/pdf/') for r in cli_transport.requests)
    assert capsys.readouterr().err == ''


@pytest.mark.req('SP-MLC-013')
@pytest.mark.parametrize('kind', ['documents', 'archives'])
def test_bulk_pdf_retries_after_wait_and_shares_request_limit(cli_transport, capsys, tmp_path, monkeypatch, kind):
    """並行する PDF の取得でも 429 の待機と毎分の回数の上限を守る。"""
    import makeleaps_client.client as module
    now = [0.0]
    waits = []
    times = []
    attempts = {}
    pdf_times = {}
    async def sleep(delay):
        waits.append(delay)
        now[0] += delay
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(module.asyncio, 'sleep', sleep)
    cli_transport.document_count = cli_transport.archive_count = 260
    def override(request):
        times.append(now[0])
        if request.url.path.endswith('/pdf/'):
            key = request.url.path
            pdf_times.setdefault(key, []).append(now[0])
            attempts[key] = attempts.get(key, 0) + 1
            if key.endswith('-0/pdf/') and attempts[key] == 1:
                return httpx.Response(429, headers={'Retry-After': '7'})
    cli_transport.override = override
    assert main([kind, 'pdf', '--dir', str(tmp_path)]) == 0
    assert len(list(tmp_path.iterdir())) == 260
    assert len(attempts) == 260 and max(attempts.values()) == 2
    retried = next(stamps for key, stamps in pdf_times.items() if key.endswith('-0/pdf/'))
    assert retried[1] - retried[0] >= 7
    assert any(delay >= 7 for delay in waits)
    assert now[0] >= 60
    assert all(sum(t <= stamp < t + 60 for stamp in times) <= 250 for t in times)
    assert capsys.readouterr().err == ''


@pytest.mark.req('SP-MLC-037')
@pytest.mark.req('SP-MLC-036')
@pytest.mark.parametrize('kind', ['documents', 'archives'])
def test_bulk_pdf_falls_back_to_detail_without_list_pdf_url(cli_transport, capsys, tmp_path, kind):
    """一覧に PDF URL が無ければ詳細から取得し、同じ mid は一度だけ保存する。"""
    singular = kind[:-1]
    row = {**resource(singular), 'mid': 'one'}
    row.pop('pdf_content_url')
    cli_transport.override = lambda r: envelope([row, row], count=2, page=1, next=None) if r.url.path.endswith(f'/{singular}/') else None
    assert main([kind, 'pdf', '--dir', str(tmp_path)]) == 0
    assert len(list(tmp_path.iterdir())) == 1
    assert next(tmp_path.iterdir()).read_bytes() == PDF
    assert sum(r.url.path.endswith(f'/{singular}/one/') for r in cli_transport.requests) == 1
    assert sum(r.url.path.endswith('/pdf/') for r in cli_transport.requests) == 1
    assert capsys.readouterr().err == ''


@pytest.mark.req('SP-MLC-044')
@pytest.mark.parametrize('elements', [
    ('makeleaps documents get', '> document.yaml', 'makeleaps documents create --file',
     'copied', 'existing document'),
    ('change', 'document_number', 'dates', 'lineitems', 'same', 'number', 'content'),
    ('makeleaps api', 'makeleaps-client', 'do not fill', 'recipient_contact_name',
     'recipient_postal_code', 'sender_contact_name', 'sender_postal_code',
     'client', 'client_contact', 'company', 'from scratch', 'input'),
    ('makeleaps-client', 'autocalculate', 'true', 'unit costs', 'quantities',
     'normal and subtotal line item', 'subtotals', 'taxes', 'totals', 'omitted', 'input'),
    ('updates without line items', 'sent as supplied', 'does not recalculate'),
], ids=['reuse-document', 'change-number-dates-lineitems', 'supply-recipient-sender', 'calculated-totals', 'update-without-lineitems'])
def test_create_help_explains_reusing_documents_and_required_input(cli_transport, capsys, elements):
    """create の help は書類の再利用と必要な入力、明細の有無による合計の扱いを伝える。"""
    with pytest.raises(SystemExit) as caught:
        main(['documents', 'create', '--help'])
    assert caught.value.code == 0
    output = capsys.readouterr()
    help_text = ' '.join(output.out.lower().split())
    for element in elements:
        assert element in help_text
    assert output.err == ''
    assert cli_transport.requests == []


@pytest.mark.req('SP-MLC-044')
@pytest.mark.req('SP-MLC-040')
@pytest.mark.req('SP-MLC-029')
@pytest.mark.parametrize('operation,method', [('create', 'POST'), ('update', 'PATCH')])
@pytest.mark.parametrize('source', ['file', 'stdin'])
def test_document_write_commands_read_yaml_and_output_expanded_document(cli_transport, capsys, tmp_path, monkeypatch, operation, method, source):
    """作成と明細を含む編集は YAML の合計と通常・小計の明細の金額を送らず、自動計算を有効にして展開した書類を返す。"""
    raw = {**resource('document'), 'client': resource('client'), 'client_contact': resource('contact')}
    raw['lineitems'] = [{**resource('lineitem'), 'kind': kind} for kind in ('normal', 'simple', 'subtotal')]
    text = yaml.safe_dump(raw, allow_unicode=True)
    if source == 'file':
        path = tmp_path / 'document.yaml'
        path.write_text(text, encoding='utf-8')
        filename = str(path)
    else:
        monkeypatch.setattr('sys.stdin', io.StringIO(text))
        filename = '-'
    cli_transport.override = lambda r: envelope(resource('document')) if r.method == method and '/document/' in r.url.path else None
    args = ['documents', operation] + (['example-document'] if operation == 'update' else [])
    assert main([*args, '--file', filename, '--language', 'en']) == 0
    output = capsys.readouterr()
    assert output.err == ''
    expected = {**resource('document'), 'client': resource('client'), 'client_contact': resource('contact')}
    assert list(yaml.safe_load_all(output.out)) == [expected]
    writes = [r for r in cli_transport.requests if r.method == method and '/document/' in r.url.path]
    assert len(writes) == 1
    assert str(writes[0].url) == BASE + 'document/' + ('example-document/' if operation == 'update' else '')
    payload = json.loads(writes[0].content)
    assert payload['client'] == raw['client']['url']
    assert payload['client_contact'] == raw['client_contact']['url']
    assert 'mid' not in payload and 'url' not in payload
    assert not set(payload) & {
        'subtotal', 'tax', 'total', 'mixed_tax_rate_totals', 'tax_group_totals',
        'subtotal_sales_tax', 'subtotal_withholding_tax', 'jp_withholding_tax',
        'round_tax_down',
    }
    assert payload['tax_rounding'] == raw['tax_rounding']
    assert payload['autocalculate'] is True
    assert payload['lineitems'] == [
        {k: v for k, v in raw['lineitems'][0].items() if k != 'price'},
        raw['lineitems'][1],
        {k: v for k, v in raw['lineitems'][2].items() if k != 'price'},
    ]
    assert all(r.headers['accept-language'] == 'en' for r in cli_transport.requests)


@pytest.mark.req('SP-MLC-040')
@pytest.mark.req('SP-MLC-044')
def test_update_yaml_without_lineitems_preserves_totals(cli_transport, capsys, tmp_path):
    """明細を含まない YAML の編集は合計を保持し、autocalculate を足さない。"""
    raw = {k: v for k, v in resource('document').items() if k != 'lineitems'}
    path = tmp_path / 'document.yaml'
    path.write_text(yaml.safe_dump(raw), encoding='utf-8')
    cli_transport.override = lambda r: envelope(resource('document')) if r.method == 'PATCH' else None
    assert main(['documents', 'update', 'example-document', '--file', str(path)]) == 0
    output = capsys.readouterr()
    assert output.err == ''
    assert yaml.safe_load(output.out)['mid'] == 'example-document'
    writes = [r for r in cli_transport.requests if r.method == 'PATCH']
    assert len(writes) == 1
    payload = json.loads(writes[0].content)
    assert 'autocalculate' not in payload
    for field in ('subtotal', 'tax', 'total', 'jp_withholding_tax', 'subtotal_sales_tax',
                  'subtotal_withholding_tax', 'mixed_tax_rate_totals', 'tax_group_totals'):
        assert payload[field] == raw[field]


@pytest.mark.req('SP-MLC-040')
@pytest.mark.req('SP-MLC-044')
@pytest.mark.parametrize('operation', ['create', 'update'])
def test_document_write_yaml_preserves_dates_false_null_and_numbers(cli_transport, capsys, tmp_path, operation):
    """YAML の日付と日時を API の値に戻し、false、null、数値を保つ。"""
    path = tmp_path / 'document.yaml'
    path.write_text('date: 2025-04-12\ndate_paid: 2025-04-12T01:02:03Z\nhas_hanko: false\ndate_due: null\nfuture_amount: 12\n', encoding='utf-8')
    cli_transport.override = lambda r: envelope(resource('document')) if r.method in ('POST', 'PATCH') and '/document/' in r.url.path else None
    args = ['documents', operation] + (['one'] if operation == 'update' else [])
    assert main([*args, '--file', str(path)]) == 0
    write = next(r for r in cli_transport.requests if r.method in ('POST', 'PATCH') and '/document/' in r.url.path)
    expected = {'date': '2025-04-12', 'date_paid': '2025-04-12T01:02:03Z', 'has_hanko': False, 'date_due': None, 'future_amount': 12}
    if operation == 'create':
        expected['autocalculate'] = True
    assert json.loads(write.content) == expected
    assert capsys.readouterr().err == ''


@pytest.mark.req('SP-MLC-044')
@pytest.mark.req('SP-MLC-045')
@pytest.mark.req('SP-MLC-029')
def test_delete_command_requires_yes_before_requests_and_outputs_nothing(cli_transport, capsys):
    """削除は --yes が無ければ request を送らず、承認後は標準出力に何も出さない。"""
    assert main(['documents', 'delete', 'one']) != 0
    output = capsys.readouterr()
    assert output.out == '' and '--yes' in output.err
    assert cli_transport.requests == []
    cli_transport.override = lambda r: httpx.Response(204) if r.method == 'DELETE' else None
    assert main(['documents', 'delete', 'one', '--yes', '--language', 'en']) == 0
    output = capsys.readouterr()
    assert output.out == output.err == ''
    assert sum(r.method == 'DELETE' and str(r.url) == BASE + 'document/one/' for r in cli_transport.requests) == 1
    assert all(r.headers['accept-language'] == 'en' for r in cli_transport.requests)


@pytest.mark.req('SP-MLC-044')
@pytest.mark.parametrize('operation', ['create', 'update'])
@pytest.mark.parametrize('text', ['', '[]', '- item', 'scalar', '42', 'value: [', '!!python/object:invalid {}'])
def test_document_write_rejects_invalid_yaml_before_requests(cli_transport, capsys, tmp_path, operation, text):
    """作成と編集は mapping 以外と不正な YAML を request の前に拒否する。"""
    path = tmp_path / 'invalid.yaml'
    path.write_text(text, encoding='utf-8')
    args = ['documents', operation] + (['one'] if operation == 'update' else [])
    assert main([*args, '--file', str(path)]) != 0
    output = capsys.readouterr()
    assert output.out == '' and 'YAML' in output.err
    assert cli_transport.requests == []


@pytest.mark.req('SP-MLC-044')
def test_document_write_reports_unreadable_file_without_requests(cli_transport, capsys, tmp_path):
    """入力 file が読めなければ request を送らず、標準エラーで失敗を知らせる。"""
    assert main(['documents', 'create', '--file', str(tmp_path / 'missing.yaml')]) != 0
    output = capsys.readouterr()
    assert output.out == '' and output.err
    assert cli_transport.requests == []


@pytest.mark.req('SP-MLC-046')
@pytest.mark.req('SP-MLC-011')
@pytest.mark.parametrize('operation', ['create', 'update'])
def test_document_write_errors_reach_stderr_without_credentials(cli_transport, capsys, tmp_path, operation):
    """API の入力エラーを field ごとに標準エラーへ出し、認証情報と token を出さない。"""
    path = tmp_path / 'document.yaml'
    path.write_text('{}', encoding='utf-8')
    cli_transport.override = lambda r: httpx.Response(400, json={'response': {
        'currency': ['Choose an example currency.'],
        'non_field_errors': ['Cannot save example document. example-id example-secret example-token-1'],
    }}) if '/document/' in r.url.path else None
    args = ['documents', operation] + (['one'] if operation == 'update' else [])
    assert main([*args, '--file', str(path)]) != 0
    output = capsys.readouterr()
    assert output.out == ''
    for value in ('HTTP 400', 'currency', 'Choose an example currency.', 'non_field_errors', 'Cannot save example document.'):
        assert value in output.err
    assert not any(secret in output.err for secret in ('example-id', 'example-secret', 'example-token-1'))


@pytest.mark.req('SP-MLC-046')
@pytest.mark.req('SP-MLC-011')
@pytest.mark.parametrize('operation', ['create', 'update'])
def test_nested_document_write_errors_reach_stderr_with_row_numbers(cli_transport, capsys, tmp_path, operation):
    """cli は入れ子の入力エラーの明細番号と内容を標準エラーへ出し、認証情報を除く。"""
    path = tmp_path / 'document.yaml'
    path.write_text('{}', encoding='utf-8')
    cli_transport.override = lambda r: httpx.Response(400, json={'response': {
        'lineitems': [{}, {'has_jp_withholding_tax': [
            'Example withholding flag is required. example-id example-secret example-token-1',
        ]}, {}],
    }}) if '/document/' in r.url.path else None
    args = ['documents', operation] + (['one'] if operation == 'update' else [])
    assert main([*args, '--file', str(path)]) != 0
    output = capsys.readouterr()
    assert output.out == ''
    assert 'HTTP 400' in output.err
    assert 'lineitems.1.has_jp_withholding_tax: Example withholding flag is required.' in output.err
    assert 'lineitems.0' not in output.err and 'lineitems.2' not in output.err
    assert not any(secret in output.err for secret in ('example-id', 'example-secret', 'example-token-1'))
