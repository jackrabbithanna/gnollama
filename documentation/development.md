# Development

How to build Tureen natively, run its checks, and understand how it is put together. For the quickest way to build and run it, see [Build](../README.md#build) in the README.

## Native build with Meson

### Requirements

- Python 3.11+ and PyGObject
- GTK 4.18+, libadwaita 1.9+, libsoup 3
- libsecret 0.20+ with Secret 1 introspection
- jsonschema 4.26+, pypdf, and Markdown
- sqlite-vec 0.1.9, and SQLite 3.41+ with extension loading
- [GTKSourceView](https://wiki.gnome.org/Projects/GtkSourceView) 5, for code highlighting
- [ziamath](https://ziamath.readthedocs.io) (in `requirements.lock`), with the librsvg introspection data (Rsvg 2.0) and pycairo, for math rendering

Without ziamath, display equations are shown as TeX source and simple inline equations as plain text. Without librsvg or pycairo, equations are drawn through GTK's image loader, which can fail inside a sandbox.

The Flatpak manifest uses GNOME 51 and builds the current checkout. It bundles checksum-pinned dependencies for aarch64 and x86_64, including sqlite-vec 0.1.9 and pypdf 6.18.0. librsvg and pycairo come from the GNOME runtime.

### Building

On a Debian/Ubuntu installation whose GTK and libadwaita packages meet the minimum versions above, install the development and introspection packages, then the locked Python dependencies:

```bash
sudo apt-get install meson ninja-build gettext desktop-file-utils python3-venv python3-gi \
  libgtk-4-dev libadwaita-1-dev libsoup-3.0-dev libsecret-1-dev \
  gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-soup-3.0 gir1.2-secret-1 \
  libgtksourceview-5-dev gir1.2-gtksource-5 gir1.2-rsvg-2.0 python3-gi-cairo
python3 -m venv --system-site-packages .venv
. .venv/bin/activate
python -m pip install -r requirements.lock
```

Then build and install:

```bash
meson setup build
meson compile -C build
meson install -C build
```

Run `tureen` to start the application.

## Validation

Run `meson test -C build --print-errorlogs` after building.

- Regression tests use temporary databases and a local HTTP fixture server; they never contact your model servers.
- GTK tests require a display. Set `TUREEN_REQUIRE_DISPLAY=1` to reject missing GTK coverage, as CI does. `tools/ci-test.sh` runs the complete suite under Xvfb.
- CI also runs `ruff check .` and `mypy`, configured in `pyproject.toml`, and fails on any finding. Install the versions pinned in `.github/workflows/validation.yml` to run them locally.
- The CI matrix builds GNOME 51 Flatpaks on x86_64 and aarch64 and runs core compatibility tests on Python 3.11.

GNOME Builder builds the repository Flatpak manifest from the current checkout. For a command-line Flatpak build without installing, use:

```bash
flatpak-builder --user --force-clean /tmp/tureen-flatpak-build io.github.jackrabbithanna.Tureen.json
```

Translations have their own checks; see the [translation guide](../po/README.md).

## Architecture

`Services` owns four inference workers, two control/read workers, two discovery workers, and two transfer workers.

- Model lists and selected-model capabilities use separate shared requests with a 60-second cache and digest-based capability identities. Subscribers do not occupy workers while waiting for another subscriber's request. Empty lists are not cached, and interacting with an empty selector refreshes discovery.
- Knowledge processing keeps its own bounded queues.
- An ordered writer serializes retryable transactions on one long-lived SQLite connection, and each worker thread keeps its own read connection. Submitted prompts consume only their matching draft revision.
- Message writes append or update changed records and keep unchanged attachment rows.
- History and library lists load in pages of 100, and conversation messages in pages of 50. Streaming updates are coalesced to at most 20 per second.

## Retrieval performance

Knowledge search is exact, using sqlite-vec. Cosine uses vec0 nearest-neighbor search; Euclidean and Manhattan use sqlite-vec's native distance functions over the selected vectors.

For the pinned sqlite-vec 0.1.9 layout, distance searches reuse read-only vector block handles, validate slot mappings, and keep only the best candidates in memory. Other extension versions fall back to the public scalar-query interface.

Benchmarks on GNOME 50, aarch64, excluding embedding time on the host:

- **10,000 vectors at 1,024 dimensions** matched reference rankings, taking about 0.04 seconds for cosine, 0.19 seconds for Euclidean, and 0.21 seconds for Manhattan.
- **50,000 vectors at 1,024 dimensions** returned the same top six results as the earlier batched NumPy ranking. Whole-library retrieval took 0.15–0.19 seconds, selecting one 25,000-vector document took 0.17–0.18 seconds, and selecting 25,000 individual chunks took 0.42–0.44 seconds.

## Evaluation tools

- The **performance fixture** creates and removes its own database of 10,000 conversations, 10,000 documents, a 1,000-message conversation with attachments, and 50,000 vectors. It asserts bounded page reads and writes proportional to changed messages. Its timings are machine-dependent comparisons, not performance guarantees.
- The **retrieval fixture** ranks a versioned corpus of answerable, unanswerable, follow-up, misleading-source, and untrusted-document cases with recorded embeddings. It checks the stored-vector retrieval path, not the quality of a live model.
- The **layout review** renders real GTK widgets from fixture data with mocked responses. Add `--language ar` for right-to-left captures.

Run them in the configured Python/SDK environment:

```bash
python3 tools/benchmark_workspace.py --output workspace-benchmark.json
python3 tools/evaluate_retrieval.py --output retrieval-evaluation.json
python3 tools/review_layout.py --resource build/src/tureen.gresource --output validation-screenshots
```

Live retrieval evaluation is opt-in and contacts only the host and models you supply. It records model digests, ranked passages, recall@6, reciprocal rank, and optional generated answers for human review of correctness and citation support:

```bash
python3 tools/evaluate_retrieval.py --live-host http://localhost:11434 \
  --embedding-model embeddinggemma --answer-model qwen3:4b --output live-evaluation.json
```
