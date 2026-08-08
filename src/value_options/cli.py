"""Offline operator commands; neither command has a broker or Sheets adapter."""
import argparse, json
from datetime import datetime
from pathlib import Path

from .market_data import EvidenceKind, EvidencePacket
from .operations import PaperRun


def load(path: Path) -> EvidencePacket:
    row = json.loads(path.read_text())
    return EvidencePacket(row["packet_id"], EvidenceKind(row["kind"]), row["provider"], row["feed"],
                          row["request"], datetime.fromisoformat(row["requested_at"]),
                          datetime.fromisoformat(row["received_at"]), row["raw"], row["normalized"]).sealed()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="value-options")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("dry-run", "replay"):
        command = sub.add_parser(name); command.add_argument("fixture", type=Path)
        command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv); packet = load(args.fixture); run = PaperRun()
    artifact = ({"mode": "dry-run", "accepted_packet": packet.verify()}
                if args.command == "dry-run" else {"mode": "replay", **run.excluded_replay([packet])})
    artifact.update({"classification": "PAPER ONLY", "order_policy": "NO LIVE ORDER"})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, sort_keys=True, indent=2) + "\n")
    print(f"PAPER ONLY | NO LIVE ORDER | {args.command} | artifact={args.output}")
    return 0


if __name__ == "__main__": raise SystemExit(main())
