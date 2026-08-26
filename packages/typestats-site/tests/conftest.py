from collections.abc import Callable

import pytest
from pytest_httpx import HTTPXMock

from typestats._testing import mock_uv_factory
from typestats_site._testing import PyPIMocker


@pytest.fixture
def mock_uv(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    return mock_uv_factory(
        monkeypatch,
        default_target="typestats_site._uv.install_to_venv",
    )


@pytest.fixture
def pypi(httpx_mock: HTTPXMock) -> PyPIMocker:
    return PyPIMocker(httpx_mock)
