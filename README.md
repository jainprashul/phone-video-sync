# phone-video-sync

Plug in a USB phone, run `phone-sync process`, and sync compressed HEVC copies of new videos back to the device.

The tool detects an authorized ADB device, recursively discovers videos under `/sdcard`, tracks them in SQLite, compresses new ones with parallel NVENC HEVC (metadata preserved), verifies integrity, pushes compressed copies, and archives or deletes originals only after a passing check.

## Requirements

- Python 3.11+
- [ADB](https://developer.android.com/tools/adb) on `PATH` (or set `adb_path` in config)
- [FFmpeg](https://ffmpeg.org/) / ffprobe on `PATH` (or set paths in config)
- NVIDIA GPU with `hevc_nvenc` for the default encoder (or set `video_encoder` to a software encoder)

## Install

```bash
cd phone-video-sync
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
# source .venv/bin/activate

pip install -e ".[dev]"
cp config.yaml.example config.yaml
```

## Usage

```bash
# Discover videos — folder + size report + recommendation (uses cache)
phone-sync scan

# Force a fresh phone search (ignore listing cache)
phone-sync scan --refresh

# Interactively pick with radio + checkboxes (optionally process)
phone-sync scan --select

# Compress only the recommended high-value set
phone-sync process --recommend --yes

# Or filter by folder / size
phone-sync process --folder /storage/emulated/0/DCIM/Camera --min-size 100MB
phone-sync process --select

# Compress, verify, push, archive/delete (prompts unless --yes)
phone-sync process
phone-sync process --yes
phone-sync process --limit 1

# Watch for USB connect and auto-process
phone-sync watch
phone-sync watch --once --limit 1

# Verify compressed outputs still exist on the phone
phone-sync verify

# Clear local work dirs; optionally purge on-phone archive
phone-sync clean
phone-sync clean --archive --yes

# Detailed tracking statistics
phone-sync stats

# Config
phone-sync config show
phone-sync config validate
```

Aliases (still supported): `dry-run` → `scan`, `run` → `process`, `status` (compact), `purge-archive`.

You can also run as a module:

```bash
python -m phone_video_sync scan
```

## How it works

1. Detect a single authorized ADB device.
2. List videos under `/sdcard` (skips `Android/` and the archive folder).
3. Upsert into SQLite; reconcile stuck in-progress rows for resume.
4. Show a plan summary and confirm (unless `--yes`).
5. Per video: pull → probe → encode → verify → push `<name>_hevc.mp4` → archive/delete original.
6. Report done / failed / skipped counts and bytes saved.

Progress survives restarts via SQLite. Failures keep the phone original and retry up to `max_attempts`.

## Configuration

See [`config.yaml.example`](config.yaml.example). Key options:

| Key | Default | Meaning |
|-----|---------|---------|
| `delete_mode` | `archive` | `archive` moves originals to `/sdcard/.compressed_archive/...`; `delete` removes them after verify |
| `encode_workers` | `2` | Parallel NVENC encode workers |
| `cq` / `preset` | `28` / `p5` | NVENC quality / preset |
| `video_encoder` | `hevc_nvenc` | Override if NVENC is unavailable |
| `watch_interval_sec` | `5` | Poll interval for `phone-sync watch` |

## License

MIT — see [LICENSE](LICENSE).
