"""Generate the markdown dashboard pages from collected JSON data."""

import logging
import operator
import re
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Final, NotRequired, TypedDict

import httpx
from packaging.version import Version
from tabulate import tabulate

from typestats.projects import load_projects
from typestats.report import ModuleReport, PackageReport

if TYPE_CHECKING:
    import anyio
    from _typeshed import StrPath
    from jinja2 import Environment

    from typestats import analyze

__all__ = ("build_site",)


_logger: Final = logging.getLogger(__name__)
_DEFAULT_PROJECTS: Final = Path(__file__).parents[2] / "projects.toml"

# Pattern for stubs package names: {name}-stubs or types-{name}
_STUBS_RE: Final = re.compile(r"^(?:(.+)-stubs|types-(.+))$")

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


async def _load_latest_reports(
    data_dir: anyio.Path,
    projects_path: StrPath | None,
    /,
) -> list[PackageReport]:
    if projects_path is None:
        projects_path = _DEFAULT_PROJECTS
    projects = load_projects(projects_path)
    reports: list[PackageReport] = []

    for project in projects:
        project_dir = data_dir / project.name
        if not await project_dir.is_dir():
            _logger.warning("No data directory for %s, skipping", project.name)
            continue

        json_files = [
            (Version(json_file.stem), json_file)
            async for json_file in project_dir.glob("*.json")
        ]
        assert json_files

        # Pick the highest version
        _, latest_path = max(json_files, key=operator.itemgetter(0))

        raw = await latest_path.read_bytes()
        reports.append(PackageReport.model_validate_json(raw))

    return reports


def render_index(reports: list[PackageReport], /) -> str:
    return tabulate(
        [
            [
                f"[{r.package}]({r.package}.md)",
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
        colalign=(
            "left",
            "left",
            "right",
            "right",
            "right",
            "left",
            "left",
        ),
        tablefmt="pipe",
    )


_INDENT: Final = "    "


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


def render_detail(report: PackageReport, /) -> str:
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

    template = _get_env().get_template("detail.md.j2")
    return template.render(
        report=report,
        coverage=f"{report.coverage():.1%}",
        strict_coverage=f"{report.coverage(True):.1%}",
        modules_table=modules_table,
        annotation_sections=annotation_sections,
        type_ignore_table=type_ignore_table,
        project_urls=project_urls,
    )


def _detail_wrapper(package: str, site_dir_name: str, /) -> str:
    """Generate a docs wrapper that snippet-includes `_site/{package}.md`."""
    template = _get_env().get_template("wrapper.md.j2")
    return template.render(package=package, site_dir_name=site_dir_name)


async def _copy_tree(src: anyio.Path, dst: anyio.Path, /) -> None:
    """Recursively copy `src` into `dst`, creating directories as needed."""
    await dst.mkdir(parents=True, exist_ok=True)
    async for entry in src.iterdir():
        target = dst / entry.name
        if await entry.is_dir():
            await _copy_tree(entry, target)
        else:
            await target.write_bytes(await entry.read_bytes())


async def build_site(
    data_dir: anyio.Path,
    site_dir: anyio.Path,
    projects_path: StrPath | None = None,
    /,
) -> None:
    """Build the markdown pages and write them to `site_dir`.

    Generated detail pages go into `site_dir/` and wrapper pages into `site_dir/docs/`.
    The committed `docs/` directory (next to `site_dir`) is copied into `site_dir/docs/`
    so that zensical can use `docs_dir = "<site_dir>/docs"`.

    Raises:
        RuntimeError: If no reports could be loaded.
    """
    reports = await _load_latest_reports(data_dir, projects_path)

    if not reports:
        msg = "No reports loaded -- cannot build dashboard"
        raise RuntimeError(msg)

    await site_dir.mkdir(parents=True, exist_ok=True)

    # Index page
    content = render_index(reports)
    out = site_dir / "index.md"
    await out.write_text(content + "\n")
    _logger.info("Wrote index page to %s (%d projects)", out, len(reports))

    # Assemble docs dir: copy committed docs/ then generate wrappers
    assembled_docs = site_dir / "docs"
    committed_docs = site_dir.parent / "docs"
    if await committed_docs.exists():
        await _copy_tree(committed_docs, assembled_docs)

    # Detail pages + wrappers
    site_dir_name = site_dir.name
    for report in reports:
        detail_content = render_detail(report)
        detail_path = site_dir / f"{report.package}.md"
        await detail_path.write_text(detail_content)

        wrapper = _detail_wrapper(report.package, site_dir_name)
        await (assembled_docs / f"{report.package}.md").write_text(wrapper)

    _logger.info("Wrote %d detail page(s) to %s", len(reports), site_dir)
