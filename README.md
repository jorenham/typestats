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
  <code>typestats</code> computes type-coverage metrics &mdash; the percentage
  of public symbols that carry meaningful type annotations &mdash;
  so maintainers and contributors can prioritize effort where it matters most.
</p>

<p align="center">
  <em>
    Visit the dashboard at
    <a href="https://jorenham.github.io/typestats/dashboard/">jorenham.github.io/typestats</a>.
  </em>
</p>

---

## Quick start

Check the type-annotation coverage of any installed package:

```bash
$ typestats check scipy-stubs
coverage:   100.00%
typable:    13589
typed:      13554
any:           35
```

## Contributing

See [CONTRIBUTING.md](https://github.com/jorenham/typestats/blob/main/CONTRIBUTING.md) for development setup and workflow.
