import importlib.metadata
from unittest.mock import patch

import pytest

from typestats.__main__ import Version, _run


class TestVersion:
    pytestmark = pytest.mark.anyio

    async def test_prints_version(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch.object(importlib.metadata, "version", return_value="1.2.3"):
            await _run(Version())

        assert capsys.readouterr().out.strip() == "1.2.3"


class TestDeprecation:
    pytestmark = pytest.mark.anyio

    async def test_cli_emits_deprecation_warning(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch.object(importlib.metadata, "version", return_value="1.2.3"):
            await _run(Version())

        err = capsys.readouterr().err
        assert "deprecated" in err.lower()
        assert "pyrefly coverage" in err
