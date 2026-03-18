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
from typing import TYPE_CHECKING, ClassVar, Final, NamedTuple

import anyio
import anyio.to_thread
from packaging.version import Version

from typestats.index import PyTyped
from typestats.projects import load_projects
from typestats.report import ClassReport, ModuleReport, PackageReport, StubsOnly

if TYPE_CHECKING:
    from _typeshed import StrPath
    from jinja2 import Environment

    from typestats import analyze

__all__ = ("build_site",)


_logger: Final = logging.getLogger(__name__)

_DEFAULT_PROJECTS: Final = Path(__file__).parents[2] / "projects.toml"

_MIN_VERSIONS_FOR_DIFF: Final = 2

_DATA_BASE_URL: Final = (
    "https://raw.githubusercontent.com/jorenham/typestats/data/reports"
)


def _release_date(r: PackageReport, /) -> str:
    return r.pypi.upload_time[:10] if r.pypi and r.pypi.upload_time else ""


_STUBS_ONLY_LABEL: Final[dict[StubsOnly, str]] = {
    StubsOnly.NO: "",
    StubsOnly.THIRD_PARTY: "third-party",
    StubsOnly.TYPESHED: "typeshed",
}


class _IndexRow(NamedTuple):
    package: str
    version: str
    release_date: str
    coverage: str
    coverage_strict: str
    n_typable: str
    py_typed_sort: int
    py_typed: str
    stubs_only_label: str


class _MetricCell(NamedTuple):
    value: str
    delta: str | None = None
    color: str | None = None


class _ChartData(NamedTuple):
    labels: list[str]
    cov: list[float]
    strict_cov: list[float]
    palette: str
    theme_colors: tuple[str, ...]


class _DiffRow(NamedTuple):
    version: str
    release_date: str
    coverage: _MetricCell
    coverage_strict: _MetricCell
    typables: _MetricCell
    untyped: _MetricCell
    ignores: _MetricCell


class _ModuleRow(NamedTuple):
    display_name: str
    slug: str | None
    coverage: str
    coverage_strict: str
    n_typable: str
    n_type_ignores: str


class _AnnotationRow(NamedTuple):
    name: str
    kind: str
    n_typed: int
    n_any: int
    n_untyped: int


class _AnnotationSection(NamedTuple):
    display_name: str
    slug: str
    n_untyped: int
    n_any: int
    rows: list[_AnnotationRow]


class _SymbolsByKind(NamedTuple):
    functions: int
    classes: int
    attrs: int


@functools.cache
def _get_env() -> Environment:
    from jinja2 import ChoiceLoader, Environment, PackageLoader  # noqa: PLC0415

    return Environment(
        loader=ChoiceLoader([
            PackageLoader("typestats", "templates"),
            PackageLoader("zensical", "templates"),
        ]),
        keep_trailing_newline=True,
        lstrip_blocks=True,
        trim_blocks=True,
        autoescape=True,
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

    def __init__(self, reports: list[PackageReport], /) -> None:
        self._reports = reports

    def render(self) -> str:
        rows = [self._row(r) for r in self._reports]
        template = _get_env().get_template(self.TEMPLATE)
        return template.render(rows=rows)

    @classmethod
    def _row(cls, r: PackageReport, /) -> _IndexRow:
        return _IndexRow(
            package=r.package,
            version=r.version,
            release_date=_release_date(r),
            coverage=f"{r.coverage():.1%}",
            coverage_strict=f"{r.coverage(True):.1%}",
            n_typable=f"{r.n_typable:,}",
            py_typed_sort=cls._PY_TYPED_SORT[r.py_typed],
            py_typed=r.py_typed.name.lower(),
            stubs_only_label=_STUBS_ONLY_LABEL[r.stubs_only],
        )


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

        # Build rows oldest-to-newest (so deltas reference the
        # previous version), then reverse for newest-first display.
        prevs = [None, *reports[:-1]]
        rows = [
            _DiffRow(
                version=r.version,
                release_date=_release_date(r),
                coverage=self._cov_data(r, prev, strict=False),
                coverage_strict=self._cov_data(r, prev, strict=True),
                typables=self._int_data(
                    r.n_typable,
                    prev.n_typable if prev else None,
                    neutral=True,
                ),
                untyped=self._int_data(
                    r.n_untyped,
                    prev.n_untyped if prev else None,
                    prefer_lower=True,
                ),
                ignores=self._int_data(
                    r.n_type_ignores,
                    prev.n_type_ignores if prev else None,
                    prefer_lower=True,
                ),
            )
            for prev, r in zip(prevs, reports, strict=True)
        ]
        rows.reverse()

        chart = self._chart_data()

        template = _get_env().get_template(self.TEMPLATE)
        return template.render(package=package, rows=rows, chart=chart)

    def _chart_data(self) -> _ChartData:
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

        return _ChartData(
            labels=labels,
            cov=cov,
            strict_cov=strict_cov,
            palette=self._CHART_PALETTE,
            theme_colors=self._CHART_THEME_COLORS,
        )

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
    def _cov_data(
        r: PackageReport,
        prev: PackageReport | None,
        *,
        strict: bool,
    ) -> _MetricCell:
        val = r.coverage(strict)
        if prev is None:
            return _MetricCell(value=f"{val:.1%}")
        delta_pp = (val - prev.coverage(strict)) * 100
        if not delta_pp:
            return _MetricCell(value=f"{val:.1%}")
        sign = "+" if delta_pp > 0 else ""
        return _MetricCell(
            value=f"{val:.1%}",
            delta=f"({sign}{delta_pp:.1f}%)",
            color="green" if delta_pp > 0 else "red",
        )

    @staticmethod
    def _int_data(
        val: int,
        prev_val: int | None,
        /,
        *,
        prefer_lower: bool = False,
        neutral: bool = False,
    ) -> _MetricCell:
        if prev_val is None:
            return _MetricCell(value=str(val))
        delta = val - prev_val
        if delta == 0:
            return _MetricCell(value=str(val))
        sign = "+" if delta > 0 else ""
        color = None if neutral else ("green" if (delta < 0) == prefer_lower else "red")
        return _MetricCell(
            value=str(val),
            delta=f"({sign}{delta})",
            color=color,
        )


class DetailPage:
    TEMPLATE: ClassVar = "detail.md.j2"

    _STUBS_RE: ClassVar = re.compile(r"^(?:(.+)-stubs|types-(.+))$")
    _MERMAID_CONFIG_PIE: ClassVar = {
        "theme": "neutral",
        "themeVariables": {"pieStrokeWidth": "1px"},
        "pie": {"textPosition": 0.85},
    }

    def __init__(
        self,
        report: PackageReport,
        /,
        *,
        diff_link: str | None = None,
        json_url: str | None = None,
    ) -> None:
        self._report = report
        self._diff_link = diff_link
        self._json_url = json_url
        self._sorted_modules = sorted(report.module_reports, key=lambda r: r.path)

    def render(self) -> str:
        annotation_secs, incomplete_slugs = self._annotation_sections()

        template = _get_env().get_template(self.TEMPLATE)
        return template.render(
            report=self._report,
            coverage=f"{self._report.coverage():.1%}",
            strict_coverage=f"{self._report.coverage(True):.1%}",
            py_typed=self._report.py_typed.name.lower(),
            stubs_only_label=_STUBS_ONLY_LABEL[self._report.stubs_only],
            modules=self._modules_data(incomplete_slugs),
            annotation_sections=annotation_secs,
            type_ignores=self._type_ignore_data(),
            project_urls=self._report.project_urls(),
            diff_link=self._diff_link,
            json_url=self._json_url,
            symbols_by_kind=self._symbols_by_kind(),
            mermaid_config_pie=self._MERMAID_CONFIG_PIE,
        )

    def _annotation_sections(self) -> tuple[list[_AnnotationSection], dict[str, str]]:
        """Build collapsible annotation sections for incomplete modules.

        Returns `(sections, incomplete_slugs)` where `incomplete_slugs`
        maps each display name to its HTML anchor slug.
        """
        sections: list[_AnnotationSection] = []
        slugs: dict[str, str] = {}
        package = self._report.package
        for m in self._sorted_modules:
            if not (rows := self._incomplete_annotations(m)):
                continue

            display_name = self._display_module_name(m.name, package)
            # sanitize for HTML id
            slug = re.sub(r"[^\w.-]", "", f"module-{display_name}")
            slugs[display_name] = slug
            sections.append(
                _AnnotationSection(
                    display_name=f"`{display_name}`",
                    slug=slug,
                    n_untyped=sum(r.n_untyped for r in rows),
                    n_any=sum(r.n_any for r in rows),
                    rows=rows,
                )
            )

        return sections, slugs

    def _modules_data(self, incomplete_slugs: dict[str, str]) -> list[_ModuleRow]:
        package = self._report.package
        result: list[_ModuleRow] = []
        for m in self._sorted_modules:
            display_name = self._display_module_name(m.name, package)
            result.append(
                _ModuleRow(
                    display_name=display_name,
                    slug=incomplete_slugs.get(display_name),
                    coverage=f"{m.coverage():.1%}",
                    coverage_strict=f"{m.coverage(True):.1%}",
                    n_typable=str(m.n_typable),
                    n_type_ignores=str(m.n_type_ignores),
                )
            )
        return result

    def _symbols_by_kind(self) -> _SymbolsByKind:
        kind2key = {"function": "functions", "attr": "attrs", "property": "classes"}
        totals = {"functions": 0, "classes": 0, "attrs": 0}
        for m in self._report.module_reports:
            for s in m.symbol_reports:
                if s.kind == "class":
                    for method in s.methods:
                        totals["classes"] += method.n_typable
                    for prop in s.properties:
                        totals["classes"] += prop.n_typable
                else:
                    totals[kind2key[s.kind]] += s.n_typable
        return _SymbolsByKind(**totals)

    def _type_ignore_data(self) -> list[tuple[str, int]]:
        """Return sorted (flavor, count) pairs for type-ignore comments."""

        def _ignore_label(ic: analyze.IgnoreComment) -> str:
            out = f"{ic.kind}: ignore"
            if ic.rules:
                out += f"[{', '.join(sorted(ic.rules))}]"
            return out

        counts = Counter(_ignore_label(ic) for ic in self._report.type_ignores)
        return sorted(counts.items(), key=lambda x: (-x[1], x[0]))

    @staticmethod
    def _incomplete_annotations(report: ModuleReport) -> list[_AnnotationRow]:
        rows: list[_AnnotationRow] = []
        for s in report.symbol_reports:
            if s.n_untyped == 0 and s.n_any == 0:
                continue

            short_name = s.name.removeprefix(f"{report.name}.")

            # expand classes into individual method/property/attr rows.
            if isinstance(s, ClassReport):
                for member in (*s.methods, *s.properties, *s.attrs):
                    if member.n_untyped == 0 and member.n_any == 0:
                        continue

                    kind = "method" if member.kind == "function" else member.kind
                    rows.append(
                        _AnnotationRow(
                            name=member.name,
                            kind=kind,
                            n_typed=member.n_typed,
                            n_any=member.n_any,
                            n_untyped=member.n_untyped,
                        )
                    )
                continue

            rows.append(
                _AnnotationRow(
                    name=short_name,
                    kind=s.kind,
                    n_typed=s.n_typed,
                    n_any=s.n_any,
                    n_untyped=s.n_untyped,
                )
            )

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
        json_url = f"{_DATA_BASE_URL}/{report.package}/{report.version}.json"
        pages.append((
            str(docs_dir / report.package / "index.md"),
            DetailPage(report, diff_link=diff_link, json_url=json_url).render(),
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

    The `docs/` directory itself is preserved (not removed and recreated) so that
    external tools watching it via inotify keep their watches intact.
    """
    site_dir = Path(site_dir_str)
    tmp_dir = Path(tmp_str)

    for f in site_dir.glob("*.md"):
        f.unlink()

    # Remove stale files and empty dirs from docs/ without deleting the
    # directory tree itself (which would break inotify-based watchers).
    docs_dir = site_dir / "docs"
    tmp_docs = tmp_dir / "docs"
    if docs_dir.is_dir():
        new_files: set[Path] = set()
        if tmp_docs.is_dir():
            new_files = {
                p.relative_to(tmp_docs) for p in tmp_docs.rglob("*") if p.is_file()
            }
        for existing in sorted(docs_dir.rglob("*"), reverse=True):
            rel = existing.relative_to(docs_dir)
            if existing.is_file() and rel not in new_files:
                existing.unlink()
            elif existing.is_dir() and not any(existing.iterdir()):
                existing.rmdir()

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
