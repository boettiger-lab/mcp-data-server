# Contributing to MCP Data Server

Thanks for your interest in improving the MCP Data Server! This project is an
open [Model Context Protocol](https://modelcontextprotocol.io/) server that
connects AI agents to cloud-native data. Contributions of all kinds are welcome
— code, datasets, query-guidance improvements, and documentation.

By participating in this project you agree to abide by our
[Code of Conduct](CODE_OF_CONDUCT.md).

## Where to start

Good first areas to contribute:

- **Additional dataset integrations** — extending the STAC catalog
- **Query optimization patterns** — the guidance injected at call time
- **STAC catalog enhancements**
- **Documentation improvements**

Browse [open issues](https://github.com/boettiger-lab/mcp-data-server/issues) for
concrete tasks, or open a [Discussion](https://github.com/boettiger-lab/mcp-data-server/discussions)
to propose an idea or ask a question before writing code.

## Development setup

The server is a Python application. Clone the repo and install dependencies:

```bash
git clone https://github.com/boettiger-lab/mcp-data-server
cd mcp-data-server
pip install -r requirements.txt
```

Run the server locally:

```bash
python server.py
```

## Running tests

Tests use `pytest` and live in the [`tests/`](tests/) directory (see
[`tests/README.md`](tests/README.md) for details):

```bash
pip install pytest
pytest tests/          # run all tests
pytest tests/ -v       # verbose
```

Please add or update tests for any behavior you change. CI runs the suite on
every pull request (`.github/workflows/test.yml`).

## Documentation

End-user docs are a [VitePress](https://vitepress.dev/) site under
[`docs/`](docs/), published to
<https://boettiger-lab.github.io/mcp-data-server/>. To preview locally:

```bash
npm install
npm run docs:dev
```

## Pull request process

1. Fork the repo and create a topic branch off `main`.
2. Make your change, keeping it focused; match the style of the surrounding
   code.
3. Add or update tests and documentation as appropriate.
4. Run `pytest tests/` and make sure everything passes.
5. Open a pull request with a clear description of the change and the motivation
   behind it. Link any related issue.

Maintainers will review and may request changes. Once approved, a maintainer
will merge.

## Reporting bugs and asking questions

- **Bugs / feature requests:** open a
  [GitHub Issue](https://github.com/boettiger-lab/mcp-data-server/issues).
- **Questions / ideas:** start a
  [GitHub Discussion](https://github.com/boettiger-lab/mcp-data-server/discussions).
- **Dataset questions:** use the `browse_stac_catalog` tool or browse the
  [public STAC catalog](https://s3-west.nrp-nautilus.io/public-data/stac/catalog.json).

## License

By contributing, you agree that your contributions will be licensed under the
[BSD 3-Clause License](LICENSE) that covers this project.
