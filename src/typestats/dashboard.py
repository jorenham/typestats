"""Generate the markdown dashboard pages from collected JSON data."""

import asyncio
import functools
import logging
import operator
import re
import shutil
import tempfile
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Final, NotRequired, TypedDict

import anyio
import anyio.to_thread
import httpx
from packaging.version import Version
from tabulate import tabulate

from typestats.projects import load_projects
from typestats.report import ModuleReport, PackageReport

if TYPE_CHECKING:
    from _typeshed import StrPath
    from jinja2 import Environment

    from typestats import analyze

__all__ = ("TEMPLATES", "build_site")


_logger: Final = logging.getLogger(__name__)
_DEFAULT_PROJECTS: Final = Path(__file__).parents[2] / "projects.toml"

# Pattern for stubs package names: {name}-stubs or types-{name}
_STUBS_RE: Final = re.compile(r"^(?:(.+)-stubs|types-(.+))$")

_DETAIL_TEMPLATE: Final = "detail.md.j2"
_DIFF_TEMPLATE: Final = "diff.md.j2"
TEMPLATES: Final = frozenset({_DETAIL_TEMPLATE, _DIFF_TEMPLATE})

_MIN_VERSIONS_FOR_DIFF: Final = 2

_PAGE_FRONTMATTER: Final = """\
---
hide:
  - navigation
  - path
---

"""
_INDEX_FRONTMATTER: Final = """\
---
hide:
  - navigation
  - path
  - toc
---

# Overview

"""

_INDENT: Final = " " * 4

_env: Environment | None = None


def _get_env() -> Environment:
    """Lazily create the Jinja2 template environment."""
    global _env  # noqa: PLW0603

    if _env is None:
        from jinja2 import Environment, PackageLoader  # noqa: PLC0415

        _env = Environment(
            loader=PackageLoader("typestats", "templates"),
            keep_trailing_newline=True,
            lstrip_blocks=True,
            trim_blocks=True,
            autoescape=False,  # noqa: S701
        )

    return _env


def _display_module_name(module_name: str, package: str, /) -> str:
    """Normalize module name for stubs packages.

    For stubs packages (e.g. `scipy-stubs`), the on-disk directory is
    `scipy-stubs/`, so `ModuleReport.name` yields `scipy-stubs.fft`.
    This function replaces the top-level component with the base package
    name, producing `scipy.fft`.
    """
    m = _STUBS_RE.match(package)
    if m is None:
        return module_name
    base = m.group(1) or m.group(2)
    stubs_prefix = module_name.split(".", maxsplit=1)[0]
    return base + module_name.removeprefix(stubs_prefix)


async def _load_all_version_reports(
    data_dir: anyio.Path,
    projects_path: StrPath | None,
    /,
) -> dict[str, list[PackageReport]]:
    """Load all available version reports for every project.

    Returns a dict keyed by project name. Each value is a list of
    `PackageReport` objects sorted oldest-to-newest by version.
    Projects with no data directory are skipped with a warning.
    """
    if projects_path is None:
        projects_path = _DEFAULT_PROJECTS
    projects = load_projects(projects_path)

    # Collect (project_name, version-sorted paths) for every project that has data.
    per_project: list[tuple[str, list[anyio.Path]]] = []
    for project in projects:
        project_dir = data_dir / project.name
        if not await project_dir.is_dir():
            _logger.warning("No data directory for %s, skipping", project.name)
            continue
        versioned = sorted(
            [
                (Version(json_file.stem), json_file)
                async for json_file in project_dir.glob("*.json")
            ],
            key=operator.itemgetter(0),
        )
        if versioned:
            per_project.append((project.name, [p for _, p in versioned]))

    # Read all files in parallel.
    flat_paths = [p for _, paths in per_project for p in paths]
    raws = await asyncio.gather(*[p.read_bytes() for p in flat_paths])

    # Reconstruct per-project lists in the original sorted order.
    result: dict[str, list[PackageReport]] = {}
    i = 0
    for name, paths in per_project:
        result[name] = [
            PackageReport.model_validate_json(raws[i + j]) for j in range(len(paths))
        ]
        i += len(paths)
    return result


def render_index(reports: list[PackageReport], /) -> str:
    return tabulate(
        [
            [
                f"[{r.package}]({r.package}/index.md)",
                r.version,
                f"{r.coverage():.1%}",
                f"{r.coverage(True):.1%}",
                str(r.n_annotatable),
                r.py_typed.name,
                r.stubs_only.value,
            ]
            for r in reports
        ],
        headers=[
            "Package",
            "Version",
            "Coverage",
            "Strict Coverage",
            "Public Symbols",
            "`py.typed`",
            "Stub-only",
        ],
        colalign=("left", "left", "right", "right", "right", "left", "left"),
        tablefmt="pipe",
    )


def _indent(text: str, /) -> str:
    """Indent each line of `text` by 4 spaces for pymdownx admonition blocks."""
    return "\n".join(f"{_INDENT}{line}" for line in text.splitlines())


def _annotation_status(
    report: ModuleReport,
) -> list[tuple[str, str, str, str, str, str]]:
    """
    Return rows of (symbol, kind, status, annotated, any, unannotated) for imperfect
    symbols.
    """
    rows: list[tuple[str, str, str, str, str, str]] = []
    for s in report.symbol_reports:
        if s.n_unannotated == 0 and s.n_any == 0:
            continue

        if s.n_unannotated > 0 and s.n_any > 0:
            status = "missing + Any"
        elif s.n_unannotated > 0:
            status = "missing"
        else:
            status = "Any"

        # Strip module prefix from symbol name for brevity
        short_name = s.name.removeprefix(f"{report.name}.")
        rows.append((
            short_name,
            s.kind,
            status,
            str(s.n_annotated),
            str(s.n_any),
            str(s.n_unannotated),
        ))

    return rows


# Hosts that indicate a repository URL.
_REPO_HOSTS: Final = {
    "github.com",
    "gitlab.com",
    "bitbucket.org",
    "codeberg.org",
    "sr.ht",
}


class _ProjectUrls(TypedDict):
    pypi: str
    repo: NotRequired[str]


def _extract_project_urls(report: PackageReport, /) -> _ProjectUrls:
    urls: _ProjectUrls = {"pypi": f"https://pypi.org/project/{report.package}/"}

    if report.metadata:
        for entry in report.metadata.get("Project-URL", []):
            url = entry.rsplit(",", 1)[-1].strip()
            assert url, f"Malformed Project-URL: {entry!r}"

            if httpx.URL(url).host in _REPO_HOSTS:
                urls["repo"] = url
                break

    return urls


def render_diff(reports: list[PackageReport], /) -> str:
    """Render a version-history diff page for a package.

    `reports` must be sorted oldest-to-newest and contain at least 2 entries.
    The rendered table is displayed newest-first.

    Raises:
        ValueError: If fewer than 2 reports are provided.
    """
    if len(reports) < _MIN_VERSIONS_FOR_DIFF:
        msg = f"render_diff requires at least 2 reports, got {len(reports)}"
        raise ValueError(msg)

    package = reports[0].package

    def _cov_cell(r: PackageReport, prev: PackageReport | None, strict: bool) -> str:
        val = r.coverage(strict)
        formatted = f"{val:.1%}"
        if prev is None:
            return formatted
        delta_pp = (val - prev.coverage(strict)) * 100
        if not delta_pp:
            return formatted
        color = "green" if delta_pp > 0 else "red"
        sign = "+" if delta_pp > 0 else ""
        span = f'<span style="color:{color}">({sign}{delta_pp:.1f}%)</span>'
        return f"{formatted}<br>{span}"

    def _int_cell(
        r: PackageReport,
        prev: PackageReport | None,
        attr: str,
        *,
        lower_is_better: bool = False,
        neutral: bool = False,
    ) -> str:
        val: int = getattr(r, attr)
        formatted = str(val)
        if prev is None:
            return formatted
        delta = val - getattr(prev, attr)
        if delta == 0:
            return formatted
        sign = "+" if delta > 0 else ""
        if neutral:
            return f"{formatted}<br>({sign}{delta})"
        # For directional metrics: green = improvement, red = regression.
        color = "green" if (delta < 0) == lower_is_better else "red"
        span = f'<span style="color:{color}">({sign}{delta})</span>'
        return f"{formatted}<br>{span}"

    headers = [
        "Version",
        "Coverage",
        "Strict Coverage",
        "Public Symbols",
        "Unannotated",
        "Type-ignores",
    ]
    # Build rows oldest-to-newest (so deltas reference the previous version),
    # then reverse for newest-first display.
    rows = [
        [
            r.version,
            _cov_cell(r, reports[i - 1] if i > 0 else None, False),
            _cov_cell(r, reports[i - 1] if i > 0 else None, True),
            _int_cell(
                r, reports[i - 1] if i > 0 else None, "n_annotatable", neutral=True
            ),
            _int_cell(
                r,
                reports[i - 1] if i > 0 else None,
                "n_unannotated",
                lower_is_better=True,
            ),
            _int_cell(
                r,
                reports[i - 1] if i > 0 else None,
                "n_type_ignores",
                lower_is_better=True,
            ),
        ]
        for i, r in enumerate(reports)
    ]
    rows.reverse()
    # Make the latest version (first row after reversal) link to the detail page.
    rows[0][0] = f"[{rows[0][0]}](index.md)"

    table = tabulate(
        rows,
        headers=headers,
        colalign=("left", *("right",) * (len(headers) - 1)),
        tablefmt="pipe",
    )

    template = _get_env().get_template(_DIFF_TEMPLATE)
    return template.render(package=package, table=table)


def render_detail(report: PackageReport, /, *, diff_link: str | None = None) -> str:
    """Render a detailed markdown page for a single package report."""
    sorted_modules = sorted(report.module_reports, key=lambda r: r.path)

    # Pre-render modules table
    modules_table = tabulate(
        [
            [
                f"`{_display_module_name(m.name, report.package)}`",
                f"{m.coverage():.1%}",
                f"{m.coverage(True):.1%}",
                str(m.n_annotatable),
                str(m.n_type_ignores),
            ]
            for m in sorted_modules
        ],
        headers=["Module", "Coverage", "Strict Coverage", "Symbols", "Ignores"],
        colalign=("left", "right", "right", "right", "right"),
        tablefmt="pipe",
    )

    # Pre-render annotation sections
    annotation_sections: list[dict[str, str | int]] = []
    for m in sorted_modules:
        rows = _annotation_status(m)
        if not rows:
            continue
        annotation_sections.append({
            "display_name": f"`{_display_module_name(m.name, report.package)}`",
            "n_issues": len(rows),
            "table": _indent(
                tabulate(
                    rows,
                    headers=[
                        "Symbol",
                        "Kind",
                        "Status",
                        "Annotated",
                        "Any",
                        "Unannotated",
                    ],
                    colalign=("left", "left", "left", "right", "right", "right"),
                    tablefmt="pipe",
                ),
            ),
        })

    # Pre-render type-ignore counts table
    def _ignore_label(ic: analyze.IgnoreComment) -> str:
        if ic.rules is None:
            return f"{ic.kind}: ignore"
        return f"{ic.kind}: ignore[{', '.join(sorted(ic.rules))}]"

    type_ignore_counter = Counter(_ignore_label(ic) for ic in report.type_ignores)
    type_ignore_counts = sorted(
        type_ignore_counter.items(), key=lambda x: (-x[1], x[0])
    )
    type_ignore_table = (
        tabulate(
            [[f"`{flavor}`", str(count)] for flavor, count in type_ignore_counts],
            headers=["Flavor", "Count"],
            colalign=("left", "right"),
            tablefmt="pipe",
        )
        if type_ignore_counts
        else ""
    )

    project_urls = _extract_project_urls(report)

    template = _get_env().get_template(_DETAIL_TEMPLATE)
    return template.render(
        report=report,
        coverage=f"{report.coverage():.1%}",
        strict_coverage=f"{report.coverage(True):.1%}",
        modules_table=modules_table,
        annotation_sections=annotation_sections,
        type_ignore_table=type_ignore_table,
        project_urls=project_urls,
        diff_link=diff_link,
    )


async def _ensure_reports(
    data_dir: anyio.Path,
    projects_path: StrPath | None,
    reports: list[PackageReport] | None,
    all_reports: dict[str, list[PackageReport]] | None,
    /,
) -> tuple[list[PackageReport], dict[str, list[PackageReport]]]:
    """Load reports from disk if not provided, deriving `reports` from `all_reports`."""
    if all_reports is None:
        all_reports = await _load_all_version_reports(data_dir, projects_path)
    if reports is None:
        reports = [r[-1] for r in all_reports.values()]
    return reports, all_reports


def _pkg_page_entries(
    docs_dir: anyio.Path,
    reports: list[PackageReport],
    all_reports: dict[str, list[PackageReport]],
    /,
    *,
    include_detail: bool,
    include_diff: bool,
) -> list[tuple[str, str]]:
    """Build per-package `(path, content)` entries for `docs_dir`."""
    pages: list[tuple[str, str]] = []
    for report in reports:
        pkg_versions = all_reports.get(report.package, [])
        has_diff = len(pkg_versions) >= _MIN_VERSIONS_FOR_DIFF
        if include_detail:
            diff_link = "diff.md" if has_diff else None
            pages.append((
                str(docs_dir / report.package / "index.md"),
                _PAGE_FRONTMATTER + render_detail(report, diff_link=diff_link),
            ))
        if has_diff and include_diff:
            pages.append((
                str(docs_dir / report.package / "diff.md"),
                render_diff(pkg_versions),
            ))
    return pages


async def _copy_tree(src: anyio.Path, dst: anyio.Path, /) -> None:
    """Recursively copy `src` into `dst`, creating directories as needed."""
    await dst.mkdir(parents=True, exist_ok=True)
    async for entry in src.iterdir():
        target = dst / entry.name
        if await entry.is_dir():
            await _copy_tree(entry, target)
        else:
            await target.write_bytes(await entry.read_bytes())


async def _write_pages_async(pages: list[tuple[str, str]], /) -> None:
    def _write_pages(pages: list[tuple[str, str]], /) -> None:
        for path_str, content in pages:
            p = Path(path_str)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")

    await anyio.to_thread.run_sync(_write_pages, pages)


def _install_site_dir(tmp_str: str, site_dir_str: str) -> None:
    """Replace the markdown content of `site_dir` with the build in `tmp_str`.

    Only the `.md` files directly in `site_dir` and the `docs/` subtree are replaced.
    Other files (e.g. `.preview_sha`, `.reports/`) are left intact.
    This ensures stale package pages are removed when projects are renamed or deleted.
    """
    site_dir = Path(site_dir_str)
    for f in site_dir.glob("*.md"):
        f.unlink()
    docs = site_dir / "docs"
    if docs.exists():
        shutil.rmtree(docs)
    shutil.copytree(tmp_str, site_dir_str, dirs_exist_ok=True)


async def build_site(  # noqa: PLR0913
    data_dir: anyio.Path,
    site_dir: anyio.Path,
    projects_path: StrPath | None = None,
    /,
    *,
    reports: list[PackageReport] | None = None,
    all_reports: dict[str, list[PackageReport]] | None = None,
    rebuild: frozenset[str] | None = None,
) -> tuple[list[PackageReport], dict[str, list[PackageReport]]]:
    """Build the markdown pages and write them to `site_dir`.

    All pages are written directly into `site_dir/docs/` with MkDocs frontmatter
    prepended. The committed `docs/` directory (next to `site_dir`) is copied into
    `site_dir/docs/` first so that static assets (e.g. scripts/) are preserved.

    If `all_reports` is provided, it is used as-is (incremental rebuild). When absent,
    all version JSON files are loaded from disk and `reports` (the latest per package)
    is derived from `all_reports`. Pass `reports` explicitly only when you need to
    override which version counts as "latest" for the detail and index pages.

    Returns `(reports, all_reports)` so callers can cache both for the next build.

    If `rebuild` is a frozenset of template filenames (see `TEMPLATES`), only the pages
    driven by those templates are re-rendered and written directly to `site_dir`,
    skipping the temp-dir round-trip.
    Pass `None` (the default) to perform a full rebuild via a temp dir.

    Raises:
        RuntimeError: If no reports could be loaded.
    """
    reports, all_reports = await _ensure_reports(
        data_dir, projects_path, reports, all_reports
    )

    if not reports:
        msg = "No reports loaded -- cannot build dashboard"
        raise RuntimeError(msg)

    await site_dir.mkdir(parents=True, exist_ok=True)

    render_detail_pages = rebuild is None or _DETAIL_TEMPLATE in rebuild
    render_diff_pages = rebuild is None or _DIFF_TEMPLATE in rebuild

    if rebuild is not None:
        # Partial rebuild: only re-render and write the affected pages.
        # Written synchronously in a thread so all inotify events land in one burst.
        pages: list[tuple[str, str]] = []
        if render_detail_pages:
            pages.append((
                str(site_dir / "docs" / "index.md"),
                _INDEX_FRONTMATTER + render_index(reports) + "\n",
            ))
        pages += _pkg_page_entries(
            site_dir / "docs",
            reports,
            all_reports,
            include_detail=render_detail_pages,
            include_diff=render_diff_pages,
        )
        await _write_pages_async(pages)
    else:
        # Full rebuild: write everything into a temp dir first, then replace site_dir
        # in one blocking call so all changes land in a single inotify burst.
        tmp_str = tempfile.mkdtemp(dir=site_dir.parent, prefix=".build_")
        try:
            tmp_docs = anyio.Path(tmp_str) / "docs"
            await tmp_docs.mkdir()
            committed_docs = site_dir.parent / "docs"
            if await committed_docs.exists():
                await _copy_tree(committed_docs, tmp_docs)
            full_pages: list[tuple[str, str]] = [
                (
                    str(tmp_docs / "index.md"),
                    _INDEX_FRONTMATTER + render_index(reports) + "\n",
                )
            ]
            full_pages += _pkg_page_entries(
                tmp_docs,
                reports,
                all_reports,
                include_detail=True,
                include_diff=True,
            )
            await _write_pages_async(full_pages)
            await anyio.to_thread.run_sync(
                functools.partial(_install_site_dir, tmp_str, str(site_dir))
            )
        finally:
            shutil.rmtree(tmp_str, ignore_errors=True)

    n_diff = sum(
        1
        for r in reports
        if len(all_reports.get(r.package, [])) >= _MIN_VERSIONS_FOR_DIFF
    )
    _logger.info(
        "Wrote index + %d detail + %d diff page(s) to %s",
        len(reports),
        n_diff,
        site_dir,
    )
    return reports, all_reports
