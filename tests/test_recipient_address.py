"""Copying the selected recipient address during document creation."""
import asyncio
from copy import deepcopy
import io
import json

import httpx
import pytest
import yaml

from makeleaps_client import Document, MakeLeapsClient, MakeLeapsError
from makeleaps_client.cli import main
from conftest import BASE, envelope, resource

ADDRESS = {
    'format': 'jp_default', 'country_name': 'JP', 'postal_code': '000-1234',
    'region': 'tokyo', 'locality': 'Example City', 'street_address': '9-8-7',
    'extended_address': 'Example Building',
    'display': 'Example formatted address',
}
RECIPIENT = {
    'recipient_address_format': 'jp_default', 'recipient_country': 'JP',
    'recipient_postal_code': '000-1234', 'recipient_region': 'tokyo',
    'recipient_locality': 'Example City', 'recipient_street_address': '9-8-7',
    'recipient_extended_address': 'Example Building',
}


def client(backend):
    return MakeLeapsClient('example-id', 'example-secret', transport=httpx.MockTransport(backend))


def address_backend(backend, address=ADDRESS):
    contact = {**resource('contact'), 'addresses': [address]}
    def override(request):
        if str(request.url) == BASE + 'contact/example-contact/':
            return envelope(contact)
        if request.method == 'POST' and request.url.path.endswith('/document/'):
            return envelope({**resource('document'), **json.loads(request.content)})
    backend.override = override


@pytest.mark.req('SP-MLC-058')
@pytest.mark.parametrize('input_form', ['urls', 'expanded', 'model'])
def test_creation_copies_the_document_contact_address_and_preserves_other_fields(backend, input_form):
    """担当者の住所を選ぶと住所の全 field を写し、名前と電話などは入力のまま送る。"""
    address_backend(backend)
    value = resource('document')
    value.update(recipient_name='Example Recipient', recipient_contact_name='Example Person',
                 recipient_phone_number='00-0000-0000', sender_postal_code='111-1111')
    if input_form != 'urls':
        value.update(client=resource('client'), client_contact=resource('contact'))
    if input_form == 'model':
        value = Document.model_validate(value)
    before = deepcopy(value)
    async def scenario():
        async with client(backend) as api:
            result = await api.create_document(value, recipient_address='contact')
            assert result.recipient_postal_code == ADDRESS['postal_code']
            assert result.client_contact.mid == 'example-contact'
    asyncio.run(scenario())
    assert value == before
    payload = json.loads(next(r for r in backend.requests if r.method == 'POST' and '/document/' in r.url.path).content)
    assert {field: payload[field] for field in RECIPIENT} == RECIPIENT
    assert payload['recipient_name'] == 'Example Recipient'
    assert payload['recipient_contact_name'] == 'Example Person'
    assert payload['recipient_phone_number'] == '00-0000-0000'
    assert payload['sender_postal_code'] == '111-1111'
    assert payload['client_contact'] == BASE + 'contact/example-contact/'


@pytest.mark.req('SP-MLC-058')
def test_missing_parts_of_selected_address_clear_previous_document_address_parts(backend):
    """選んだ住所に建物名が無い時は、元の書類の建物名を残さない。"""
    address_backend(backend, {k: v for k, v in ADDRESS.items() if k != 'extended_address'})
    async def scenario():
        async with client(backend) as api:
            await api.create_document(resource('document'), recipient_address='contact')
    asyncio.run(scenario())
    payload = json.loads(next(r for r in backend.requests if r.method == 'POST' and '/document/' in r.url.path).content)
    assert payload['recipient_extended_address'] == ''


@pytest.mark.req('SP-MLC-058')
@pytest.mark.parametrize('value,source', [({}, 'contact'), ({}, 'unknown')])
def test_unusable_address_selection_fails_before_any_request(backend, value, source):
    """住所の選択が不正か担当者が無い時は、書類の作成を始めない。"""
    async def scenario():
        async with client(backend) as api:
            with pytest.raises(MakeLeapsError):
                await api.create_document(value, recipient_address=source)
    asyncio.run(scenario())
    assert backend.requests == []


@pytest.mark.req('SP-MLC-058')
@pytest.mark.parametrize('addresses', [[], None])
def test_contact_without_address_cannot_silently_reuse_the_old_address(backend, addresses):
    """選んだ連絡先に住所が無い時は、古い住所で作成せず理由を返す。"""
    backend.override = lambda r: envelope({**resource('contact'), 'addresses': addresses}) if '/contact/' in r.url.path else None
    async def scenario():
        async with client(backend) as api:
            with pytest.raises(MakeLeapsError, match='address'):
                await api.create_document(resource('document'), recipient_address='contact')
    asyncio.run(scenario())
    assert not any(r.method == 'POST' and '/document/' in r.url.path for r in backend.requests)


@pytest.mark.req('SP-MLC-058')
def test_cli_create_copies_selected_contact_address_from_stdin(cli_transport, capsys, monkeypatch):
    """cli は宛先の住所の指定と標準入力の YAML を library に渡す。"""
    address_backend(cli_transport)
    monkeypatch.setattr('sys.stdin', io.StringIO(yaml.safe_dump(resource('document'))))
    assert main(['documents', 'create', '--file', '-', '--recipient-address', 'contact', '--language', 'en']) == 0
    output = capsys.readouterr()
    assert output.err == ''
    assert yaml.safe_load(output.out)['recipient_postal_code'] == ADDRESS['postal_code']
    assert all(r.headers['accept-language'] == 'en' for r in cli_transport.requests)


def organization_backend(backend, *, organization=True, addresses=None):
    contacts = [{**resource('contact'), 'addresses': [{**ADDRESS, 'postal_code': '999-9999'}]}]
    if organization:
        contacts.append({**resource('contact'), 'mid': 'example-organization',
                         'url': BASE + 'contact/example-organization/', 'contact_type': 'organization',
                         'addresses': [ADDRESS] if addresses is None else addresses})
    original = backend.override
    def override(request):
        if request.url.path.endswith('/client/example-client/contact/'):
            return envelope(contacts, count=len(contacts), next=None)
        if original:
            return original(request)
    backend.override = override


@pytest.mark.req('SP-MLC-058')
@pytest.mark.parametrize('input_form', ['urls', 'model'])
def test_client_address_comes_from_its_organization_contact_instead_of_the_person(backend, input_form):
    """取引先の住所は、担当者の住所ではなく組織の連絡先の住所を写す。"""
    address_backend(backend)
    organization_backend(backend)
    value = resource('document')
    if input_form == 'model':
        value.update(client=resource('client'), client_contact=resource('contact'))
        value = Document.model_validate(value)
    async def scenario():
        async with client(backend) as api:
            document = await api.create_document(value, recipient_address='client')
            assert document.recipient_postal_code == ADDRESS['postal_code']
    asyncio.run(scenario())
    payload = json.loads(next(r for r in backend.requests if r.method == 'POST' and '/document/' in r.url.path).content)
    assert {field: payload[field] for field in RECIPIENT} == RECIPIENT
    assert 'recipient_display' not in payload


@pytest.mark.req('SP-MLC-058')
@pytest.mark.parametrize('organization,addresses,reason', [(False, None, 'organization'), (True, [], 'address')])
def test_client_without_organization_or_address_fails_without_creating(backend, organization, addresses, reason):
    """組織の連絡先かその住所が無い時は、理由を返して書類を作らない。"""
    organization_backend(backend, organization=organization, addresses=addresses)
    async def scenario():
        async with client(backend) as api:
            with pytest.raises(MakeLeapsError, match=reason):
                await api.create_document(resource('document'), recipient_address='client')
    asyncio.run(scenario())
    assert not any(r.method == 'POST' and '/document/' in r.url.path for r in backend.requests)


@pytest.mark.req('SP-MLC-058')
def test_cli_create_uses_client_address_from_a_yaml_file(cli_transport, capsys, tmp_path):
    """cli は file の取引先を使って組織の住所を写す。"""
    address_backend(cli_transport)
    organization_backend(cli_transport)
    path = tmp_path / 'document.yaml'
    path.write_text(yaml.safe_dump(resource('document')))
    assert main(['documents', 'create', '--file', str(path), '--recipient-address', 'client']) == 0
    output = capsys.readouterr()
    assert output.err == ''
    assert yaml.safe_load(output.out)['recipient_postal_code'] == ADDRESS['postal_code']


@pytest.mark.req('SP-MLC-058')
def test_cli_reports_missing_selected_address_without_creating(cli_transport, capsys, tmp_path):
    """cli は選んだ住所が無い理由を標準エラーへ出し、書類を作らない。"""
    organization_backend(cli_transport, organization=False)
    path = tmp_path / 'document.yaml'
    path.write_text(yaml.safe_dump(resource('document')))
    assert main(['documents', 'create', '--file', str(path), '--recipient-address', 'client']) == 1
    output = capsys.readouterr()
    assert output.out == '' and 'organization' in output.err
    assert not any(r.method == 'POST' and '/document/' in r.url.path for r in cli_transport.requests)
