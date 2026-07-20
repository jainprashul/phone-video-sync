"""phone-video-sync: ADB phone video discovery, NVENC HEVC compression, and sync.

Package layout:
  cli       — Typer entry points (phone-sync)
  pipeline/ — orchestration (discover → report → process)
  adb/      — device listing, pull/push, MediaStore
  report/   — scan breakdown, recommendations, export
  ffmpeg    — ffprobe + NVENC encode
  db        — SQLite tracking and resume
"""

__version__ = "0.1.0"
