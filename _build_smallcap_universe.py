"""One-shot: pull small-cap universe from Finviz ($300M-$2B, price>$5, vol>500K)."""
import re
import sys
import time
from pathlib import Path

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
FILTERS = "cap_small,sh_price_o5,sh_avgvol_o500"
BLACKLIST = {"BRK-B", "PBR-A", "BF-B", "CTRA", "ACLX", "TERN", "FOLD", "CUK"}


def fetch(offset):
    params = {"v": "111", "f": FILTERS, "o": "-marketcap", "r": str(offset)}
    r = requests.get(
        "https://finviz.com/screener.ashx",
        params=params, headers=HEADERS, timeout=15,
    )
    r.raise_for_status()
    tickers = []
    for m in re.finditer(r'data-boxover-ticker="([A-Z\.\-]+)"', r.text):
        t = m.group(1)
        if t not in tickers:
            tickers.append(t)
    return tickers


def main():
    all_t = []
    offset = 1
    page = 1
    while True:
        print(f"  Page {page} (offset {offset})...", file=sys.stderr)
        try:
            page_t = fetch(offset)
        except Exception as e:
            print(f"  Error: {e}", file=sys.stderr)
            break
        if not page_t:
            break
        new = [t for t in page_t if t not in all_t and t not in BLACKLIST]
        if not new:
            break
        all_t.extend(new)
        offset += 20
        page += 1
        time.sleep(0.7)
        if page > 100:
            break

    print(f"\nFetched {len(all_t)} small-cap tickers")
    print("First 20:", " ".join(all_t[:20]))
    print("Last 20:", " ".join(all_t[-20:]))

    lines = [
        "# Small-cap universe (Finviz cap_small: $300M-$2B, price>$5, avg vol>500K)",
        f"# Count: {len(all_t)}",
        "",
    ]
    for i in range(0, len(all_t), 10):
        lines.append(" ".join(all_t[i:i + 10]))
    out = Path("C:/Users/algee/TraderMCP-RH/small_cap_universe.txt")
    out.write_text("\n".join(lines) + "\n")
    print(f"\nWrote {len(all_t)} tickers to {out}")


if __name__ == "__main__":
    main()
