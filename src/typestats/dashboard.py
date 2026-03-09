"""Generate the markdown dashboard pages from collected JSON data."""

import asyncio
import calendar
import datetime
import functools
import logging
import operator
import re
import shutil
import tempfile
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Final

import anyio
import anyio.to_thread
from packaging.version import Version
from tabulate import tabulate

from typestats.index import PyTyped
from typestats.projects import load_projects
from typestats.report import ModuleReport, PackageReport, StubsOnly

if TYPE_CHECKING:
    from _typeshed import StrPath
    from jinja2 import Environment

    from typestats import analyze

__all__ = ("build_site",)


_logger: Final = logging.getLogger(__name__)

_DEFAULT_PROJECTS: Final = Path(__file__).parents[2] / "projects.toml"

_MIN_VERSIONS_FOR_DIFF: Final = 2


def _abbr(text: str, title: str, /) -> str:
    return f'<abbr title="{title}">{text}</abbr>'


def _icon(name: str, /, cls: str = "", **attrs: str) -> str:
    parts = [f".{cls}"] if cls else []
    parts += [f'{k}="{v}"' for k, v in attrs.items()]
    return f":{name}:{{ {' '.join(parts)} }}"


def _release_date(r: PackageReport, /) -> str:
    return r.pypi.upload_time[:10] if r.pypi and r.pypi.upload_time else ""


_ICON_PY_TYPED: Final[dict[PyTyped, str]] = {
    PyTyped.YES: _icon("material-check-circle", style="color: #4caf50"),
    PyTyped.NO: _icon("material-close-circle", style="color: #e53935"),
    PyTyped.PARTIAL: _icon("material-progress-check", style="color: #fb8c00"),
    PyTyped.STUBS: _icon("material-check-circle-outline", style="color: #4caf50"),
}
_STUBS_ONLY_LABEL: Final[dict[StubsOnly, str]] = {
    StubsOnly.NO: "",
    StubsOnly.THIRD_PARTY: "third-party",
    StubsOnly.TYPESHED: "typeshed",
}


_COL_COV: Final = _abbr("Coverage", "Percentage of annotated symbols")
_COL_COV_STRICT: Final = _abbr(
    "Coverage (strict)",
    "Percentage of annotated symbols, excluding `Any`",
)
_COL_SYMBOLS: Final = _abbr(
    "Symbols",
    (
        "Number of public annotatable slots: "
        "each function parameter, return type, and variable counts as one"
    ),
)
_COL_UNANNOTATED: Final = _abbr("Unannotated", "Slots without a type annotation")
_COL_IGNORES: Final = _abbr("Ignores", "Number of type-checker ignore comments")


@functools.cache
def _get_env() -> Environment:
    from jinja2 import Environment, PackageLoader  # noqa: PLC0415

    return Environment(
        loader=PackageLoader("typestats", "templates"),
        keep_trailing_newline=True,
        lstrip_blocks=True,
        trim_blocks=True,
        autoescape=False,  # noqa: S701
    )


class IndexPage:
    TEMPLATE: ClassVar = "index.md.j2"

    # Sort values for icon-only columns (lower = better typing status).
    # Exposed as hidden <span> elements so tablesort can order icon cells.
    _PY_TYPED_SORT: ClassVar[dict[PyTyped, int]] = {
        PyTyped.YES: 0,
        PyTyped.STUBS: 1,
        PyTyped.PARTIAL: 2,
        PyTyped.NO: 3,
    }
    _HEADERS: ClassVar = [
        _abbr("Package", "PyPI package name"),
        _abbr("Version", "Latest release version"),
        _abbr("Released", "Release date on PyPI"),
        _COL_COV,
        _COL_COV_STRICT,
        _COL_SYMBOLS,
        _abbr("py.typed", "PEP 561 py.typed marker"),
        _abbr("Stubs-only", "Type info from a standalone stubs package"),
    ]
    _COLALIGN: ClassVar = ("left",) * 3 + ("right",) * 3 + ("left",) * 2

    def __init__(self, reports: list[PackageReport], /) -> None:
        self._reports = reports

    def render(self) -> str:
        table = tabulate(
            [self._row(r) for r in self._reports],
            headers=self._HEADERS,
            colalign=self._COLALIGN,
            tablefmt="pipe",
        )
        template = _get_env().get_template(self.TEMPLATE)
        return template.render(table=table)

    @classmethod
    def _row(cls, r: PackageReport, /) -> list[str]:
        return [
            f"[{r.package}]({r.package}/index.md)",
            r.version,
            _release_date(r),
            f"{r.coverage():.1%}",
            f"{r.coverage(True):.1%}",
            f"{r.n_annotatable:,}",
            (
                f"<span hidden>{cls._PY_TYPED_SORT[r.py_typed]}</span>"
                f"{_ICON_PY_TYPED[r.py_typed]}"
            ),
            _STUBS_ONLY_LABEL[r.stubs_only],
        ]


class DiffPage:
    TEMPLATE: ClassVar = "diff.md.j2"

    _CHART_PALETTE: ClassVar = "#4caf50, #fb8c00"
    _CHART_THEME_COLORS: ClassVar = (
        "xAxisLabelColor",
        "yAxisLabelColor",
        "xAxisTitleColor",
        "yAxisTitleColor",
        "xAxisTickColor",
        "yAxisTickColor",
        "xAxisLineColor",
        "yAxisLineColor",
    )

    def __init__(self, reports: list[PackageReport], /) -> None:
        if len(reports) < _MIN_VERSIONS_FOR_DIFF:
            msg = (
                f"DiffPage requires at least {_MIN_VERSIONS_FOR_DIFF} reports, "
                f"got {len(reports)}"
            )
            raise ValueError(msg)

        self._reports = reports

    def render(self) -> str:
        reports = self._reports
        package = reports[0].package

        headers = [
            "Version",
            _abbr("Released", "Release date on PyPI"),
            _COL_COV,
            _COL_COV_STRICT,
            _COL_SYMBOLS,
            _COL_UNANNOTATED,
            _COL_IGNORES,
        ]
        # Build rows oldest-to-newest (so deltas reference the
        # previous version), then reverse for newest-first display.
        prevs: list[PackageReport | None] = [None, *reports[:-1]]
        rows = [
            [
                r.version,
                _release_date(r),
                self._cov_cell(r, prev, False),
                self._cov_cell(r, prev, True),
                self._int_cell(
                    r.n_annotatable,
                    prev.n_annotatable if prev else None,
                    neutral=True,
                ),
                self._int_cell(
                    r.n_unannotated,
                    prev.n_unannotated if prev else None,
                    prefer_lower=True,
                ),
                self._int_cell(
                    r.n_type_ignores,
                    prev.n_type_ignores if prev else None,
                    prefer_lower=True,
                ),
            ]
            for prev, r in zip(prevs, reports, strict=True)
        ]
        rows.reverse()
        # Latest version (first row after reversal) links to detail.
        rows[0][0] = f"[{rows[0][0]}](index.md)"

        table = tabulate(
            rows,
            headers=headers,
            colalign=("left", *("right",) * (len(headers) - 1)),
            tablefmt="pipe",
        )

        chart = self._chart_data()

        template = _get_env().get_template(self.TEMPLATE)
        return template.render(package=package, table=table, chart=chart)

    def _chart_data(self) -> dict[str, object]:
        """Prepare chart template variables.

        When all reports have upload dates, the x-axis uses monthly
        buckets with date labels. Otherwise falls back to version
        strings.
        """
        reports = self._reports
        cov_raw = [r.coverage() * 100 for r in reports]
        strict_raw = [r.coverage(True) * 100 for r in reports]

        dates = [
            datetime.date.fromisoformat(r.pypi.upload_time[:10])
            for r in reports
            if r.pypi and r.pypi.upload_time
        ]

        if len(dates) == len(reports) >= _MIN_VERSIONS_FOR_DIFF:
            labels, cov, strict_cov = self._monthly_series(dates, cov_raw, strict_raw)
        else:
            labels = [r.version for r in reports]
            cov = [round(v, 1) for v in cov_raw]
            strict_cov = [round(v, 1) for v in strict_raw]

        return {
            "labels": labels,
            "cov": cov,
            "strict_cov": strict_cov,
            "palette": self._CHART_PALETTE,
            "theme_colors": self._CHART_THEME_COLORS,
        }

    @staticmethod
    def _monthly_series(
        dates: list[datetime.date],
        cov: list[float],
        strict: list[float],
    ) -> tuple[list[str], list[float], list[float]]:
        """Quantize coverage data into monthly buckets.

        Multiple releases within the same month are collapsed by taking
        the maximum coverage value. Months with no releases carry
        forward the previous month's value, giving a continuous series
        with one tick per calendar month.
        """
        buckets: dict[tuple[int, int], tuple[float, float]] = {}
        for d, c, s in zip(dates, cov, strict, strict=True):
            key = (d.year, d.month)
            prev_c, prev_s = buckets.get(key, (c, s))
            buckets[key] = (max(prev_c, c), max(prev_s, s))

        year, month = dates[0].year, dates[0].month
        end = (dates[-1].year, dates[-1].month)

        labels: list[str] = []
        out_cov: list[float] = []
        out_strict: list[float] = []
        last_c, last_s = cov[0], strict[0]

        while (year, month) <= end:
            labels.append(f"{calendar.month_abbr[month]} {year}")
            last_c, last_s = buckets.get((year, month), (last_c, last_s))
            out_cov.append(round(last_c, 1))
            out_strict.append(round(last_s, 1))
            year, month = year + month // 12, month % 12 + 1

        return labels, out_cov, out_strict

    @staticmethod
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

    @staticmethod
    def _int_cell(
        val: int,
        prev_val: int | None,
        /,
        *,
        prefer_lower: bool = False,
        neutral: bool = False,
    ) -> str:
        formatted = str(val)
        if prev_val is None:
            return formatted
        delta = val - prev_val
        if delta == 0:
            return formatted
        sign = "+" if delta > 0 else ""
        if neutral:
            return f"{formatted}<br>({sign}{delta})"
        color = "green" if (delta < 0) == prefer_lower else "red"
        span = f'<span style="color:{color}">({sign}{delta})</span>'
        return f"{formatted}<br>{span}"


class DetailPage:
    TEMPLATE: ClassVar = "detail.md.j2"

    _STUBS_RE: ClassVar = re.compile(r"^(?:(.+)-stubs|types-(.+))$")

    _INDENT: ClassVar = " " * 4
    _ICON_INCOMPLETE: ClassVar = _icon(
        "material-arrow-down-right",
        cls="md-icon",
        style="vertical-align: middle",
    )
    _HEADERS: ClassVar = (
        "Module",
        _COL_COV,
        _COL_COV_STRICT,
        _COL_SYMBOLS,
        _COL_IGNORES,
    )

    def __init__(
        self,
        report: PackageReport,
        /,
        *,
        diff_link: str | None = None,
    ) -> None:
        self._report = report
        self._diff_link = diff_link
        self._sorted_modules = sorted(report.module_reports, key=lambda r: r.path)

    def render(self) -> str:
        annotation_secs, incomplete_slugs = self._annotation_sections()

        template = _get_env().get_template(self.TEMPLATE)
        return template.render(
            report=self._report,
            coverage=f"{self._report.coverage():.1%}",
            strict_coverage=f"{self._report.coverage(True):.1%}",
            py_typed_icon=_ICON_PY_TYPED[self._report.py_typed],
            stubs_only_label=_STUBS_ONLY_LABEL[self._report.stubs_only],
            modules_table=self._modules_table(incomplete_slugs),
            annotation_sections=annotation_secs,
            type_ignore_table=self._type_ignore_table(),
            project_urls=self._report.project_urls(),
            diff_link=self._diff_link,
        )

    def _annotation_sections(self) -> tuple[list[dict[str, str | int]], dict[str, str]]:
        """Build collapsible annotation sections for incomplete modules.

        Returns `(sections, incomplete_slugs)` where `incomplete_slugs`
        maps each display name to its HTML anchor slug.
        """
        sections: list[dict[str, str | int]] = []
        slugs: dict[str, str] = {}
        package = self._report.package
        for m in self._sorted_modules:
            if not (rows := self._annotation_status(m)):
                continue

            display_name = self._display_module_name(m.name, package)
            # sanitize for HTML id
            slug = re.sub(r"[^\w.-]", "", f"module-{display_name}")
            slugs[display_name] = slug
            sections.append({
                "display_name": f"`{display_name}`",
                "slug": slug,
                "n_issues": len(rows),
                "table": self._indent(
                    tabulate(
                        rows,
                        headers=[
                            "Symbol",
                            "Kind",
                            "Status",
                            _abbr("Annotated", "Slots with a type annotation"),
                            _abbr("Any", "Slots typed as Any"),
                            _COL_UNANNOTATED,
                        ],
                        colalign=("left", "left", "left", "right", "right", "right"),
                        tablefmt="pipe",
                    ),
                ),
            })
        return sections, slugs

    def _modules_table(self, incomplete_slugs: dict[str, str]) -> str:
        package = self._report.package

        def _cell(m: ModuleReport) -> str:
            display_name = self._display_module_name(m.name, package)
            if display_name in incomplete_slugs:
                slug = incomplete_slugs[display_name]
                icon = f'[{self._ICON_INCOMPLETE}](#{slug} "Incomplete annotations")'
                return f"`{display_name}` {icon}"
            return f"`{display_name}`"

        return tabulate(
            [
                [
                    _cell(m),
                    f"{m.coverage():.1%}",
                    f"{m.coverage(True):.1%}",
                    str(m.n_annotatable),
                    str(m.n_type_ignores),
                ]
                for m in self._sorted_modules
            ],
            headers=self._HEADERS,
            colalign=("left", "right", "right", "right", "right"),
            tablefmt="pipe",
        )

    def _type_ignore_table(self) -> str:
        """Render the type-ignore comments table, or empty string."""

        def _ignore_label(ic: analyze.IgnoreComment) -> str:
            out = f"{ic.kind}: ignore"
            if ic.rules:
                out += f"[{', '.join(sorted(ic.rules))}]"
            return out

        counts = Counter(_ignore_label(ic) for ic in self._report.type_ignores)
        sorted_counts = sorted(counts.items(), key=lambda x: (-x[1], x[0]))
        if not sorted_counts:
            return ""
        return tabulate(
            [[f"`{flavor}`", str(count)] for flavor, count in sorted_counts],
            headers=[
                _abbr("Flavor", "Type-checker ignore directive"),
                "Count",
            ],
            colalign=("left", "right"),
            tablefmt="pipe",
        )

    @classmethod
    def _indent(cls, text: str, /) -> str:
        """Indent each line by 4 spaces for pymdownx admonition blocks."""
        return "\n".join(f"{cls._INDENT}{line}" for line in text.splitlines())

    @staticmethod
    def _annotation_status(
        report: ModuleReport,
    ) -> list[tuple[str, str, str, str, str, str]]:
        """Return rows for symbols with imperfect annotations."""
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

            short_name = s.name.removeprefix(f"{report.name}.")
            rows.append((
                f"`{short_name}`",
                s.kind,
                status,
                str(s.n_annotated),
                str(s.n_any),
                str(s.n_unannotated),
            ))

        return rows

    @classmethod
    def _display_module_name(cls, module: str, package: str, /) -> str:
        """Normalize module name for stubs packages.

        For stubs packages (e.g. `scipy-stubs`), the on-disk directory
        is `scipy-stubs/`, so `ModuleReport.name` yields
        `scipy-stubs.fft`. This replaces the top-level component with
        the base package name, producing `scipy.fft`.
        """
        if not (m := cls._STUBS_RE.match(package)):
            return module

        base = m.group(1) or m.group(2)
        stubs_prefix = module.split(".", maxsplit=1)[0]
        return base + module.removeprefix(stubs_prefix)


async def _load_all_version_reports(
    data_dir: anyio.Path,
    projects_path: StrPath,
    /,
) -> dict[str, list[PackageReport]]:
    """Load all available version reports for every project.

    Returns a dict keyed by project name. Each value is a list of
    `PackageReport` objects sorted oldest-to-newest by version.
    Projects with no data directory are skipped with a warning.
    """
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
    flat_paths = (p for _, paths in per_project for p in paths)
    raws = await asyncio.gather(*(p.read_bytes() for p in flat_paths))

    # Reconstruct per-project lists in the original sorted order.
    result: dict[str, list[PackageReport]] = {}
    i = 0
    for name, paths in per_project:
        result[name] = [
            PackageReport.model_validate_json(raws[j]) for j in range(i, i + len(paths))
        ]
        i += len(paths)
    return result


def _pkg_page_entries(
    docs_dir: anyio.Path,
    reports: list[PackageReport],
    all_reports: dict[str, list[PackageReport]],
    /,
) -> list[tuple[str, str]]:
    """Build per-package `(path, content)` entries for `docs_dir`."""
    pages: list[tuple[str, str]] = []
    for report in reports:
        pkg_versions = all_reports.get(report.package, [])
        has_diff = len(pkg_versions) >= _MIN_VERSIONS_FOR_DIFF

        diff_link = "diff.md" if has_diff else None
        pages.append((
            str(docs_dir / report.package / "index.md"),
            DetailPage(report, diff_link=diff_link).render(),
        ))

        if has_diff:
            pages.append((
                str(docs_dir / report.package / "diff.md"),
                DiffPage(pkg_versions).render(),
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


async def _write_pages(pages: list[tuple[str, str]], /) -> None:
    def _write_pages_sync(pages: list[tuple[str, str]], /) -> None:
        for path_str, content in pages:
            p = Path(path_str)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")

    await anyio.to_thread.run_sync(_write_pages_sync, pages)


def _install_site_dir(tmp_str: str, site_dir_str: str) -> None:
    """Replace the markdown content of `site_dir` with the build in `tmp_str`.

    Only the `.md` files directly in `site_dir` and the `docs/` subtree are replaced.
    Other files (e.g. `.preview_sha`, `.reports/`) are left intact.
    This ensures stale package pages are removed when projects are renamed or deleted.
    """
    site_dir = Path(site_dir_str)
    for f in site_dir.glob("*.md"):
        f.unlink()
    shutil.rmtree(site_dir / "docs", ignore_errors=True)
    shutil.copytree(tmp_str, site_dir_str, dirs_exist_ok=True)


async def build_site(
    data_dir: anyio.Path,
    site_dir: anyio.Path,
    projects_path: StrPath = _DEFAULT_PROJECTS,
    /,
    *,
    reports: list[PackageReport] | None = None,
    all_reports: dict[str, list[PackageReport]] | None = None,
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

    Raises:
        RuntimeError: If no reports could be loaded.
    """
    if all_reports is None:
        all_reports = await _load_all_version_reports(data_dir, projects_path)
    if reports is None:
        reports = [r[-1] for r in all_reports.values()]

    if not reports:
        msg = "No reports loaded -- cannot build dashboard"
        raise RuntimeError(msg)

    await site_dir.mkdir(parents=True, exist_ok=True)

    tmp_str = tempfile.mkdtemp(dir=site_dir.parent, prefix=".build_")
    try:
        tmp_docs = anyio.Path(tmp_str) / "docs"
        await tmp_docs.mkdir()

        committed_docs = site_dir.parent / "docs"
        if await committed_docs.exists():
            await _copy_tree(committed_docs, tmp_docs)

        pages = [(str(tmp_docs / "index.md"), IndexPage(reports).render())]
        pages += _pkg_page_entries(tmp_docs, reports, all_reports)
        await _write_pages(pages)

        await anyio.to_thread.run_sync(_install_site_dir, tmp_str, str(site_dir))
    finally:
        shutil.rmtree(tmp_str, ignore_errors=True)

    n_diff = sum(
        len(all_reports.get(r.package, [])) >= _MIN_VERSIONS_FOR_DIFF for r in reports
    )
    _logger.info(
        "Wrote index + %d detail + %d diff page(s) to %s",
        len(reports),
        n_diff,
        site_dir,
    )
    return reports, all_reports
