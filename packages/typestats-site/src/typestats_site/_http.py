import httpx
from httpx_retries import RetryTransport

__all__ = ("retry_client",)


def retry_client() -> httpx.AsyncClient:
    """Create an `httpx.AsyncClient` with automatic retries for transient errors.

    Uses `httpx-retries` default retry policy with HTTP/2 enabled on the
    underlying transport.
    """
    return httpx.AsyncClient(
        transport=RetryTransport(transport=httpx.AsyncHTTPTransport(http2=True)),
    )
