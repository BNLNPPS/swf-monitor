"""Small synchronous client for the corun-ai REST API."""

import json
import logging
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from decouple import config

logger = logging.getLogger(__name__)


class CorunAPIError(RuntimeError):
    """Raised when corun-ai cannot service a request."""

    def __init__(self, message, *, status=None, payload=None):
        super().__init__(message)
        self.status = status
        self.payload = payload


def corun_base_url():
    return str(config('CORUN_BASE_URL', default='https://epic-devcloud.org/doc') or '').rstrip('/')


def corun_api_token():
    return str(config('CORUN_API_TOKEN', default='') or '').strip()


def corun_configured():
    return bool(corun_base_url() and corun_api_token())


class CorunClient:
    """Token-authenticated corun-ai API client."""

    def __init__(self, *, base_url=None, token=None, timeout=15):
        self.base_url = (base_url or corun_base_url()).rstrip('/')
        self.token = token if token is not None else corun_api_token()
        self.timeout = timeout

    def _request(self, method, path, *, payload=None, query=None):
        if not self.base_url:
            raise CorunAPIError('CORUN_BASE_URL is not configured')
        if not self.token:
            raise CorunAPIError('CORUN_API_TOKEN is not configured')

        url = f"{self.base_url}/api/v1/{path.lstrip('/')}"
        if query:
            clean_query = {
                key: value for key, value in query.items()
                if value not in (None, '', [])
            }
            if clean_query:
                url = f"{url}?{urlencode(clean_query, doseq=True)}"

        body = None
        headers = {
            'Authorization': f'Token {self.token}',
            'Accept': 'application/json',
            'User-Agent': 'swf-monitor-corun-client/1.0',
        }
        if payload is not None:
            body = json.dumps(payload).encode('utf-8')
            headers['Content-Type'] = 'application/json'

        req = Request(url, data=body, method=method.upper(), headers=headers)
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode('utf-8')
        except HTTPError as exc:
            raw = exc.read().decode('utf-8', errors='replace')
            parsed = _parse_json(raw)
            detail = parsed if parsed is not None else raw
            raise CorunAPIError(
                f'corun-ai API returned HTTP {exc.code}: {detail}',
                status=exc.code,
                payload=parsed,
            ) from exc
        except URLError as exc:
            raise CorunAPIError(f'corun-ai API request failed: {exc.reason}') from exc

        parsed = _parse_json(raw)
        if parsed is None:
            raise CorunAPIError('corun-ai API returned non-JSON response')
        return parsed

    def create_page(self, *, section, content, title='', status='published',
                    data=None, tags=None):
        return self._request('POST', 'pages/', payload={
            'section': section,
            'content': content,
            'title': title,
            'status': status,
            'data': data or {},
            'tags': tags or [],
        })

    def create_version(self, group_id, *, content, title='', status='draft',
                       data=None, tags=None):
        """New version of an existing page — corun-ai's correction path.

        corun inherits the prior version's data/title and merges supplied
        keys (explicit null removes); omit what you don't mean to change —
        an empty value sent explicitly would overwrite inherited metadata.
        """
        payload = {'content': content, 'status': status}
        if title:
            payload['title'] = title
        if data is not None:
            payload['data'] = data
        if tags is not None:
            payload['tags'] = tags
        return self._request('POST', f'pages/{group_id}/versions/', payload=payload)

    def list_versions(self, group_id):
        return self._request('GET', f'pages/{group_id}/versions/')

    def get_page(self, group_id):
        return self._request('GET', f'pages/{group_id}/')

    def list_pages(self, **filters):
        return self._request('GET', 'pages/', query=filters)

    def list_comments(self, group_id):
        return self._request('GET', f'pages/{group_id}/comments/')

    def create_comment(self, group_id, *, content, data=None):
        return self._request('POST', f'pages/{group_id}/comments/', payload={
            'content': content,
            'data': data or {},
        })


def _parse_json(raw):
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.debug('Non-JSON corun-ai API response: %s', raw[:200])
        return None
