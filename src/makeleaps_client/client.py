"""Async MakeLeaps document, client, and contact operations."""
from __future__ import annotations

import asyncio
import base64
from collections import deque
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import date, datetime, time as day_time, timedelta, timezone
from email.utils import parsedate_to_datetime
import math
import re
import time
from typing import Any, TypeVar
from urllib.parse import quote, urljoin

import httpx
from pydantic import TypeAdapter, ValidationError

from .models import Contact, Client, Document, DocumentArchive, MarkSentResult, Partner, Resource, SentStatus

_ORIGIN = 'https://api.makeleaps.com'
_TOKEN_URL = _ORIGIN + '/user/oauth2/token/'
_PAGE_SIZE = 100
_PAGE_CONCURRENCY = 8
_ATTEMPTS = 4
_JAPAN = timezone(timedelta(hours=9))
_EXTERNAL_DELIVERY_CATEGORIES = frozenset({
    'electronically_outside_makeleaps', 'physically_outside_makeleaps',
})
_UNCONFIRMED_CREATE = (
    'The MakeLeaps API did not confirm the result; the {resource} may have been created. '
    'Check MakeLeaps before retrying.'
)
_CLIENT_WRITE_EXCLUDED = frozenset({
    'mid', 'url', 'display_name', 'organization_type', 'is_organization',
    'date_archived', 'managing_teams', 'default_presets', 'integrations',
    'settings', 'organization_contact', 'virtual_bank_accounts',
})
_CONTACT_WRITE_EXCLUDED = frozenset({
    'mid', 'url', 'owner', 'owner_type', 'display_name', 'format',
    'is_default', 'default_address', 'integrations',
})
_DOCUMENT_WRITE_EXCLUDED = frozenset({
    'mid', 'url', 'display_name', 'pdf_content_url', 'pdf_upload_url',
    'pickup_links', 'modified', 'date_removed', 'date_converted',
    'is_accepted', 'is_confirmed', 'is_converted', 'approval_status', 'integrations',
})
_DOCUMENT_CALCULATED_FIELDS = frozenset({
    'subtotal', 'tax', 'total', 'mixed_tax_rate_totals', 'tax_group_totals',
    'subtotal_sales_tax', 'subtotal_withholding_tax', 'jp_withholding_tax',
})
_DOCUMENT_STATUS_DATES = frozenset({
    'date_paid', 'date_sent', 'date_cancelled', 'date_accepted',
    'date_confirmed', 'date_shipped', 'payment_date',
})
_DOCUMENT_DEADLINES = {'invoice': 'date_due', 'quote': 'date_valid', 'order': 'date_valid'}
_RECIPIENT_ADDRESS_FIELDS = {
    'format': 'recipient_address_format', 'country_name': 'recipient_country',
    'postal_code': 'recipient_postal_code', 'region': 'recipient_region',
    'locality': 'recipient_locality', 'street_address': 'recipient_street_address',
    'extended_address': 'recipient_extended_address',
}
T = TypeVar('T', bound=Resource)
Filters = Mapping[str, Any] | Sequence[tuple[str, Any]]


class MakeLeapsError(Exception):
    """A request or response failure with safe API validation details."""


def _safe_url(value: str) -> httpx.URL:
    try:
        # Only API-relative links can be resolved against the fixed TLS origin.
        url = httpx.URL(urljoin(_ORIGIN + '/', value))
        if (url.scheme != 'https' or url.host != 'api.makeleaps.com'
                or url.port not in (None, 443) or url.userinfo or url.fragment):
            raise ValueError
        return url
    except (ValueError, TypeError, httpx.InvalidURL):
        raise MakeLeapsError('The API returned an unsupported resource URL.') from None


async def _together(*coroutines):
    tasks = [asyncio.create_task(coro) for coro in coroutines]
    try:
        return await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class _Limiter:
    def __init__(self):
        self._recent = deque()
        self._daily = deque()
        self._lock = asyncio.Lock()
        self._blocked_until = 0.0

    def defer(self, seconds: float) -> None:
        self._blocked_until = max(self._blocked_until, time.monotonic() + seconds)

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._recent and self._recent[0] <= now - 60:
                    self._recent.popleft()
                while self._daily and self._daily[0] <= now - 86400:
                    self._daily.popleft()
                delay = max(0.0, self._blocked_until - now)
                if len(self._recent) >= 250:
                    delay = max(delay, self._recent[0] + 60 - now)
                if len(self._daily) >= 20000:
                    delay = max(delay, self._daily[0] + 86400 - now)
                if delay > 0:
                    await asyncio.sleep(delay)
                    continue
                self._recent.append(now)
                self._daily.append(now)
                return


def _retry_after(response: httpx.Response, attempt: int) -> float:
    value = response.headers.get('Retry-After', '')
    try:
        delay = float(value)
        if math.isfinite(delay) and delay >= 0:
            return delay
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            pass
    return float(2 ** attempt)


def _validation_details(value: Any, path: str = '') -> list[str] | None:
    if isinstance(value, dict):
        children = value.items()
    elif isinstance(value, list):
        if all(isinstance(item, str) for item in value):
            return [f'{path}: {"; ".join(value)}'] if value else []
        children = enumerate(value)
    else:
        return None
    details = []
    for field, messages in children:
        nested = _validation_details(messages, f'{path}.{field}' if path else str(field))
        if nested is None:
            return None
        details.extend(nested)
    return details


def _document_date(value: date | str, label: str = 'issue date') -> date:
    try:
        if isinstance(value, str):
            value = date.fromisoformat(value)
        if not isinstance(value, date) or isinstance(value, datetime):
            raise ValueError
        return value
    except ValueError:
        raise MakeLeapsError(f'The {label} must be an ISO date (YYYY-MM-DD).') from None


class MakeLeapsClient:
    """Use credentials from arguments only; close with ``async with`` or ``aclose``.

    ``transport`` supports HTTP boundary substitution, e.g. httpx.MockTransport.
    A client belongs to one event loop. Its token and request limiter are shared
    by all operations on that client.
    """

    def __init__(self, client_id: str, client_secret: str, *, language: str = 'ja',
                 transport: httpx.AsyncBaseTransport | None = None):
        if not client_id or not client_secret:
            raise MakeLeapsError('Both client_id and client_secret are required.')
        self._credentials = httpx.BasicAuth(client_id, client_secret)
        self._sensitive_values = {
            client_id, client_secret,
            base64.b64encode(f'{client_id}:{client_secret}'.encode()).decode(),
        }
        self._http = httpx.AsyncClient(
            headers={'Accept-Language': language, 'Accept': 'application/json'},
            transport=transport, follow_redirects=False, timeout=60.0,
            trust_env=False,
        )
        self._token = None
        self._expires_at = 0.0
        self._token_lock = asyncio.Lock()
        self._partner_url = None
        self._partner_lock = asyncio.Lock()
        self._limiter = _Limiter()

    async def __aenter__(self) -> MakeLeapsClient:
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()
        self._token = None
        self._expires_at = 0.0
        self._sensitive_values.clear()

    async def _send(self, method: str, url: str | httpx.URL, **kwargs) -> httpx.Response:
        url = _safe_url(str(url))
        creating = method == 'POST' and str(url) != _TOKEN_URL
        resource = url.path.rstrip('/').rsplit('/', 1)[-1]
        if url.path.endswith('/archive/create/'):
            resource = 'document archive'
        unconfirmed = _UNCONFIRMED_CREATE.format(resource=resource)
        for attempt in range(_ATTEMPTS):
            await self._limiter.acquire()
            try:
                response = await self._http.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                if creating and not isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
                    raise MakeLeapsError(unconfirmed) from None
                if attempt + 1 == _ATTEMPTS:
                    raise MakeLeapsError('Unable to communicate with the MakeLeaps API.') from None
                await asyncio.sleep(2 ** attempt)
                continue
            if response.status_code == 429:
                if attempt + 1 == _ATTEMPTS:
                    raise MakeLeapsError('The MakeLeaps API rate limit persisted after retries.')
                self._limiter.defer(_retry_after(response, attempt))
                continue
            if response.status_code >= 500:
                if creating:
                    raise MakeLeapsError(f'MakeLeaps API request failed (HTTP {response.status_code}). {unconfirmed}')
                if attempt + 1 < _ATTEMPTS:
                    await asyncio.sleep(2 ** attempt)
                    continue
            return response
        raise MakeLeapsError('Unable to complete the MakeLeaps API request.')

    def _redact(self, message: str) -> str:
        for secret in sorted(self._sensitive_values, key=len, reverse=True):
            message = message.replace(secret, '[redacted]')
        return message

    def _check(self, response: httpx.Response) -> None:
        if not 200 <= response.status_code < 300:
            message = f'MakeLeaps API request failed (HTTP {response.status_code}).'
            if 400 <= response.status_code < 500:
                try:
                    data = response.json()
                except (ValueError, UnicodeError):
                    data = None
                errors = data.get('response') if isinstance(data, dict) else None
                details = _validation_details(errors) if isinstance(errors, dict) else None
                if details:
                    message += ' ' + '; '.join(details)
            raise MakeLeapsError(self._redact(message))

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError
            return data
        except (ValueError, UnicodeError):
            raise MakeLeapsError('The MakeLeaps API returned invalid JSON.') from None

    async def _access_token(self, rejected: str | None = None) -> str:
        async with self._token_lock:
            if self._token and time.monotonic() < self._expires_at and self._token != rejected:
                return self._token
            response = await self._send('POST', _TOKEN_URL, auth=self._credentials,
                                        data={'grant_type': 'client_credentials'})
            self._check(response)
            data = self._json(response)
            try:
                token = data['access_token']
                expires = float(data['expires_in'])
                if not isinstance(token, str) or not token or not math.isfinite(expires) or expires <= 0:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                raise MakeLeapsError('The MakeLeaps API returned an invalid access token response.') from None
            self._token = token
            self._sensitive_values.add(token)
            self._expires_at = time.monotonic() + expires
            return token

    async def _request(self, url: str | httpx.URL, *, params=None,
                       method: str = 'GET', json=None) -> httpx.Response:
        url = _safe_url(str(url))
        token = await self._access_token()
        response = await self._send(method, url, params=params, json=json,
                                    headers={'Authorization': f'Bearer {token}'})
        if response.status_code == 401:
            token = await self._access_token(rejected=token)
            response = await self._send(method, url, params=params, json=json,
                                        headers={'Authorization': f'Bearer {token}'})
        self._check(response)
        return response

    async def _data(self, url: str | httpx.URL, *, params=None,
                    method: str = 'GET', json=None) -> dict[str, Any]:
        data = self._json(await self._request(url, params=params, method=method, json=json))
        if 'response' not in data:
            raise MakeLeapsError('The MakeLeaps API response is missing its resource data.')
        return data

    @staticmethod
    def _model(model: type[T], data: Any) -> T:
        try:
            return model.model_validate(data)
        except ValidationError:
            raise MakeLeapsError('The MakeLeaps API returned an invalid resource.') from None

    async def _company(self) -> str:
        async with self._partner_lock:
            if self._partner_url is None:
                data = (await self._data(_ORIGIN + '/api/'))['response']
                try:
                    partners = data['partners']
                    if len(partners) != 1:
                        raise ValueError
                    self._partner_url = str(_safe_url(partners[0]['url']))
                except (KeyError, TypeError, ValueError):
                    raise MakeLeapsError('The API key must identify exactly one company.') from None
            return self._partner_url

    async def get_partner(self) -> Partner:
        """Return the company associated with the API key."""
        return self._model(Partner, (await self._data(await self._company()))['response'])

    async def list_clients(self, *, filters: Filters | None = None) -> AsyncIterator[Client]:
        """Yield all matching clients in API order; filters use API query names."""
        async for page in self._pages((await self._company()) + 'client/', filters):
            for value in page:
                yield self._model(Client, value)

    async def get_client(self, mid: str) -> Client:
        """Return the client identified by its mid."""
        return self._model(Client, await self._resource('client', mid))

    async def list_contacts(self, *, client_mid: str | None = None,
                            filters: Filters | None = None) -> AsyncIterator[Contact]:
        """Yield all matching contacts, optionally limited to one client."""
        url = ((await self._resource_url('client', client_mid)) + 'contact/'
               if client_mid is not None else (await self._company()) + 'contact/')
        async for page in self._pages(url, filters):
            for value in page:
                yield self._model(Contact, value)

    async def get_contact(self, mid: str) -> Contact:
        """Return the contact identified by its mid."""
        return self._model(Contact, await self._resource('contact', mid))

    @staticmethod
    def _write_payload(data: Mapping[str, Any] | Resource, excluded: frozenset[str]) -> dict[str, Any]:
        if isinstance(data, Resource):
            data = data.model_dump()
        value = {key: val for key, val in data.items() if key not in excluded}
        return TypeAdapter(Any).dump_python(value, mode='json')

    @staticmethod
    def _contact_urls(contacts: Any) -> None:
        if not isinstance(contacts, list):
            raise MakeLeapsError('The contacts field must be a list of contact URLs when updating a client.')
        for value in contacts:
            if not isinstance(value, str):
                raise MakeLeapsError('The contacts field must contain only contact URLs when updating a client.')
            try:
                url = _safe_url(value)
                if (not value.startswith(_ORIGIN + '/') or url.query
                        or not re.fullmatch(r'/api/partner/[^/]+/contact/[^/]+/', url.path)):
                    raise ValueError
            except (MakeLeapsError, ValueError):
                raise MakeLeapsError('The contacts field must contain only contact URLs when updating a client.') from None

    @classmethod
    def _client_payload(cls, data: Mapping[str, Any] | Client, *, updating: bool = False) -> dict[str, Any]:
        value = cls._write_payload(data, _CLIENT_WRITE_EXCLUDED)
        if updating and 'contacts' in value:
            cls._contact_urls(value['contacts'])
        elif isinstance(value.get('contacts'), list):
            value['contacts'] = [cls._write_payload(item, _CONTACT_WRITE_EXCLUDED)
                                 if isinstance(item, Mapping) else item for item in value['contacts']]
        if isinstance(value.get('default_contact'), Mapping):
            value['default_contact'] = value['default_contact'].get('url')
        return value

    async def create_client(self, data: Mapping[str, Any] | Client) -> Client:
        """Create a client from contact URLs or inline contact data."""
        payload = self._client_payload(data)
        value = (await self._data((await self._company()) + 'client/',
                                  method='POST', json=payload))['response']
        return self._model(Client, value)

    async def update_client(self, mid: str, data: Mapping[str, Any] | Client) -> Client:
        """Patch supplied client fields; contacts must contain only URLs.

        Include every contact URL to keep. The API deletes omitted contacts.
        """
        payload = self._client_payload(data, updating=True)
        value = (await self._data(await self._resource_url('client', mid),
                                  method='PATCH', json=payload))['response']
        return self._model(Client, value)

    async def create_contact(self, client_mid: str, data: Mapping[str, Any] | Contact) -> Contact:
        """Create a contact for the required client, keeping its existing contacts.

        If adding fails, the error identifies the created contact; it is not deleted.
        """
        current = await self.get_client(client_mid)
        self._contact_urls(current.contacts)
        payload = self._write_payload(data, _CONTACT_WRITE_EXCLUDED)
        value = (await self._data((await self._company()) + 'contact/',
                                  method='POST', json=payload))['response']
        contact = self._model(Contact, value)
        try:
            self._contact_urls([contact.url])
            await self.update_client(client_mid, {'contacts': [*current.contacts, contact.url]})
        except MakeLeapsError as exc:
            message = f'The contact (mid: {contact.mid}) was created, but could not be added to the client. {exc}'
            raise MakeLeapsError(self._redact(message)) from None
        return contact

    async def update_contact(self, mid: str, data: Mapping[str, Any] | Contact) -> Contact:
        """Patch only supplied writable contact fields."""
        payload = self._write_payload(data, _CONTACT_WRITE_EXCLUDED)
        value = (await self._data(await self._resource_url('contact', mid),
                                  method='PATCH', json=payload))['response']
        return self._model(Contact, value)

    async def delete_contact(self, mid: str) -> None:
        """Delete the contact identified by its mid, if the API permits it."""
        await self._request(await self._resource_url('contact', mid), method='DELETE')

    async def _resource_url(self, kind: str, mid: str) -> str:
        if not mid:
            raise MakeLeapsError('A resource mid is required.')
        return (await self._company()) + kind + '/' + quote(str(mid), safe='') + '/'

    async def _resource(self, kind: str, mid: str) -> dict[str, Any]:
        value = (await self._data(await self._resource_url(kind, mid)))['response']
        if not isinstance(value, dict):
            raise MakeLeapsError('The MakeLeaps API returned an invalid resource.')
        return value

    async def _pages(self, url: str, params=None) -> AsyncIterator[list[dict[str, Any]]]:
        query = httpx.QueryParams({'page_size': _PAGE_SIZE, 'page': 1})
        if params:
            query = query.merge(params)
        first = await self._data(url, params=query)
        values, meta = self._page(first)
        yield values
        if not meta.get('next'):
            return
        _safe_url(meta['next'])
        count, page = meta.get('count'), meta.get('page', 1)
        if isinstance(count, int) and isinstance(page, int) and values:
            last_page = math.ceil(count / len(values))
            for start in range(page + 1, last_page + 1, _PAGE_CONCURRENCY):
                pages = await _together(*(self._data(url, params=query.set('page', number))
                                         for number in range(start, min(start + _PAGE_CONCURRENCY, last_page + 1))))
                for data in pages:
                    batch, _ = self._page(data)
                    yield batch
        else:
            # Older responses without count still have a usable next link.
            seen = {str(_safe_url(url).copy_merge_params(query))}
            while meta.get('next'):
                next_url = str(_safe_url(meta['next']))
                if next_url in seen:
                    raise MakeLeapsError('The MakeLeaps API returned a repeated page.')
                seen.add(next_url)
                values, meta = self._page(await self._data(next_url))
                yield values

    @staticmethod
    def _page(data):
        values, meta = data['response'], data.get('meta', {})
        if not isinstance(values, list) or not all(isinstance(v, dict) for v in values) or not isinstance(meta, dict):
            raise MakeLeapsError('The MakeLeaps API returned an invalid list page.')
        return values, meta

    async def _related(self) -> tuple[dict[str, Any], dict[str, Any]]:
        company = await self._company()
        async def collect(kind):
            records = {}
            async for page in self._pages(company + kind + '/'):
                for item in page:
                    if isinstance(item.get('url'), str):
                        records[item['url']] = item
            return records
        clients, contacts = await _together(collect('client'), collect('contact'))
        return clients, contacts

    def _expand(self, value, related) -> Document:
        value = dict(value)
        for field, records in zip(('client', 'client_contact'), related):
            reference = value.get(field)
            if isinstance(reference, str):
                if reference not in records:
                    raise MakeLeapsError('A document references an unavailable client or contact.')
                value[field] = records[reference]
        return self._model(Document, value)

    async def _expand_references(self, value: dict[str, Any]) -> Document:
        if not isinstance(value, dict):
            raise MakeLeapsError('The MakeLeaps API returned an invalid resource.')
        async def collect(field):
            reference = value.get(field)
            if not isinstance(reference, str):
                return {}
            resource = (await self._data(reference))['response']
            if not isinstance(resource, dict):
                raise MakeLeapsError('The MakeLeaps API returned an invalid client or contact.')
            return {reference: resource}
        related = await _together(collect('client'), collect('client_contact'))
        return self._expand(value, related)

    async def get_document(self, mid: str) -> Document:
        """Return a document with its complete client and contact resources."""
        return await self._expand_references(await self._resource('document', mid))

    @staticmethod
    def _document_payload(data: Mapping[str, Any] | Document, *, creating: bool = False) -> dict[str, Any]:
        if isinstance(data, Document):
            data = data.model_dump()
        value = {key: val for key, val in data.items() if key not in _DOCUMENT_WRITE_EXCLUDED}
        if 'tax_rounding' in value:
            value.pop('round_tax_down', None)
        for field in ('client', 'client_contact'):
            if isinstance(value.get(field), Mapping):
                value[field] = value[field].get('url')
        # YAML can contain date/datetime values; serialize without validating fields.
        value = TypeAdapter(Any).dump_python(value, mode='json')
        if creating or 'lineitems' in value:
            value['autocalculate'] = True
            for field in _DOCUMENT_CALCULATED_FIELDS:
                value.pop(field, None)
            if isinstance(value.get('lineitems'), list):
                value['lineitems'] = [
                    {key: val for key, val in item.items() if key != 'price'}
                    if isinstance(item, Mapping) and item.get('kind') in ('normal', 'subtotal') else item
                    for item in value['lineitems']
                ]
        return value

    async def _recipient_address(self, payload: dict[str, Any], source: str) -> None:
        if source not in ('client', 'contact'):
            raise MakeLeapsError('Recipient address must be client or contact.')
        field = 'client' if source == 'client' else 'client_contact'
        reference = payload.get(field)
        if not isinstance(reference, str) or not reference:
            raise MakeLeapsError(f'The {field} URL is required to select its recipient address.')
        if source == 'contact':
            contact = (await self._data(reference))['response']
        else:
            contact = None
            pages = self._pages(reference.rstrip('/') + '/contact/')
            try:
                async for page in pages:
                    contact = next((item for item in page if item.get('contact_type') == 'organization'), None)
                    if contact is not None:
                        break
            finally:
                await pages.aclose()
            if contact is None:
                raise MakeLeapsError('The client has no organization contact for its recipient address.')
        addresses = contact.get('addresses') if isinstance(contact, dict) else None
        if not isinstance(addresses, list) or not addresses or not isinstance(addresses[0], dict):
            raise MakeLeapsError('The selected contact has no address to copy to the document.')
        payload.update({target: addresses[0].get(field, '') for field, target in _RECIPIENT_ADDRESS_FIELDS.items()})

    async def create_document(self, data: Mapping[str, Any] | Document, *,
                               recipient_address: str | None = None) -> Document:
        """Create a document and return its complete client and contact resources.

        ``recipient_address`` copies the client's organization address (client)
        or the document's client_contact address (contact). Omitting it preserves
        the input recipient fields. A missing selected address fails before creation.
        A duplicate type/number fails before creation and reports the existing MID.
        """
        payload = self._document_payload(data, creating=True)
        if recipient_address is not None:
            await self._recipient_address(payload, recipient_address)
        await self._check_document_number(payload)
        value = (await self._data((await self._company()) + 'document/',
                                  method='POST', json=payload))['response']
        try:
            return await self._expand_references(value)
        except MakeLeapsError as exc:
            if not isinstance(value, dict):
                raise
            mid = value.get('mid')
            identifier = f' (mid: {mid})' if isinstance(mid, str) and mid else ''
            message = f'The document{identifier} was created, but its expanded details could not be returned. {exc}'
            raise MakeLeapsError(self._redact(message)) from None

    async def _check_document_number(self, payload: Mapping[str, Any], *, ignore_mid: str | None = None) -> None:
        kind, number = payload.get('document_type'), payload.get('document_number')
        # Leave missing or invalid input fields to the API's validation.
        if not isinstance(kind, str) or not kind or not isinstance(number, str) or not number:
            return
        pages = self._pages((await self._company()) + 'document/',
                            {'document_type': kind, 'document_number': number})
        try:
            async for page in pages:
                for value in page:
                    if (value.get('document_type') == kind and value.get('document_number') == number
                            and (ignore_mid is None or value.get('mid') != ignore_mid)):
                        raise MakeLeapsError(self._redact(
                            f'A {kind} document with number {number} already exists (mid: {value.get("mid")}).'
                        ))
        finally:
            await pages.aclose()

    async def _templates(self, document_type: str) -> list[dict[str, Any]]:
        data = await self._data(await self._resource_url('template', document_type),
                                params={'hide_archived': 'y', 'is_archived': 'false'})
        values, _ = self._page(data)
        return values

    async def _conversion_template(self, source: dict[str, Any], document_type: str) -> str:
        code = source.get('document_template')
        if not isinstance(code, str) or not code:
            raise MakeLeapsError('Specify a template; the source document has no template code.')
        if source.get('document_type') == document_type:
            return code
        targets = await self._templates(document_type)
        if any(item.get('template_code') == code for item in targets):
            return code
        source_type = source.get('document_type')
        if not isinstance(source_type, str) or not source_type:
            raise MakeLeapsError('Specify a template; the source document has no document type.')
        sources = await self._templates(source_type)
        original = next((item for item in sources if item.get('template_code') == code), {})
        language = original.get('language_code')
        if not isinstance(language, str) or not language:
            raise MakeLeapsError('Specify a template; the source template language is unavailable.')
        candidates = [item for item in targets if item.get('language_code') == language
                      and isinstance(item.get('template_code'), str) and item['template_code']]
        if not candidates:
            raise MakeLeapsError('Specify a template; no target template has the source template language.')
        selected = next((item for item in candidates if item.get('use_advanced_tax_settings')
                         == original.get('use_advanced_tax_settings')), candidates[0])
        return selected['template_code']

    async def convert_document(self, mid: str, document_type: str, document_number: str, *,
                                template: str | None = None, date: date | str | None = None,
                                deadline: date | str | None = None,
                                deadline_days: int | None = None) -> Document:
        """Copy a document to a new number and any type, including its original type.

        An omitted template preserves the source language, preferring its code
        and then its tax settings. The issue date defaults to today in Japan;
        supply an ISO date string or datetime.date to override it. Status dates
        are not copied. Choose either an explicit deadline or days from the new
        issue date. Otherwise, preserve the original interval only when both
        types use the same deadline field. A same-type copy keeps its template.
        """
        issued = _document_date(date) if date is not None else datetime.now(_JAPAN).date()
        if deadline is not None and deadline_days is not None:
            raise MakeLeapsError('Specify either deadline or deadline_days, not both.')
        field = _DOCUMENT_DEADLINES.get(document_type)
        if (deadline is not None or deadline_days is not None) and field is None:
            raise MakeLeapsError('This document type does not support a deadline.')
        due = _document_date(deadline, 'deadline') if deadline is not None else None
        if deadline_days is not None:
            if type(deadline_days) is not int:
                raise MakeLeapsError('Deadline days must be an integer.')
            try:
                due = issued + timedelta(days=deadline_days)
            except OverflowError:
                raise MakeLeapsError('Deadline days are outside the supported date range.') from None
        source = await self._resource('document', mid)
        if (due is None and field is not None and source.get(field)
                and _DOCUMENT_DEADLINES.get(source.get('document_type')) == field):
            interval = (_document_date(source[field], 'source deadline')
                        - _document_date(source.get('date'), 'source issue date'))
            try:
                due = issued + interval
            except OverflowError:
                raise MakeLeapsError('The calculated deadline is outside the supported date range.') from None
        if template is None:
            template = await self._conversion_template(source, document_type)
        payload = {key: value for key, value in source.items() if key not in _DOCUMENT_STATUS_DATES}
        for key in ('date_due', 'date_valid'):
            payload.pop(key, None)
        if due is not None:
            payload[field] = due.isoformat()
        payload.update(document_type=document_type, document_number=document_number,
                       document_template=template, date=issued.isoformat())
        return await self.create_document(payload)

    async def update_document(self, mid: str, data: Mapping[str, Any] | Document) -> Document:
        """Patch supplied writable fields and return the expanded document.

        Number changes check for another document with the same type and number;
        conflicts report the other MID without sending the update.
        """
        payload = self._document_payload(data)
        if 'document_number' in payload:
            current = await self._resource('document', mid)
            if payload['document_number'] != current.get('document_number'):
                await self._check_document_number({
                    'document_type': payload.get('document_type', current.get('document_type')),
                    'document_number': payload['document_number'],
                }, ignore_mid=mid)
        value = (await self._data(await self._resource_url('document', mid),
                                  method='PATCH', json=payload))['response']
        return await self._expand_references(value)

    async def delete_document(self, mid: str) -> None:
        """Delete the document identified by its mid."""
        await self._request(await self._resource_url('document', mid), method='DELETE')

    async def mark_document_sent(self, mid: str, date_sent: date | datetime | str,
                                 method: str) -> MarkSentResult:
        """Save a sent date and archive electronic or physical delivery outside MakeLeaps.

        Date-only and timezone-naive inputs use Japan time. Order slips and
        acceptance certificates receive the date without an archive.
        """
        methods = {'electronic': 'manually_sent_electronically', 'physical': 'manually_sent_physically'}
        if method not in methods:
            raise MakeLeapsError('Sending method must be electronic or physical.')
        try:
            if isinstance(date_sent, str):
                date_sent = datetime.fromisoformat(date_sent)
            elif isinstance(date_sent, date) and not isinstance(date_sent, datetime):
                date_sent = datetime.combine(date_sent, day_time())
            if not isinstance(date_sent, datetime):
                raise ValueError
            if date_sent.tzinfo is None:
                date_sent = date_sent.replace(tzinfo=_JAPAN)
            timestamp = date_sent.isoformat()
        except ValueError:
            raise MakeLeapsError('The sent date must be an ISO date or datetime.') from None
        value = (await self._data(await self._resource_url('document', mid),
                                  method='PATCH', json={'date_sent': timestamp}))['response']
        try:
            document = self._model(Document, value)
            if document.date_sent is None or not document.document_type:
                raise MakeLeapsError('The API response is missing the sent date or document type.')
        except MakeLeapsError as exc:
            raise MakeLeapsError(self._redact(
                f'The document (mid: {mid}) was updated, but its sent date could not be confirmed. {exc}'
            )) from None
        archive = None
        if document.document_type not in ('orderslip', 'acceptance'):
            try:
                value = (await self._data((await self._company()) + 'archive/create/', method='POST', json={
                    'document_mid': mid, 'sending_method': methods[method],
                }))['response']
                archive = self._model(DocumentArchive, value)
                if not archive.mid:
                    raise MakeLeapsError('The API did not identify the created document archive. Check MakeLeaps before retrying.')
            except MakeLeapsError as exc:
                raise MakeLeapsError(self._redact(
                    f'The document (mid: {mid}) has date_sent set to {document.date_sent.isoformat()}, '
                    f'but its document archive could not be created or confirmed. {exc}'
                )) from None
        return MarkSentResult(mid=mid, date_sent=document.date_sent,
                              archive_created=archive is not None, archive=archive)

    async def list_sent_status(self, *, document_type: str | None = None,
                               date_from: date | str | None = None,
                               date_to: date | str | None = None,
                               client_name: str | None = None,
                               project_name: str | None = None,
                               filters: Filters | None = None) -> AsyncIterator[SentStatus]:
        """Yield sent dates and archive matches by document type and number.

        Only legal categories for delivery outside MakeLeaps count as a match.
        Documents sharing a type and number cannot be distinguished by archives.
        """
        documents = self.list_documents(document_type=document_type, date_from=date_from,
                                        date_to=date_to, client_name=client_name,
                                        project_name=project_name, filters=filters)
        external = None
        try:
            async for document in documents:
                if external is None:
                    external = set()
                    async for archive in self.list_archives():
                        if (archive.document_type and archive.document_number
                                and any(category in _EXTERNAL_DELIVERY_CATEGORIES
                                        for category in archive.legal_categories or [])):
                            external.add((archive.document_type, archive.document_number))
                yield SentStatus(mid=document.mid, document_type=document.document_type,
                                 document_number=document.document_number, date_sent=document.date_sent,
                                 has_external_archive=(document.document_type, document.document_number) in external)
        finally:
            await documents.aclose()

    async def _document_queries(self, params: httpx.QueryParams,
                                client_name: str | None) -> AsyncIterator[httpx.QueryParams]:
        if client_name is None:
            yield params
            return
        clients = self.list_clients(filters={'search': client_name})
        selected = params.get_list('client')
        try:
            async for client in clients:
                if (client.mid and client_name in (client.display_name or '')
                        and (not selected or client.mid in selected)):
                    yield params.set('client', client.mid)
        finally:
            await clients.aclose()

    async def list_documents(self, *, document_type: str | None = None,
                             date_from: date | str | None = None,
                             date_to: date | str | None = None,
                             client_name: str | None = None,
                             project_name: str | None = None,
                             filters: Filters | None = None) -> AsyncIterator[Document]:
        """Yield matching documents, fetching pages and matching clients on demand.

        Date bounds are inclusive. ``filters`` accepts a mapping or pairs, so
        repeated API parameters such as document_number retain all values.
        Name substrings are case-sensitive and intersect all other filters.
        Client-name searches group documents by matching clients in API order.
        """
        params = httpx.QueryParams(filters or {})
        for key, value in (('document_type', document_type), ('date__gte', date_from), ('date__lte', date_to)):
            if value is not None:
                params = params.set(key, value.isoformat() if isinstance(value, date) else value)
        url = (await self._company()) + 'document/'
        queries = self._document_queries(params, client_name)
        first = True
        related = None
        try:
            async for query in queries:
                pages = self._pages(url, query)
                try:
                    async for page in pages:
                        for value in page:
                            if project_name is not None:
                                project = value.get('project_name')
                                if not isinstance(project, str) or project_name not in project:
                                    continue
                            if first:
                                # Return promptly even when related lists are large.
                                yield await self._expand_references(value)
                                first = False
                            else:
                                # Bulk expansion avoids two requests per document.
                                if related is None:
                                    related = await self._related()
                                yield self._expand(value, related)
                finally:
                    await pages.aclose()
        finally:
            await queries.aclose()

    async def list_archives(self) -> AsyncIterator[DocumentArchive]:
        """Yield every Document Archive in API order."""
        async for page in self._pages((await self._company()) + 'archive/'):
            for value in page:
                yield self._model(DocumentArchive, value)

    async def _pdf(self, kind: str, mid: str) -> bytes:
        value = await self._resource(kind, mid)
        url = value.get('pdf_content_url')
        if not isinstance(url, str) or not url:
            raise MakeLeapsError('No PDF is available for this resource.')
        return (await self._request(url)).content

    async def get_document_pdf(self, mid: str) -> bytes:
        """Return current document PDF bytes, without modifying the document."""
        return await self._pdf('document', mid)

    async def get_archive_pdf(self, mid: str) -> bytes:
        """Return the PDF bytes saved at the time of sending."""
        return await self._pdf('archive', mid)
