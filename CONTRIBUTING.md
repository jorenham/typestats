# Contributing to typestats

## Ways to contribute

- **Adding a package to the dashboard:**
  To be included in the [dashboard](https://jorenham.github.io/typestats/), a package must
  be in the top 15,000 most-downloaded packages on PyPI over the last 30 days.
  You can check the current rankings at <https://hugovk.github.io/top-pypi-packages/>.
  Add the package to `packages/typestats-site/projects.toml`.
- **Reporting bugs:**
  If you find incorrect coverage numbers, missing symbols, or crashes, please
  [open an issue](https://github.com/jorenham/typestats/issues/new) with the package name
  and a short description of what went wrong.
- **Improving type coverage upstream:**
  The dashboard highlights packages with low type-annotation coverage. One of the most
  impactful ways to contribute is to submit type-annotation improvements to those upstream
  projects directly.
- **Improving the documentation:**
  Improvements to the README, this contributing guide, or the
  [documentation site](https://jorenham.github.io/typestats/) are welcome.
  Typo fixes, clearer explanations, and new examples all help.
- **Spreading the word:**
  Star the repo, share the [dashboard](https://jorenham.github.io/typestats/) with fellow
  Python developers, or mention it in blog posts, talks, and social media.

## Pull request guidelines

Pull requests are always welcome. When submitting one, please check the following:

- **Scope:**
  Keep your PR focused on a single, well-defined change. Avoid mixing unrelated refactors,
  formatting fixes, or feature additions into the same PR. If you notice something else
  that needs fixing, open a separate issue or PR for it.
- **Tests:**
  New features and bug fixes must include tests. See [Development](#development) for how
  to run the test suite and linters locally.
- **Licensing:**
  The contributed code will be licensed under the project's
  [license](https://github.com/jorenham/typestats/blob/main/LICENSE).

### AI policy

Use of AI coding assistants is permitted as a productivity aid, but should be done
responsibly and transparently:

- **You must understand every line of code you submit.** If you cannot explain a change when
  asked, it will be reverted.
- **AI usage must be disclosed** in the PR description.
- **Fully AI-generated PRs are not allowed** and will be closed without review.
- **Automated or agentic AI communication is not allowed.** PR descriptions, commit messages,
  review responses, and comments must be written by you, not by an AI. We want to interact with
  *you*, not a language model.

## Development

To set up a development environment (using [uv](https://github.com/astral-sh/uv)), run:

```bash
uv sync
```

In CI we currently run [ruff](https://github.com/astral-sh/ruff),
[dprint](https://github.com/dprint/dprint), [pyrefly](https://github.com/facebook/pyrefly), and
[pytest](https://github.com/pytest-dev/pytest).
See the
[CI workflow](https://github.com/jorenham/typestats/blob/main/.github/workflows/ci.yml)
for the exact commands.

### Lefthook

You can optionally install and enable
[lefthook](https://github.com/evilmartians/lefthook), a modern Git hook manager, to
automatically lint and format before each commit:

```bash
uv tool install lefthook --upgrade
uvx lefthook install
uvx lefthook validate
```

For alternative ways of installing lefthook, see
<https://github.com/evilmartians/lefthook#install>

### just

Common tasks are defined in the [justfile](justfile) and can be run with
[just](https://github.com/casey/just).

### Previewing the documentation locally

`packages/typestats-site/scripts/preview.py` provides a live-reloading preview of the
documentation site:

```bash
uv run packages/typestats-site/scripts/preview.py
```

On first run it extracts report data from `origin/data`, builds the `_site/` pages, and
starts `zensical serve`. Subsequent runs reuse cached data if the `origin/data` SHA is
unchanged.

While running, changes to `packages/typestats-site/docs/`,
`packages/typestats-site/src/typestats_site/templates/`, `dashboard.py`, and
`projects.toml` trigger an automatic rebuild. Changes to `dashboard.py` are hot-reloaded;
other Python source changes require a restart.

Pass `--clean` to force a fresh extraction regardless of the cached SHA.
