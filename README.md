<h1 align="center">typestats</h1>

<p align="center">
  <strong>Type annotation coverage statistics for Python packages</strong>
</p>

<p align="center">
  <a href="https://github.com/jorenham/typestats"><img alt="GitHub License" src="https://img.shields.io/github/license/jorenham/typestats?style=flat-square&color=121d2f&labelColor=3d444d"></a>
  <a href="https://pypi.org/project/typestats"><img alt="PyPI Version" src="https://img.shields.io/pypi/v/typestats?style=flat-square&color=121d2f&labelColor=3d444d"></a>
  <a href="https://github.com/jorenham/typestats"><img alt="Python Versions" src="https://img.shields.io/pypi/pyversions/typestats?style=flat-square&color=121d2f&labelColor=3d444d"></a>
</p>

<p align="center">
  <a href="https://jorenham.github.io/typestats/dashboard/"><strong>Dashboard</strong></a>
  &middot;
  <a href="https://jorenham.github.io/typestats/guides/"><strong>Guides</strong></a>
  &middot;
  <a href="https://jorenham.github.io/typestats/reference/"><strong>Reference</strong></a>
  &middot;
  <a href="https://github.com/jorenham/typestats/blob/main/CONTRIBUTING.md"><strong>Contributing</strong></a>
</p>

<p align="center">
  <code>typestats</code> computes type-coverage metrics so maintainers and contributors can prioritize effort where it matters most.
</p>

<p align="center">
  <em>
    Visit the dashboard at
    <a href="https://jorenham.github.io/typestats/dashboard/">jorenham.github.io/typestats</a>.
  </em>
</p>

---

## Quick start

> [!WARNING]
> The `typestats` CLI is deprecated; use [`pyrefly coverage`](https://pyrefly.org/)
> `check`/`report` instead.

Check a package's public type-annotation coverage by pointing `pyrefly coverage check` at
its source, or omit the path to check the current project (pyrefly finds the nearest
config):

```bash
$ pyrefly coverage check --public-only src/yourpackage
 INFO type coverage 100.00% (13589 of 13589 typable)
```

## Contributing

See [CONTRIBUTING.md](https://github.com/jorenham/typestats/blob/main/CONTRIBUTING.md) for development setup and workflow.
