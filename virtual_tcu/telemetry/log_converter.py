"""Replay log conversion helpers for post-drive analysis."""

from __future__ import annotations

import csv
import json
import re
import threading
from collections.abc import Callable
from io import StringIO
from pathlib import Path

from virtual_tcu.replay import format_replay, format_text_line, telemetry_record
from virtual_tcu.telemetry.parser import parse_fh6_packet
from virtual_tcu.telemetry.replay_reader import iter_replay_records


def _replay_stem(path: Path) -> str:
    stem = path.stem
    if stem.endswith(".bin"):
        stem = stem[:-4]
    if stem.startswith("tcu_"):
        stem = stem[4:]
    return stem


def _replay_date(path: Path) -> str | None:
    match = re.search(r"(\d{8})", path.name)
    return match.group(1) if match else None


def _write_csv_file(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def convert_replay_csv_split(path: Path, *, base_name: str | None = None) -> list[Path]:
    """Convert replay to CSV files split into roaming and race segments."""
    path = Path(path)
    base_name = base_name or _replay_stem(path)
    out_dir = path.parent
    roaming_rows: list[dict[str, object]] = []
    race_segments: list[list[dict[str, object]]] = []
    current_race_rows: list[dict[str, object]] = []
    prev_race_on = False
    car_ordinal = 0

    for rel_ms, raw in iter_replay_records(path):
        td = parse_fh6_packet(raw)
        if td is None:
            continue
        if td.car_ordinal and not car_ordinal:
            car_ordinal = td.car_ordinal
        row = telemetry_record(rel_ms, td)
        if td.is_race_on:
            if not prev_race_on:
                current_race_rows = []
            current_race_rows.append(row)
        else:
            if prev_race_on and current_race_rows:
                race_segments.append(current_race_rows)
                current_race_rows = []
            roaming_rows.append(row)
        prev_race_on = bool(td.is_race_on)

    if current_race_rows:
        race_segments.append(current_race_rows)

    generated: list[Path] = []
    if roaming_rows:
        out = out_dir / f"{base_name}_roaming.csv"
        _write_csv_file(out, roaming_rows)
        generated.append(out)
    for index, rows in enumerate(race_segments, start=1):
        seg_car = rows[0].get("car_ordinal", car_ordinal) or car_ordinal
        out = out_dir / f"{base_name}_race{index}_car{seg_car}.csv"
        _write_csv_file(out, rows)
        generated.append(out)
    return generated


def export_decision_log_for_replay(path: Path, *, out_path: Path | None = None) -> Path | None:
    """Copy same-day decision JSONL next to a replay export, when available."""
    path = Path(path)
    date = _replay_date(path)
    if not date:
        return None
    decision_path = path.parent / f"tcu_decisions_{date}.jsonl"
    if not decision_path.is_file():
        return None
    out = out_path or path.parent / f"{_replay_stem(path)}_decisions.jsonl"
    out.write_text(decision_path.read_text(encoding="utf-8"), encoding="utf-8")
    return out


def convert_replay(path: Path, fmt: str, *, include_decisions: bool = False) -> list[Path]:
    """Convert a replay to one of csv, chart, json, jsonl, summary, or text."""
    path = Path(path)
    fmt = fmt.lower()
    if fmt == "bin.gz":
        generated: list[Path] = []
    elif fmt == "chart":
        from virtual_tcu.telemetry.snapshot_chart import render_chart_html

        generated = []
        for csv_path in convert_replay_csv_split(path):
            chart = render_chart_html(
                csv_path,
                out_path=csv_path.with_name(f"{csv_path.stem}.chart.html"),
                delete_source=True,
            )
            if chart is not None:
                generated.append(chart)
    elif fmt == "csv":
        generated = convert_replay_csv_split(path)
    elif fmt in {"json", "jsonl", "summary", "text"}:
        generated = [_convert_single_file(path, fmt)]
    else:
        raise ValueError(f"unsupported replay conversion format: {fmt}")

    if include_decisions:
        decisions = export_decision_log_for_replay(path)
        if decisions is not None:
            generated.append(decisions)
    return generated


def _convert_single_file(path: Path, fmt: str) -> Path:
    stem = _replay_stem(path)
    out = path.parent / (f"{stem}.{fmt}" if fmt in {"json", "jsonl"} else f"{stem}_{fmt}.txt")
    rows: list[dict[str, object]] = []
    lines: list[str] = []
    prev_gear: int | None = None

    if fmt == "summary":
        buf = StringIO()
        format_replay(path, buf, fmt="summary", shift_only=False)
        out.write_text(buf.getvalue(), encoding="utf-8")
        return out

    for rel_ms, raw in iter_replay_records(path):
        td = parse_fh6_packet(raw)
        if td is None:
            continue
        if fmt == "json":
            rows.append(telemetry_record(rel_ms, td))
        elif fmt == "jsonl":
            lines.append(json.dumps(telemetry_record(rel_ms, td), ensure_ascii=False))
        elif fmt == "text":
            lines.append(format_text_line(rel_ms, td, prev_gear=prev_gear))
        prev_gear = td.gear

    out.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        out.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        out.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return out


def convert_replay_async(
    path: Path,
    fmt: str,
    *,
    include_decisions: bool = False,
    on_done: Callable[[list[Path]], None] | None = None,
    on_error: Callable[[Exception], None] | None = None,
) -> None:
    """Run conversion in a background thread for UI/server callers."""

    def _worker() -> None:
        try:
            result = convert_replay(path, fmt, include_decisions=include_decisions)
            if on_done:
                on_done(result)
        except Exception as exc:
            if on_error:
                on_error(exc)
            else:
                print(f"[LogConverter] conversion failed: {exc}")

    threading.Thread(target=_worker, daemon=True, name="log-converter").start()
