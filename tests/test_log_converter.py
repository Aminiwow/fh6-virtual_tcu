import gzip
import json
import struct
from pathlib import Path

from virtual_tcu.telemetry.log_converter import convert_replay
from virtual_tcu.telemetry.logger import LOG_MAGIC


def _packet(
    *,
    rel_ms: int,
    race_on: int,
    gear: int,
    rpm: float,
    speed_ms: float,
    car_ordinal: int = 4197,
) -> tuple[int, bytes]:
    data = bytearray(324)
    struct.pack_into("<iIfff", data, 0, race_on, rel_ms, 8000.0, 900.0, rpm)
    struct.pack_into("<fff", data, 20, 0.0, 0.0, 0.0)
    struct.pack_into("<fff", data, 32, 0.0, 0.0, speed_ms)
    struct.pack_into("<fff", data, 44, 0.0, 0.0, 0.0)
    struct.pack_into("<fff", data, 56, 0.0, 0.0, 0.0)
    struct.pack_into("<ffff", data, 68, 0.5, 0.5, 0.5, 0.5)
    struct.pack_into("<ffff", data, 100, 60.0, 60.0, 60.0, 60.0)
    struct.pack_into("<iiiii", data, 212, car_ordinal, 5, 900, 2, 6)
    struct.pack_into("<fff", data, 256, speed_ms, 250000.0, 420.0)
    struct.pack_into("<f", data, 284, 0.0)
    data[315] = 255
    data[316] = 0
    data[317] = 0
    data[318] = 0
    data[319] = gear
    return rel_ms, bytes(data)


def _write_replay(path: Path) -> None:
    records = [
        _packet(rel_ms=0, race_on=0, gear=2, rpm=3200.0, speed_ms=25.0),
        _packet(rel_ms=16, race_on=1, gear=2, rpm=4200.0, speed_ms=30.0),
        _packet(rel_ms=32, race_on=1, gear=3, rpm=5200.0, speed_ms=35.0),
    ]
    with gzip.open(path, "wb") as f:
        f.write(LOG_MAGIC)
        for rel_ms, raw in records:
            f.write(struct.pack("<IH", rel_ms, len(raw)))
            f.write(raw)


def test_convert_replay_splits_csv_and_copies_decisions(tmp_path):
    replay = tmp_path / "tcu_replay_20260608_120000.bin.gz"
    _write_replay(replay)
    decisions = tmp_path / "tcu_decisions_20260608.jsonl"
    decisions.write_text(json.dumps({"event": "shift_up"}) + "\n", encoding="utf-8")

    generated = convert_replay(replay, "csv", include_decisions=True)
    names = {path.name for path in generated}

    assert "replay_20260608_120000_roaming.csv" in names
    assert "replay_20260608_120000_race1_car4197.csv" in names
    assert "replay_20260608_120000_decisions.jsonl" in names
    assert (tmp_path / "replay_20260608_120000_decisions.jsonl").read_text(
        encoding="utf-8"
    ) == decisions.read_text(encoding="utf-8")


def test_convert_replay_writes_chart_html(tmp_path):
    replay = tmp_path / "tcu_replay_20260608_120000.bin.gz"
    _write_replay(replay)

    generated = convert_replay(replay, "chart")

    assert generated
    assert all(path.suffix == ".html" for path in generated)
    assert "<svg" in generated[0].read_text(encoding="utf-8")
