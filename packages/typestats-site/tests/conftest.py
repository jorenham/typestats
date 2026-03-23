from collections.abc import Callable

import pytest

from typestats._testing import mock_uv_factory


@pytest.fixture
def mock_uv(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    return mock_uv_factory(
        monkeypatch,
        default_target="typestats_site._uv.install_to_venv",
    )
