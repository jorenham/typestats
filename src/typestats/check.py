import json
import math
import sys
from collections.abc import Sequence

import anyio


async def _exec_pyrefly(args: Sequence[str]) -> None:
    """Run `python -m pyrefly <args>`, streaming output, then exit with its code."""
    cmd = [sys.executable, "-m", "pyrefly", *args]
    result = await anyio.run_process(cmd, check=False, stdout=None, stderr=None)
    sys.exit(result.returncode)


def _build_check_args(
    paths: tuple[str, ...],
    *,
    strict: bool,
    concise: bool,
    fail_under: float | None,
    exclude: Sequence[str],
) -> list[str]:
    """Map typestats `check` options onto a `pyrefly coverage check` arg list."""
    args = ["coverage", "check", "--public-only"]
    if strict:
        args.append("--strict")
    if concise:
        args += ["--output-format", "min-text"]
    # pyrefly defaults `--fail-under` to 100; pass 0 to keep "exit 0 unless asked".
    args += ["--fail-under", str(fail_under if fail_under is not None else 0)]
    args += [f"--project-excludes={pat}" for pat in exclude]
    args += list(paths)
    return args


def _truncate2(pct: float) -> float:
    """Truncate to 2 decimals; never round up, or unchanged code fails the gate."""
    return math.floor(pct * 100) / 100


async def _baseline_fail_under(path: anyio.Path, *, strict: bool) -> float:
    """Read a `pyrefly coverage report` baseline; return its (strict) coverage %.

    Raises:
        SystemExit: if the file is missing or is not a pyrefly coverage report.
    """
    try:
        summary = json.loads(await path.read_bytes())["summary"]
        pct = float(summary["strict_coverage"] if strict else summary["coverage"])
    except (OSError, ValueError, LookupError, TypeError) as exc:
        msg = (
            f"{path} is not a `pyrefly coverage report`; "
            f"regenerate it with `typestats report ... > {path}`."
        )
        raise SystemExit(msg) from exc
    return _truncate2(pct)


async def check(
    *paths: str,
    strict: bool = False,
    concise: bool = False,
    fail_under: float | None = None,
    fail_under_from: anyio.Path | None = None,
    exclude: Sequence[str] = (),
) -> None:
    """Run `pyrefly coverage check` (deprecated alias)."""
    if fail_under_from is not None:
        fail_under = await _baseline_fail_under(fail_under_from, strict=strict)

    args = _build_check_args(
        paths,
        strict=strict,
        concise=concise,
        fail_under=fail_under,
        exclude=exclude,
    )
    await _exec_pyrefly(args)


async def report(*paths: str, exclude: Sequence[str] = ()) -> None:
    """Run `pyrefly coverage report` (deprecated alias)."""
    args = ["coverage", "report", "--public-only"]
    args += [f"--project-excludes={pat}" for pat in exclude]
    args += list(paths)
    await _exec_pyrefly(args)
