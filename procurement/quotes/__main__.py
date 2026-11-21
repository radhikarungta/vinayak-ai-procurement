"""Read the quotes in the data pack, print the re-priced comparison and write the deals seed.

    uv run python -m procurement.quotes            # print + write data/seed/parsed_quote_deals.json
    uv run python -m procurement.quotes --sync     # also replace those PDFs' deals in the deals store
"""

from __future__ import annotations

import sys

from . import compare as C
from . import pack


def main() -> None:
    cmp = pack.comparison()
    for v in sorted(cmp.vendors, key=lambda v: (v.rank_repriced is None, v.rank_repriced or 0)):
        pct = f"+{v.pct_above_lowest:.1f}%" if v.pct_above_lowest is not None else ""
        print(f"{v.vendor:40.40} conf={v.read_confidence:6} basis={v.quantity_basis:9} as quoted={C.inr(v.as_quoted_total):>14} "
              f"re-priced={C.inr(v.repriced_total):>14} {pct}")
    for s in cmp.insights + cmp.notes:
        print("-", s)
    if "--sync" in sys.argv:
        print(pack.sync_deals())
    else:
        print("Seed written to", pack.write_seed())


if __name__ == "__main__":
    main()
