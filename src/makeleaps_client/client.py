"""Async, read-only MakeLeaps client with bounded retries and pagination."""
from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import date
from email.utils import parsedate_to_datetime
import math
import time
from typing import Any, TypeVar
from urllib.parse import quote, urljoin

import httpx
from pydantic import ValidationError

from .models import Contact, Client, Document, DocumentArchive, Partner, Resource

_ORIGIN = 'https://api.makeleaps.com'
_TOKEN_URL = _ORIGIN + '/user/oauth2/token/'
_PAGE_SIZE = 100
_PAGE_CONCURRENCY = 8
_ATTEMPTS = 4
T = TypeVar('T', bound=Resource)
Filters = Mapping[str, Any] | Sequence[tuple[str, Any]]


class MakeLeapsError(Exception):
    """A request or response failure, without credentials or raw server content."""


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

    async def _send(self, method: str, url: str | httpx.URL, **kwargs) -> httpx.Response:
        url = _safe_url(str(url))
        for attempt in range(_ATTEMPTS):
            await self._limiter.acquire()
            try:
                response = await self._http.request(method, url, **kwargs)
            except httpx.HTTPError:
                if attempt + 1 == _ATTEMPTS:
                    raise MakeLeapsError('Unable to communicate with the MakeLeaps API.') from None
                await asyncio.sleep(2 ** attempt)
                continue
            if response.status_code == 429:
                if attempt + 1 == _ATTEMPTS:
                    raise MakeLeapsError('The MakeLeaps API rate limit persisted after retries.')
                self._limiter.defer(_retry_after(response, attempt))
                continue
            if response.status_code >= 500 and attempt + 1 < _ATTEMPTS:
                await asyncio.sleep(2 ** attempt)
                continue
            return response
        raise MakeLeapsError('Unable to complete the MakeLeaps API request.')

    @staticmethod
    def _check(response: httpx.Response) -> None:
        if not 200 <= response.status_code < 300:
            raise MakeLeapsError(f'MakeLeaps API request failed (HTTP {response.status_code}).')

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
            self._expires_at = time.monotonic() + expires
            return token

    async def _request(self, url: str | httpx.URL, *, params=None) -> httpx.Response:
        url = _safe_url(str(url))
        token = await self._access_token()
        response = await self._send('GET', url, params=params,
                                    headers={'Authorization': f'Bearer {token}'})
        if response.status_code == 401:
            token = await self._access_token(rejected=token)
            response = await self._send('GET', url, params=params,
                                        headers={'Authorization': f'Bearer {token}'})
        self._check(response)
        return response

    async def _data(self, url: str | httpx.URL, *, params=None) -> dict[str, Any]:
        data = self._json(await self._request(url, params=params))
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

    async def _resource(self, kind: str, mid: str) -> dict[str, Any]:
        if not mid:
            raise MakeLeapsError('A resource mid is required.')
        url = (await self._company()) + kind + '/' + quote(str(mid), safe='') + '/'
        value = (await self._data(url))['response']
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

    async def list_documents(self, *, document_type: str | None = None,
                             date_from: date | str | None = None,
                             date_to: date | str | None = None,
                             filters: Filters | None = None) -> AsyncIterator[Document]:
        """Yield all matching documents in API order, fetching pages on demand.

        Date bounds are inclusive. ``filters`` accepts a mapping or pairs, so
        repeated API parameters such as document_number retain all values.
        """
        params = httpx.QueryParams(filters or {})
        for key, value in (('document_type', document_type), ('date__gte', date_from), ('date__lte', date_to)):
            if value is not None:
                params = params.set(key, value.isoformat() if isinstance(value, date) else value)
        pages = self._pages((await self._company()) + 'document/', params)
        first = True
        related = None
        try:
            async for page in pages:
                for value in page:
                    if first:
                        # Return promptly even when the company's related lists are large.
                        yield await self._expand_references(value)
                        first = False
                    else:
                        # Bulk expansion avoids two requests per document in large exports.
                        if related is None:
                            related = await self._related()
                        yield self._expand(value, related)
        finally:
            await pages.aclose()

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
