"""Compact document YAML from the CLI, using synthetic responses."""
from copy import deepcopy

import pytest
import yaml

from makeleaps_client.cli import main
from conftest import envelope, resource


@pytest.mark.req('SP-MLC-066')
@pytest.mark.parametrize('operation', ['get', 'create', 'update'])
@pytest.mark.parametrize('kind,deadline', [
    ('invoice', 'date_due'), ('quote', 'date_valid'), ('order', 'date_valid'), ('deliveryslip', None),
])
def test_summary_yaml_contains_only_document_details_lineitem_values_and_totals(cli_transport, capsys, tmp_path, operation, kind, deadline):
    """短い表示は API の key で番号、種類、発行日、対応する期限、案件名、明細と合計だけを出す。"""
    raw = {**resource('document'), 'document_type': kind, 'date_due': '2025-05-20', 'date_valid': '2025-05-31'}
    raw['lineitems'] = [
        {**resource('lineitem'), 'kind': line_kind, 'description': 'Example service', 'future_field': 'omit'}
        for line_kind in ('normal', 'simple', 'subtotal', 'text')
    ]
    before = deepcopy(raw)
    method = {'get': 'GET', 'create': 'POST', 'update': 'PATCH'}[operation]
    cli_transport.override = lambda r: envelope(raw) if r.method == method and '/document/' in r.url.path else None
    args = ['documents', operation]
    if operation != 'create':
        args += ['example-document']
    if operation != 'get':
        path = tmp_path / 'document.yaml'
        path.write_text('title: Example input\n')
        args += ['--file', str(path)]
    assert main([*args, '--summary']) == 0
    output = capsys.readouterr()
    assert output.err == ''
    summary = yaml.safe_load(output.out)
    expected = {key: raw[key] for key in ('document_number', 'document_type', 'date', 'project_name', 'subtotal', 'tax', 'total')}
    if deadline:
        expected[deadline] = raw[deadline]
    expected['lineitems'] = [
        {key: line[key] for key in ('kind', 'description', 'quantity', 'unit_cost', 'price')}
        for line in raw['lineitems']
    ]
    assert summary == expected
    assert all(isinstance(summary[key], str) for key in ('subtotal', 'tax', 'total'))
    assert raw == before


@pytest.mark.req('SP-MLC-066')
def test_summary_preserves_explicit_nulls_and_omits_fields_missing_from_the_api(cli_transport, capsys):
    """短い表示は明示的な null を保ち、API に無い field や明細の値を作らない。"""
    raw = {'document_type': 'invoice', 'date_due': None, 'project_name': None,
           'lineitems': [{'kind': 'text', 'description': 'Example note'}, {'kind': 'simple', 'price': '0'}],
           'future_field': 'omit'}
    cli_transport.override = lambda r: envelope(raw) if r.url.path.endswith('/document/example-sparse/') else None
    assert main(['documents', 'get', 'example-sparse', '--summary']) == 0
    assert yaml.safe_load(capsys.readouterr().out) == {key: val for key, val in raw.items() if key != 'future_field'}
