import asyncio
import io

import httpx
import pytest
import yaml

from makeleaps_client.cli import main
from conftest import PDF, envelope, resource


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
