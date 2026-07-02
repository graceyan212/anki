# GMAT Focus Edition Study Tool (Desktop)

A spaced-repetition study tool for the **GMAT Focus Edition** exam, built as a
fork of [Anki](https://apps.ankiweb.net). This repository contains the source
code for the **desktop** application (macOS / Linux / Windows).

## About

This project adapts Anki's proven spaced-repetition engine for preparing for
the GMAT Focus Edition. The exam covered by this tool is the **GMAT Focus
Edition**.

It is a fork of Anki by Ankitects Pty Ltd and contributors. See the `NOTICE`
file for attribution and the `LICENSE` file for license terms.

## Architecture

Anki has a multi-layered architecture:

- Core Rust layer in `rslib/`
- Protobuf definitions in `proto/` used by all layers to talk to each other
- Python library wrapping the Rust layer (`pylib/`, with the Rust↔Python
  bridge in `pylib/rsbridge`)
- PyQt GUI that embeds web components (`qt/`)
- Web frontend in Svelte/TypeScript (`ts/`)

## Building and running the desktop app

> All build, run, test, lint and format commands are exposed as recipes in the
> project `justfile`. Run `just --list` to see them.

### Prerequisites

- **rustup** — the build respects `rust-toolchain.toml` (currently Rust
  1.92.0). Install from https://rustup.rs.
- **protoc** — the Protocol Buffers compiler MUST be on your `PATH`. On macOS:
  `brew install protobuf`. This is the most common build blocker.
- **Ninja 1.10+** (or the bundled `n2`; run `tools/install-n2` if needed).
- **Node.js**.
- **Python 3.10+**.
- The repository path must contain **no spaces**.

The first clean build downloads dependencies and compiles the Rust core; it can
take anywhere from ~20 minutes to a couple of hours depending on the machine.
Subsequent builds are incremental and fast.

### Build and run (development mode)

```
just run
```

This builds `pylib` and `qt`, then launches the app with debugging enabled. Web
views are served at `http://localhost:40000/_anki/pages/`.

For a release-optimized build:

```
just run-optimized
```

### Just build (no launch)

```
just build
```

### Checks and tests

```
just check       # format + full build + lint + tests
just test-rust   # Rust tests
just test-py     # Python tests
just test-ts     # TypeScript/Svelte tests
```

### Building an installer

The Briefcase-based installer code lives in `qt/installer`, with per-platform
templates. To build a desktop installer:

```
tools/build-installer
```

On macOS this produces a `.dmg` under `out/installer/dist/`.

## iOS app

> _Placeholder — the iOS application is built and maintained separately and is
> not part of this desktop repository. Build and usage instructions for the iOS
> client will be documented here once available._

## License

This project is licensed under the GNU Affero General Public License, version 3
or later (AGPL-3.0-or-later). See [LICENSE](./LICENSE) for the full text and
[NOTICE](./NOTICE) for attribution and third-party component licenses.

Portions contributed by Anki users are licensed under the BSD 3-clause license;
see [CONTRIBUTORS](./CONTRIBUTORS).
