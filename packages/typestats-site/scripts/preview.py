# ruff: noqa: INP001

"""Preview the docs site locally.

Workflow:
1. Extract report data from the `data` git branch into a temporary directory.
2. Build dashboard pages via `build_site`.
3. Run `zensical serve` to build and serve the docs site locally.

On repeat runs, steps 1-2 are skipped when the data branch SHA is unchanged.
Template and config changes are detected automatically and trigger a rebuild.
Changes to `dashboard.py` are auto-reloaded; other Python source changes
require a manual restart.

Usage:
    uv run scripts/preview.py [--clean] [zensical-serve-flags ...]

Examples:
    uv run scripts/preview.py
    uv run scripts/preview.py --clean
    uv run scripts/preview.py --dev-addr 0.0.0.0:9000
"""

import asyncio
import contextlib
import importlib
import logging
import os
import shutil
import sys
import time
from subprocess import PIPE
from typing import TYPE_CHECKING, Final

import anyio
import watchfiles

import typestats_site.dashboard

if TYPE_CHECKING:
    from typestats.report import PackageReport

type _PackageReports = list[PackageReport]

ROOT: Final = anyio.Path(__file__).parent.parent
_SITE_DIR: Final = ROOT / "_site"
_SITE_SHA: Final = _SITE_DIR / ".preview_sha"
_REPORTS_DIR: Final = _SITE_DIR / ".reports"

_CMD: Final = ">"

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


async def _run(
    *args: str,
    cwd: anyio.Path = ROOT,
    env: dict[str, str] | None = None,
    input: bytes | None = None,  # noqa: A002
    stdout: int | None = PIPE,
    stderr: int | None = None,
) -> bytes:
    log.info("%s %s", _CMD, " ".join(args))
    result = await anyio.run_process(
        list(args),
        cwd=cwd,
        stdout=stdout,
        stderr=stderr,
        env=env,
        input=input,
    )
    return result.stdout


async def _resolve_hash() -> str:
    sha = (await _run("git", "rev-parse", "origin/data")).strip().decode()
    log.info("Using origin/data (%s) for report data", sha[:12])
    return sha


async def _extract_into(into: anyio.Path, sha: str) -> None:
    log.info("Extracting report data ...")
    if await into.exists():
        shutil.rmtree(str(into))
    await into.mkdir(parents=True)
    archive = await _run("git", "archive", sha)
    await anyio.run_process(["tar", "-x", "-C", str(into)], input=archive, stderr=None)


async def _watch_and_rebuild(
    reports_dir: anyio.Path,
    initial_reports: _PackageReports | None = None,
    initial_all_reports: dict[str, _PackageReports] | None = None,
) -> None:
    watch_paths = (
        ROOT / "docs",
        ROOT / "src" / "typestats_site" / "templates",
        ROOT / "src" / "typestats_site" / "dashboard.py",
        ROOT / "projects.toml",
    )
    log.info("Watching %s ...", ", ".join(p.name for p in watch_paths))
    cached_reports = initial_reports
    cached_all_reports = initial_all_reports
    async for changes in watchfiles.awatch(*map(str, watch_paths)):
        changed = sorted({anyio.Path(c[1]).name for c in changes})
        log.info("Changed: %s -- rebuilding ...", ", ".join(changed))

        try:
            if "dashboard.py" in changed:
                importlib.reload(typestats_site.dashboard)

            invalidate = "projects.toml" in changed
            t0 = time.perf_counter()
            (
                cached_reports,
                cached_all_reports,
            ) = await typestats_site.dashboard.build_site(
                reports_dir,
                _SITE_DIR,
                ROOT / "projects.toml",
                reports=None if invalidate else cached_reports,
                all_reports=None if invalidate else cached_all_reports,
            )
            log.info("Rebuilt in %.1fs", time.perf_counter() - t0)
        except Exception:
            log.exception("Rebuild failed")


async def _serve(*args: str) -> None:
    log.info("%s zensical serve %s", _CMD, " ".join(args))
    async with await anyio.open_process(
        ["zensical", "serve", *args],
        cwd=ROOT,
        stdout=PIPE,
        stderr=None,
        env={**os.environ, "PYTHON_GIL": "1"},
    ) as proc:
        assert proc.stdout is not None
        with contextlib.suppress(anyio.EndOfStream):
            async for chunk in proc.stdout:
                for line in chunk.decode().splitlines():
                    if line and not line.startswith("+"):
                        log.info(" " * len(_CMD) + " %s", line)  # noqa: G003
    if proc.returncode:
        raise SystemExit(proc.returncode)


async def main() -> None:
    args = sys.argv[1:]
    clean = "--clean" in args
    serve_args = [a for a in args if a != "--clean"]

    t0 = time.perf_counter()

    sha = await _resolve_hash()
    sha_cached = await _SITE_SHA.read_text() if await _SITE_SHA.exists() else None

    initial_reports: _PackageReports | None = None
    initial_all_reports: dict[str, _PackageReports] | None = None
    if not clean and sha == sha_cached and await _REPORTS_DIR.exists():
        log.info("Data unchanged (%s), skipping extraction.", sha[:12])
    else:
        await _extract_into(_REPORTS_DIR, sha)

        log.info("Building dashboard pages ...")
        (initial_reports, initial_all_reports), _ = await asyncio.gather(
            typestats_site.dashboard.build_site(
                _REPORTS_DIR / "reports",
                _SITE_DIR,
                ROOT / "projects.toml",
            ),
            _SITE_SHA.write_text(sha),
        )

    log.info("Built in %.1fs", time.perf_counter() - t0)
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                _watch_and_rebuild,
                _REPORTS_DIR / "reports",
                initial_reports,
                initial_all_reports,
            )
            await _serve(*serve_args)
            tg.cancel_scope.cancel()
    finally:
        if await _REPORTS_DIR.exists():
            shutil.rmtree(str(_REPORTS_DIR))
            log.info("Cleaned up %s", _REPORTS_DIR)


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        anyio.run(main)
