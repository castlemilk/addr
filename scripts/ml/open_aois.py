"""Second training AOI set for the open orthophoto archive (open-aois-v1).

The first six AOIs proved the ingest paths. This set adds one contrasting site per
source so the training corpus is not all leafy detached suburbs:

    naip-phoenix      desert suburban, tile roofs
    ign-paris         dense European block
    pnoa-valencia     coastal Mediterranean
    swisstopo-interlaken  alpine valley
    nsw-newcastle     coastal Australian (server 500 on one tile; Maitland is the fallback)
    nsw-maitland      inland Hunter Valley
    pdok-rotterdam    port/industrial

Each entry is fetched sequentially through `open_imagery.fetch`; a matching source
already in the catalog is skipped, so a rerun is safe. A 10 m Sentinel-2 context COG is
added for every AOI through the same catalog.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .open_imagery import SOURCES, Catalog, fetch

AOIS = {
    "naip-phoenix": ("naip", (-112.020, 33.420, -111.970, 33.465)),
    "ign-paris": ("ign", (2.320, 48.820, 2.370, 48.860)),
    "pnoa-valencia": ("pnoa", (-0.400, 39.450, -0.350, 39.490)),
    "swisstopo-interlaken": ("swisstopo", (7.820, 46.660, 7.870, 46.700)),
    "nsw-newcastle": ("nsw", (151.740, -32.950, 151.790, -32.910)),
    "nsw-maitland": ("nsw", (151.530, -32.750, 151.580, -32.710)),
    "pdok-rotterdam": ("pdok", (4.100, 51.930, 4.150, 51.970)),
}


def already(catalog: Catalog, source_id: str, bbox) -> bool:
    for row in catalog.query(bbox):
        if row["source_id"] != source_id:
            continue
        b = json.loads(row["bounds_wsen"])
        if abs(b[0] - bbox[0]) < 1e-6 and abs(b[1] - bbox[1]) < 1e-6:
            return True
    return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=Path("/Volumes/gamma-systems-2/addr-imagery/open"))
    parser.add_argument("--only", default=None, help="one AOI id from the set")
    parser.add_argument("--no-sentinel", action="store_true")
    args = parser.parse_args(argv)
    out = args.out
    catalog = Catalog(out / "catalog.db")
    names = [args.only] if args.only else list(AOIS)
    for name in names:
        source_id, bbox = AOIS[name]
        if already(catalog, source_id, bbox):
            print(f"{name}: already indexed, skipping", flush=True)
            continue
        print(f"{name}: {source_id} {bbox}", flush=True)
        try:
            row = fetch(source_id, bbox, out, catalog)
            print(f"  {row['width']}x{row['height']} {row['bytes']/1e6:.1f} MB {row['layout']}", flush=True)
        except Exception as exc:
            print(f"  FAILED: {exc}", flush=True)
            continue
        if not args.no_sentinel:
            try:
                s2 = fetch("sentinel2", bbox, out, catalog)
                print(f"  sentinel2 {s2['width']}x{s2['height']} {s2['bytes']/1e3:.0f} KB {s2['capture'][:10]}", flush=True)
            except Exception as exc:
                print(f"  sentinel2 FAILED: {exc}", flush=True)
    catalog.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
