from importlib.metadata import version


def test_version() -> None:
    v = version("typestats")
    assert v
    assert v[0].isdigit()
