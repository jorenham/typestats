import httpx
import pytest

from typestats_site._http import retry_client

pytestmark = pytest.mark.anyio


async def test_retry_client_returns_async_client() -> None:
    client = retry_client()
    assert isinstance(client, httpx.AsyncClient)
    await client.aclose()


async def test_retry_client_context_manager() -> None:
    async with retry_client() as client:
        assert isinstance(client, httpx.AsyncClient)
