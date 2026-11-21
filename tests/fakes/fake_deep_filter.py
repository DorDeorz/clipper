"""Stand-in for the `deep-filter` CLI: same arguments, halves the samples into -o DIR."""

from __future__ import annotations

import argparse
import array
import sys
import wave
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-D", action="store_true")
    ap.add_argument("-a", type=float, default=100)
    ap.add_argument("-o", required=True)
    ap.add_argument("files", nargs="+")
    args = ap.parse_args()
    out = Path(args.o)
    out.mkdir(parents=True, exist_ok=True)
    for f in args.files:
        with wave.open(f, "rb") as r:
            params = r.getparams()
            data = array.array("h", r.readframes(r.getnframes()))
        data = array.array("h", (v // 2 for v in data))
        with wave.open(str(out / Path(f).name), "wb") as w:
            w.setparams(params)
            w.writeframes(data.tobytes())
    return 0


if __name__ == "__main__":
    sys.exit(main())
