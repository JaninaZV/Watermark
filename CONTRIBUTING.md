# Contributing to Watermark

Thank you for your interest in contributing. Watermark is a methodology-first
measurement tool — contributions that improve auditability, correctness, and
reproducibility are especially welcome.

## Development setup

Requires Python 3.9+.

```bash
git clone <your-fork-url>
cd watermark
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -e ".[gpu,dev]"
watermark --help             # verify CLI
```

Optional: install workload dependencies separately when testing reference
workloads (`torch`, `transformers`, `diffusers`, `sentence-transformers`).

## Running tests

```bash
pip install -e ".[dev]"
python3 -m pytest test_schema_contract.py test_water_profile.py test_dashboard.py test_portfolio.py -q
```

Optional browser-free dashboard check (requires Node.js):

```bash
node scripts/test_kpi_toggle.mjs path/to/dashboard.html
```

## Proposing changes

1. Open an issue for non-trivial changes (methodology revisions, new region
   profiles, breaking CLI changes) before starting large work.
2. Fork the repo and create a feature branch from `main`.
3. Keep PRs focused — one logical change per pull request.
4. Include tests when changing aggregation logic, dashboard payload shape,
   or portfolio discovery behavior.
5. Update `METHODOLOGY.md` when changing how numbers are computed or tagged.
6. Update `SCHEMA.md` when changing `summary.json` or portfolio payload shape.
7. Open a pull request against `main` with a clear description and test plan.

## Code style

- **Python:** follow [Black](https://black.readthedocs.io/) formatting
  (88-character line length). Match existing naming and module layout.
- **Comments:** explain non-obvious business logic only; prefer self-explanatory code.
- **CLI flags:** use kebab-case in argparse; store snake_case keys in JSON output.
- **Source tags:** every modeled or measured value must carry an explicit source tag
  in `summary.json` — do not introduce silent defaults.

## Schema and artifact contracts

`summary.json` and the portfolio HTML payload are the public integration
surfaces — not HTTP endpoints. See [`SCHEMA.md`](./SCHEMA.md) for field
definitions, source-tag enums, and versioning rules.

**Run artifact (`summary.json`)**

- Current version: `schema_version: "0.2"` (WWL, water accounting, seasonal stress, cooling type)
- **Minor bump** (`0.2` → `0.3`): new optional fields, new enum values, new
  caveat codes — existing parsers must keep working
- **Major bump** (`1.0`): renamed/removed fields, changed units, changed carbon
  energy basis
- Missing `schema_version` in a file must be treated as `"0.1"` with a warning
  (legacy runs; see SCHEMA.md)

**Portfolio payload (`0.1-portfolio`)**

- Independent semver from the run artifact — bump portfolio version when
  portfolio-specific fields change, not automatically when run schema changes

**Proposing schema changes**

1. Update `SCHEMA.md` first (or in the same PR as the code change).
2. Add or extend tests in `test_schema_contract.py`.
3. Caveats must be structured objects `{code, severity, message}` — not bare
   strings.
4. Water provenance extensions must use the boundary enum: `facility`, `grid`,
   or `region` (see SCHEMA.md design section).

Contract validation helpers live in `schema_contract.py`.

## Welcome contributions

- Bug reports with reproduction steps and environment details
- Methodology improvements with citations and test coverage
- New region or hardware SKU profiles (with documented sources)
- Reference workload scripts (`generate_*.py`) for reproducible experiments
- Dashboard and portfolio template fixes (accessibility, correctness, export)
- Documentation clarifications in `README.md` and `METHODOLOGY.md`

## Out of scope (without prior discussion)

- Large UI redesigns or rebranding without an open design issue
- Refactors that do not fix a bug or enable a planned feature
- Hosted SaaS / multi-tenant product features (v0.1 is CLI + static HTML export)
- Per-process eBPF attribution (planned for v0.3 — discuss in an issue first)

## License

By contributing, you agree that your contributions will be licensed under the
Apache License 2.0. See [LICENSE](./LICENSE).
