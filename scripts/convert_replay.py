from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _resolve_files(patterns: list[str]) -> list[Path]:
    files: list[Path] = []
    for pattern in patterns:
        path = Path(pattern)
        if any(ch in pattern for ch in "*?[]"):
            files.extend(sorted(Path().glob(pattern)))
        elif path.exists():
            files.append(path)
        else:
            raise SystemExit(f"file not found: {pattern}")
    if not files:
        raise SystemExit("no replay files matched")
    return files


def main() -> int:
    from virtual_tcu.telemetry.log_converter import convert_replay

    parser = argparse.ArgumentParser(description="Convert VirtualTCU replay logs for analysis.")
    parser.add_argument("files", nargs="+", help="Replay files, e.g. logs/tcu_replay_*.bin.gz")
    parser.add_argument(
        "--format",
        choices=("csv", "chart", "json", "jsonl", "summary", "text"),
        default="chart",
        help="Output format. chart writes self-contained HTML per race segment.",
    )
    parser.add_argument(
        "--include-decisions",
        action="store_true",
        help="Also copy same-day tcu_decisions_YYYYMMDD.jsonl next to the export.",
    )
    args = parser.parse_args()

    for replay_path in _resolve_files(args.files):
        generated = convert_replay(
            replay_path,
            args.format,
            include_decisions=args.include_decisions,
        )
        if generated:
            for path in generated:
                print(path)
        else:
            print(f"{replay_path}: nothing generated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
