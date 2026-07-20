# phone-video-sync

Plug in a USB phone, run `phone-sync process`, and sync compressed HEVC copies of new videos back to the device.

The tool detects an authorized ADB device, recursively discovers videos under `/sdcard`, tracks them in SQLite, compresses new ones with parallel NVENC HEVC (metadata preserved), verifies integrity, pushes compressed copies, and archives or deletes originals only after a passing check.

## Requirements

| Dependency | Notes |
|------------|-------|
| Python 3.11+ | Required for editable/pip install; not needed for the standalone `.exe` |
| [ADB](https://developer.android.com/tools/adb) | On `PATH`, or set `adb_path` in config |
| [FFmpeg](https://ffmpeg.org/) / ffprobe | On `PATH`, or set paths in config |
| NVIDIA GPU | Default encoder is `hevc_nvenc`; set `video_encoder` for software encoding |

Enable **USB debugging** on the phone and authorize the host when prompted. The tool expects exactly one connected, authorized device.

## Install

### Option A — Development (editable)

```powershell
cd phone-video-sync
python -m venv .venv
.\.venv\Scripts\Activate.ps1

pip install -e ".[dev]"
Copy-Item config.yaml.example config.yaml
```

Linux/macOS: use `source .venv/bin/activate` and `cp config.yaml.example config.yaml`.

### Option B — From a wheel

```powershell
pip install phone_video_sync-0.1.0-py3-none-any.whl
```

Wheels are published on [GitHub Releases](https://github.com/jainprashul/phone-video-sync/releases).

### Option C — Standalone Windows executable

Download `phone-sync.exe` from a release, or build locally:

```powershell
.\scripts\build.ps1 -BinaryOnly
# -> dist\phone-sync.exe
```

ADB and FFmpeg are **not** bundled — install them separately and ensure they are on `PATH`.

### Verify installation

```powershell
phone-sync --help
# or, if Scripts is not on PATH:
python -m phone_video_sync --help

phone-sync config validate
```

Global flags (apply to every command):

| Flag | Description |
|------|-------------|
| `-c`, `--config PATH` | Config file (default: `./config.yaml`) |
| `-v`, `--verbose` | Debug logging |

## Quick start

```powershell
# 1. Copy and edit config
Copy-Item config.yaml.example config.yaml
phone-sync config validate

# 2. Scan the phone (report only, uses listing cache)
phone-sync scan

# 3. Process pending videos (prompts for confirmation)
phone-sync process

# 4. Or process the recommended set without prompting
phone-sync process --recommend --yes
```

## Usage

### `scan` — discover and report

Lists videos on the device, groups them by folder and size, probes codecs for recommendations, and saves a report under `logs/`. Does **not** transfer or encode unless `--select` leads to processing.

```powershell
phone-sync scan
phone-sync scan --refresh          # ignore listing cache, rescan phone
phone-sync scan --select           # interactive pick, optional process
```

### `process` — compress and sync

Pull → encode → verify → push → archive/delete original.

```powershell
phone-sync process
phone-sync process --yes                    # skip confirmation
phone-sync process --limit 1                # process at most N videos
phone-sync process --recommend --yes        # recommended set only
phone-sync process --select                 # interactive selection
phone-sync process --folder /storage/emulated/0/DCIM/Camera
phone-sync process --min-size 100MB --max-size 2G
phone-sync process --refresh                # fresh device listing
```

`--folder` is repeatable. Size filters accept suffixes like `100MB`, `1G`, `500K`.

### `watch` — auto-process on USB connect

Polls for an ADB device and runs `process` when one appears.

```powershell
phone-sync watch
phone-sync watch --once --limit 1           # one connect cycle, then exit
phone-sync watch --interval 10              # override poll interval (seconds)
phone-sync watch --prompt                   # confirm before each run
```

### `verify` — check on-phone outputs

Confirms compressed files still exist for videos marked `done` in SQLite.

```powershell
phone-sync verify
phone-sync verify --limit 10
```

### `clean` — local work dirs and archive

```powershell
phone-sync clean                            # clear work/in, work/out, work/failed
phone-sync clean --archive --yes            # also delete on-phone archive folder
phone-sync clean --no-work --archive --yes  # archive only
```

### `stats` / `status`

```powershell
phone-sync stats        # detailed SQLite statistics
phone-sync status       # compact summary table
```

### `config`

```powershell
phone-sync config show
phone-sync config validate    # checks config + adb/ffmpeg/ffprobe availability
```

### Aliases

| Alias | Equivalent |
|-------|--------------|
| `dry-run` | `scan` |
| `run` | `process` |
| `purge-archive` | `clean --no-work --archive` |

### Module invocation

```powershell
python -m phone_video_sync scan
python -m phone_video_sync process --yes
```

## How it works

```
Phone (/sdcard)                    Local machine                      Phone
─────────────────                  ─────────────                      ─────
  list videos  ──ADB──►  SQLite tracking + scan report
  pull file    ◄──────   work/in/<file>
                         ffprobe → encode (NVENC) → work/out/<file>_hevc.mp4
                         verify duration/size
  receive HEVC ────────► push work/out
  archive/delete original (after verify passes)
```

1. **Discover** — connect via ADB, list videos under `remote_root` (skips `Android/` and the archive folder). Listing is cached in SQLite for `listing_cache_ttl_sec`.
2. **Report** — group pending work by folder and size bucket; probe headers/codecs for recommendation scoring; save Markdown + CSV under `logs/`.
3. **Select** (optional) — interactive radio/checkbox UI (`--select`) or CLI filters (`--folder`, `--min-size`, `--recommend`).
4. **Process** — parallel workers (`encode_workers`) each run: pull → probe → encode → verify → push → finalize.
5. **Finalize** — `delete_mode: archive` moves the original on-phone to `.compressed_archive/...`; `delete` removes it. Only after verification passes.
6. **Resume** — every video is tracked in SQLite. Interrupted jobs are reconciled on the next run. Failures retry up to `max_attempts`.

Compressed outputs are written next to the original as `<name>_hevc.mp4` (suffix configurable via `output_suffix`).

## Implementation

### Package layout

```
src/phone_video_sync/
├── cli.py              # Typer CLI (`phone-sync` entry point)
├── config.py           # YAML loading and validation
├── db.py               # SQLite schema, listing cache, resume
├── ffmpeg.py           # ffprobe + NVENC encode
├── models.py           # Config, VideoRecord, VideoStatus
├── select_ui.py        # questionary interactive selection
├── verify.py           # post-encode integrity checks
├── adb/                # device I/O
│   ├── client.py       # AdbClient (list, pull, push, shell)
│   ├── listing.py      # recursive find + MediaStore enrichment
│   ├── transfer.py     # pull/push with progress
│   └── media.py        # MediaStore row parsing
├── pipeline/           # orchestration
│   ├── core.py         # Pipeline class (discover → process → watch)
│   ├── probe.py        # remote header probing for recommendations
│   ├── cache.py        # listing + media-meta cache helpers
│   ├── display.py      # Rich tables for scan reports
│   └── paths.py        # safe local names, output paths
└── report/             # scan breakdown and recommendations
    ├── grouping.py     # folder / size buckets
    ├── recommend.py    # scoring and ranking
    ├── meta.py         # FileMeta from probe + MediaStore
    └── export.py       # Markdown / CSV report export
```

### Core types

**`Pipeline`** (`pipeline/core.py`) is the orchestrator. CLI commands construct a `Pipeline` from config and call methods like `scan()`, `process()`, `watch()`, and `verify()`.

**`VideoStatus`** state machine (persisted per file in SQLite):

`discovered` → `pulling` → `pulled` → `encoding` → `verifying` → `pushing` → `finalizing` → `done`

Failures set `failed` and are retried until `max_attempts`. In-progress rows from a crashed run are reset on startup for resume.

**Caching**

- **Listing cache** — full ADB file list keyed by device serial + scan root; TTL from `listing_cache_ttl_sec`.
- **Media meta cache** — codec/resolution/duration from ffprobe or MediaStore; invalidated when file size or mtime changes.

**Recommendations** — pending videos are scored by size, folder (e.g. DCIM/Camera), codec (H.264/HEVC), and resolution. The top set is surfaced in the scan report and selectable via `--recommend`.

### Local directories

| Path | Purpose |
|------|---------|
| `data/pvsync.db` | SQLite database (configurable) |
| `work/in/` | Pulled originals during processing |
| `work/out/` | Encoded outputs before push |
| `work/failed/` | Copies kept on encode/verify failure |
| `work/probe/` | Temporary probe downloads for scan |
| `logs/` | Run logs and scan report exports |

## Configuration

Copy [`config.yaml.example`](config.yaml.example) to `config.yaml` in the working directory (or pass `-c`).

| Key | Default | Meaning |
|-----|---------|---------|
| `remote_root` | `/sdcard` | On-device scan root |
| `archive_root` | `/sdcard/.compressed_archive` | Archive destination for originals |
| `delete_mode` | `archive` | `archive` or `delete` after verify |
| `video_encoder` | `hevc_nvenc` | FFmpeg video encoder |
| `preset` / `cq` | `p5` / `28` | NVENC preset and constant-quality |
| `encode_workers` | `2` | Parallel encode workers |
| `max_attempts` | `3` | Retries per video on failure |
| `listing_cache_ttl_sec` | `1800` | Listing cache TTL (`0` = always rescan) |
| `watch_interval_sec` | `5` | `watch` poll interval |
| `output_suffix` | `_hevc` | Suffix before `.mp4` on compressed files |
| `adb_path` / `ffmpeg_path` / `ffprobe_path` | `null` | Override tool paths (`null` = auto-detect) |

Run `phone-sync config show` to print the effective configuration.

## Building and releases

### Build locally

```powershell
.\scripts\build.ps1              # wheel + sdist in dist/
.\scripts\build.ps1 -Binary      # wheel + sdist + phone-sync.exe
.\scripts\build.ps1 -BinaryOnly  # standalone exe only
```

### CI / releases

- **CI** (`.github/workflows/ci.yml`) — runs tests on Ubuntu and Windows across Python 3.11–3.13 on every push/PR.
- **Release** (`.github/workflows/release.yml`) — on GitHub release publish, attaches the wheel, sdist, and `phone-sync.exe`.

To cut a release: bump `version` in `pyproject.toml` and `src/phone_video_sync/__init__.py`, push, then create a tagged release on GitHub (e.g. `v0.1.0`).

## Development

```powershell
pip install -e ".[dev]"
python -m pytest -q
```

Tests live under `tests/` and cover config, DB, caching, probe logic, report grouping, and verification.

## License

MIT — see [LICENSE](LICENSE).
