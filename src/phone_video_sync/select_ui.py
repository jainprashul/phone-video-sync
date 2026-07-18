"""Interactive selection UI (radio + checkboxes) via questionary."""

from __future__ import annotations

from phone_video_sync.models import VideoRecord
from phone_video_sync.report import (
    ScanBreakdown,
    choice_label,
    format_bytes,
)


def _dedupe(records: list[VideoRecord]) -> list[VideoRecord]:
    seen: set[str] = set()
    out: list[VideoRecord] = []
    for rec in records:
        if rec.remote_path in seen:
            continue
        seen.add(rec.remote_path)
        out.append(rec)
    return out


def interactive_select(breakdown: ScanBreakdown) -> list[VideoRecord] | None:
    """Radio + checkbox flow. Returns None if cancelled."""
    try:
        import questionary
        from questionary import Choice, Style
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "questionary is required for interactive select. "
            "Install with: pip install questionary"
        ) from exc

    if not breakdown.pending:
        return []

    style = Style(
        [
            ("qmark", "fg:cyan bold"),
            ("question", "bold"),
            ("answer", "fg:cyan"),
            ("pointer", "fg:cyan bold"),
            ("highlighted", "fg:cyan bold"),
            ("selected", "fg:green"),
            ("separator", "fg:black"),
            ("instruction", "fg:#888888"),
        ]
    )

    rec_n = len(breakdown.recommended)
    rec_b = format_bytes(sum(r.size for r in breakdown.recommended))
    all_n = len(breakdown.pending)
    all_b = format_bytes(sum(r.size for r in breakdown.pending))

    mode = questionary.select(
        "What do you want to process?",
        choices=[
            Choice(
                title=f"★ Recommended  ({rec_n} files, {rec_b}) — best savings",
                value="recommended",
            ),
            Choice(
                title=f"All pending    ({all_n} files, {all_b})",
                value="all",
            ),
            Choice(title="Folders…       (checkbox multi-select)", value="folders"),
            Choice(title="Size buckets…  (checkbox multi-select)", value="sizes"),
            Choice(
                title="Individual files… (checkbox; recommended / largest first)",
                value="files",
            ),
            Choice(title="Cancel", value="cancel"),
        ],
        style=style,
        use_indicator=True,
        use_shortcuts=True,
        instruction="(↑↓ move, enter select)",
    ).ask()

    if mode is None or mode == "cancel":
        return None
    if mode == "recommended":
        return list(breakdown.recommended)
    if mode == "all":
        return list(breakdown.pending)

    if mode == "folders":
        if not breakdown.by_folder:
            return []
        choices = [
            Choice(
                title=(
                    f"{g.key}  —  {g.count} files, {format_bytes(g.bytes)} "
                    f"(est. save {format_bytes(g.est_savings)})"
                ),
                value=g.key,
                checked=(i < 3),  # pre-check top 3 by size
            )
            for i, g in enumerate(breakdown.by_folder[:40])
        ]
        picked = questionary.checkbox(
            "Select folder(s)  [space toggle, enter confirm]",
            choices=choices,
            style=style,
            instruction="(space toggle, a all, i invert, enter confirm)",
        ).ask()
        if picked is None:
            return None
        if not picked:
            return []
        selected: list[VideoRecord] = []
        by_key = {g.key: g for g in breakdown.by_folder}
        for key in picked:
            selected.extend(by_key[key].records)
        return _refine_files(breakdown, _dedupe(selected), style)

    if mode == "sizes":
        if not breakdown.by_size:
            return []
        choices = [
            Choice(
                title=(
                    f"{g.key}  —  {g.count} files, {format_bytes(g.bytes)} "
                    f"(est. save {format_bytes(g.est_savings)})"
                ),
                value=g.key,
                checked=("medium" in g.key or "large" in g.key or "huge" in g.key),
            )
            for g in breakdown.by_size
        ]
        picked = questionary.checkbox(
            "Select size bucket(s)  [space toggle, enter confirm]",
            choices=choices,
            style=style,
            instruction="(space toggle, a all, i invert, enter confirm)",
        ).ask()
        if picked is None:
            return None
        if not picked:
            return []
        selected = []
        by_key = {g.key: g for g in breakdown.by_size}
        for key in picked:
            selected.extend(by_key[key].records)
        return _refine_files(breakdown, _dedupe(selected), style)

    if mode == "files":
        # Prefer recommended; fill with largest pending up to 80 choices
        pool = list(breakdown.recommended)
        if len(pool) < 80:
            extra = sorted(breakdown.pending, key=lambda r: r.size, reverse=True)
            seen = {r.remote_path for r in pool}
            for rec in extra:
                if rec.remote_path in seen:
                    continue
                pool.append(rec)
                seen.add(rec.remote_path)
                if len(pool) >= 80:
                    break
        return _checkbox_files(breakdown, pool, style, precheck_recommended=True)

    return None


def _refine_files(
    breakdown: ScanBreakdown,
    selected: list[VideoRecord],
    style,
) -> list[VideoRecord] | None:
    """Optional second step: checkbox refine when selection isn't huge."""
    import questionary
    from questionary import Choice

    if not selected:
        return []
    if len(selected) > 80:
        confirm = questionary.confirm(
            f"Process all {len(selected)} selected files "
            f"({format_bytes(sum(r.size for r in selected))})?",
            default=True,
            style=style,
        ).ask()
        if confirm is None or not confirm:
            return None
        return selected

    refine = questionary.confirm(
        f"{len(selected)} files selected — refine with checkboxes?",
        default=False,
        style=style,
    ).ask()
    if refine is None:
        return None
    if not refine:
        return selected
    return _checkbox_files(breakdown, selected, style, precheck_recommended=False)


def _checkbox_files(
    breakdown: ScanBreakdown,
    pool: list[VideoRecord],
    style,
    *,
    precheck_recommended: bool,
) -> list[VideoRecord] | None:
    import questionary
    from questionary import Choice

    choices = []
    for rec in pool:
        meta = breakdown.metas.get(rec.remote_path)
        title = choice_label(meta) if meta else rec.remote_path
        checked = precheck_recommended and bool(meta and meta.recommended)
        if not precheck_recommended:
            checked = True
        choices.append(Choice(title=title, value=rec.remote_path, checked=checked))

    picked = questionary.checkbox(
        "Select file(s)  ★ = recommended   [space toggle, enter confirm]",
        choices=choices,
        style=style,
        instruction="(space toggle, a all, i invert, enter confirm)",
    ).ask()
    if picked is None:
        return None
    by_path = {r.remote_path: r for r in pool}
    return [by_path[p] for p in picked if p in by_path]
