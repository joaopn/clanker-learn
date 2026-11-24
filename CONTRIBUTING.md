# Contributing

## What you can contribute

- Bug reports and fixes
- Improvements to the page, the tutor and the sandboxes
- Documentation updates
- Tests and developer experience improvements

Before a larger change, [open an issue](https://github.com/joaopn/clanker-learn/issues) to talk it over.

## Where things are

| Path | Main folders | Start with |
|---|---|---|
| Docs | `docs/guide`, `docs/architecture` | Edit the Markdown pages |
| The outer container, sandboxes, broker | `outer`, `sandbox`, `modes`, `compose.yaml`, `setup` | `docker compose up` |
| The web server and the page | `web` (`web/static` is the page) | `docker compose up` |
| The tutor service | `tutor` | `docker compose up` |

Installing, configuration and the settings are documented in [`docs/guide/getting-started.md`](docs/guide/getting-started.md) and [`docs/guide/configuration.md`](docs/guide/configuration.md). How clanker-learn works inside is in [`docs/architecture/`](docs/architecture/index.md).

## Local set-up

Run it from your copy of the repository, as a user would:

```
./setup
docker compose up
```

`docker compose up -d` after a change rebuilds the image and makes the container again; with nothing changed, the build is cached and the running container is left alone.

## Tests

From the repository's root:

```
python3 -m venv .venv && .venv/bin/pip install pytest ruff
.venv/bin/pytest tests
.venv/bin/ruff check .
```

The tests marked `integration` start real containers and need a Docker daemon that can run a privileged one. They publish no port on the machine. Every container, network and named volume they create starts with `cl-test-` and is removed afterwards, each container together with the unnamed volumes the image gives it; the images they build, `cl-outer:test` and `cl-sandbox:<hash>`, are kept. One test lets clanker-learn build the sandbox image itself and takes a few minutes.

## Editing the docs

`docs/` is a just-the-docs site that GitHub Pages builds with its own Jekyll and publishes whenever `docs/` changes on `main`. The user guide is `docs/guide/`, how it works is `docs/architecture/`. Every page starts with front matter giving its `title`, `parent` (`User guide` or `How it works`) and `nav_order`, which place it in the sidebar. Link pages with relative `.md` links, draw diagrams as Mermaid blocks, and wrap any `{{`…`}}` text in `{% raw %}` … `{% endraw %}`. Write one paragraph per line, and describe what the app does today: if a change to the app changes what a page says, change the page in the same pull request.

## How to make a change

1. Create a branch from `main`.
2. Keep the pull request focused on one change or one problem.
3. Run the tests and ruff.
4. If the change is visible to the learner, update the guide; if it changes how a part works, update How it works.
5. Mention any trade-offs or follow-up work in the pull request's description.
