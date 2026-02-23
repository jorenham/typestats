from math import sin
from typing import TypeVar

from pkg import _b as b
from pkg._b import spam
from pkg.a import _private_func, public_func

__all__ = ["T", "__version__", "_private_func", "b", "public_func", "sin", "spam"]

__version__ = "1.0"

T = TypeVar("T")
