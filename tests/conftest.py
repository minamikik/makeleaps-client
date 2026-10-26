"""Synthetic API responses; no MakeLeaps account data or network access."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path

import httpx
import pytest

BASE = 'https://api.makeleaps.com/api/partner/example-company/'
PDF = b'%PDF-1.7\n\x00\xffsynthetic\n%%EOF'


def resource(name):
    return json.loads((Path(__file__).parent / 'fixtures' / f'{name}.json').read_text())


def envelope(data, **meta):
    return httpx.Response(200, json={'meta': {'status_code': 200, **meta}, 'response': data})


class Backend:
    def __init__(self):
        self.requests = []
        self.tokens = 0
        self.document_count = 3
        self.archive_count = 3
        self.document_number = 'EXAMPLE-1'
        self.override = None
        self.active_pages = 0
        self.max_active_pages = 0
        self.page_gate = None

    async def __call__(self, request):
        self.requests.append(request)
        if self.override:
            reply = self.override(request)
            if reply is not None:
                return reply
        path = request.url.path
        if path == '/user/oauth2/token/':
            self.tokens += 1
            return httpx.Response(200, json={'access_token': f'example-token-{self.tokens}', 'expires_in': 36000, 'token_type': 'Bearer', 'scope': 'read write'})
        assert request.headers['Authorization'].startswith('Bearer example-token-')
        if path == '/api/':
            return envelope({'partners': [{'url': BASE, 'name': 'Example Company'}], 'currencies': 'https://api.makeleaps.com/api/currency/'})
        if str(request.url) == BASE:
            return envelope(resource('partner'))
        if path.endswith('/pdf/'):
            return httpx.Response(200, content=PDF, headers={'content-type': 'application/pdf'})
        name, *tail = path.removeprefix('/api/partner/example-company/').strip('/').split('/')
        if tail:
            value = resource(name)
            value.update(mid=tail[0], url=f'{BASE}{name}/{tail[0]}/', pdf_content_url=f'{BASE}{name}/{tail[0]}/pdf/')
            return envelope(value)
        count = self.document_count if name == 'document' else self.archive_count if name == 'archive' else 1
        page = int(request.url.params.get('page', '1'))
        size = min(int(request.url.params.get('page_size', '20')), 100)
        self.active_pages += 1
        self.max_active_pages = max(self.max_active_pages, self.active_pages)
        if self.page_gate is not None:
            if self.active_pages >= 3:
                self.page_gate.set()
            await self.page_gate.wait()
        else:
            await asyncio.sleep(0)
        self.active_pages -= 1
        values = []
        for i in range((page - 1) * size, min(page * size, count)):
            value = deepcopy(resource(name))
            if name in ('document', 'archive'):
                mid = f'example-{name}-{i}'
                value.update(mid=mid, url=f'{BASE}{name}/{mid}/', pdf_content_url=f'{BASE}{name}/{mid}/pdf/')
                value['document_number'] = self.document_number
                if name == 'document':
                    value['document_type'] = ['invoice', 'quote', 'receipt', 'order', 'deliveryslip', 'future-kind'][i % 6]
            values.append(value)
        next_url = str(request.url.copy_set_param('page', str(page + 1))) if page * size < count else None
        return envelope(values, count=count, page=page, next=next_url, prev=None)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    async def reject(*args, **kwargs):
        raise AssertionError('Tests must not access the network')
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', reject)


@pytest.fixture
def backend():
    return Backend()


@pytest.fixture
def cli_transport(monkeypatch, backend):
    original = httpx.AsyncClient.__init__
    def init(self, *args, **kwargs):
        kwargs['transport'] = httpx.MockTransport(backend)
        original(self, *args, **kwargs)
    monkeypatch.setattr(httpx.AsyncClient, '__init__', init)
    monkeypatch.setenv('MAKELEAPS_CLIENT_ID', 'example-id')
    monkeypatch.setenv('MAKELEAPS_CLIENT_SECRET', 'example-secret')
    return backend
