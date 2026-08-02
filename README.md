# harness-map

A read-only inventory of a Claude Code harness: what is configured to load, what can be invoked on demand, and what enforces rules — rendered as a report, an offline HTML map, and an optional live dashboard.

It maps a harness. It never modifies one.

## What it produces

| Stage | Tool | Output |
|---|---|---|
| Collect | `collector.py` | Deterministic, schema-pinned JSON sidecar |
| Synthesize | a model pass | 6-section Markdown report + synthesis sidecar |
| Render | `render_html.py` | Self-contained offline HTML, no network |
| Serve | `serve.py` | Loopback-only dashboard with live refresh |

The collector is a plain script — no model, no agent, no network. Only the synthesis step needs a model.

## Requirements

Python **3.10+**. Nothing else. No pip install, no virtualenv, no dependencies — the runtime is stdlib-only by design, and that is enforced by the test suite rather than merely intended.

## Install

```sh
git clone https://github.com/cmillstead/harness-map.git ~/src/harness-map
ln -s ~/src/harness-map ~/.claude/skills/harness-map
```

The symlink makes it available as a skill. Nothing is copied into your harness and nothing is written there.

## Use

```sh
OUT_DIR="$HOME/harness-map-reports"
mkdir -p "$OUT_DIR"
python3 ~/src/harness-map/collector.py \
  --root ~/.claude --project-root ~/.claude \
  --out "$OUT_DIR/harness-map-$(date +%F).json"
```

`--out` must resolve **outside** `--root`; the collector rejects it otherwise. Reuse the same `OUT_DIR` across runs — the run-over-run diff and the trend sparklines only accrue history when you do.

Render a static map, or serve it live:

```sh
python3 ~/src/harness-map/render_html.py --out-dir "$OUT_DIR"
python3 ~/src/harness-map/serve.py --out-dir "$OUT_DIR" --root ~/.claude --project-root ~/.claude
```

`serve.py` binds `127.0.0.1` only and picks an OS-assigned port, printing the URL on startup. It is a long-running process; it does not return until you stop it.

### Compose mode

Add `--compose --project-root <repo>` to map an operator harness composed with a project's, the way a session actually loads them — including which project skills are shadowed by operator ones and therefore never run.

In compose mode the project tier is treated as **untrusted**: a project symlink whose real path escapes the project root is recorded, never read.

## What it reads, and what it will not do

Point `--root` at a harness and it reads instruction files, skills, agents, hooks, settings, and telemetry logs under that root, plus git metadata for file-age signals. It reads `.md`, `.py`, and `.sh`.

Three properties hold by construction, not by convention:

- **Zero writes inside `--root`.** The only outputs are the files in your chosen out-dir.
- **Scanned bytes are data, never instructions.** Python is parsed with `ast`, never imported or executed, and text found in a scanned file is never followed as a directive.
- **Secrets do not serialize.** Configuration surfaces environment variable *names* only; MCP `env` and `headers` values are never emitted.

It reports what is configured, not whether it is good. Judgments about maturity and drag are left to a human reading the report.

## Development

```sh
./check.sh
```

Runs ruff, mypy, and the full pytest suite. Green is the bar.

Two smoke tests exercise a real collector sidecar and skip unless you point them at one:

```sh
export HARNESS_MAP_REAL_SAMPLE=/path/to/harness-map-YYYY-MM-DD.json
```

`CLAUDE.md` in this repo is contributor guidance and references a spec set maintained privately; it is not needed to run or develop against the tool.

## License

MIT — see [LICENSE](LICENSE).
