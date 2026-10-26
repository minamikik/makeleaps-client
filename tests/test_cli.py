import pytest
import yaml

from makeleaps_client.cli import main
from conftest import PDF, resource


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
