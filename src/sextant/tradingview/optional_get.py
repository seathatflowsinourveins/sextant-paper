"""D-80 over MCP 2.3.0's optional GET, without another retry implementation.

streamable_http.py:59-60,224-273 owns retries and catches Exception, not
BaseException. :142-158 identifies stream requests by session/SSE headers;
shared/_httpx_utils.py:67-100,110-140 supplies the same-origin redirect rule.
httpx2 2.16.0 _sse.py:204-208 supplies content-type validation, used as is.
"""

import httpx2
from mcp.client.streamable_http import MCP_SESSION_ID
from mcp.shared._httpx_utils import _within_origin

from sextant.tradingview.stop_record import refusal_rule


def is_optional_get(request, stream_url):
    return (
        stream_url is not None
        and request.method == "GET"
        and MCP_SESSION_ID in request.headers
        and "text/event-stream" in request.headers.get("accept", "")
        and _within_origin(httpx2.URL(stream_url), request.url)
    )


def permits_no_stream(status):
    # No response is uncertain; only the four named HTTP errors are exceptions.
    return status is not None and (status < 400 or status in {400, 404, 405, 406})


async def quota_rule(response):
    # A known 429 must stop even if reading its body would fail.
    if response.status_code == 429:
        return "status"
    await response.aread()
    return refusal_rule({"text": response.text})


def response_error(response):
    if response.status_code >= 400:
        return "status" if permits_no_stream(response.status_code) else None
    if response.has_redirect_location:
        return None  # The SDK follows or declines the redirect before checking SSE.
    try:
        httpx2.EventSource(response)._check_content_type()
    except httpx2.SSEError:
        return "SSEError"
    return None
