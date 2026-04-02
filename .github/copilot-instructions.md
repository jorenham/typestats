# Copilot Instructions for `typestats`

## Style

- **ASCII only**: all source files must contain only ASCII characters. Do not use emdashes or
  other non-ASCII punctuation.
- **Docstrings**: use Markdown formatting in docstrings. Do not use double backticks for inline
  code; prefer single backticks instead.
- **Comments**: avoid obvious and wordy code comments. If the code is clear, no comment is
  needed.

## Quality

- **Linting**: `ruff` must report zero errors and warnings.
- **Type-checking**: `pyrefly` must report zero errors and warnings.

## Contributing

- **Tests**: new features must include tests.
- **Documentation**: non-obvious design choices should be documented in the docs.
- **Uncertainty**: do not guess. If something is unclear, ask the user for clarification.
