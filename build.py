#!/usr/bin/env python3
"""Build index.html: inline prices.json and vendor/qrcode.js.

index.html in this repo is the *built* file, so it works by double-clicking with
no server. Edit the template, then run:

    python3 build.py

Only two things are generated:
  PRICES   <- prices.json            (model -> [input, read, write, output] USD/1M)
  <qrcode> <- vendor/qrcode.js       (MIT, Kazuhiko Arase; the card's QR code)
"""

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "page.html")
OUTPUT = os.path.join(HERE, "index.html")
PRICES = os.path.join(HERE, "prices.json")
QR = os.path.join(HERE, "vendor", "qrcode.js")


def main():
    for path in (TEMPLATE, PRICES, QR):
        if not os.path.exists(path):
            sys.exit("missing %s" % path)
    page = open(TEMPLATE, encoding="utf-8").read()
    prices = json.load(open(PRICES, encoding="utf-8"))
    qr = open(QR, encoding="utf-8").read()

    if "__PRICES__" not in page:
        sys.exit("page.html has no __PRICES__ placeholder")
    page = page.replace("__PRICES__", json.dumps(prices, separators=(",", ":"), ensure_ascii=False))

    tag = re.compile(r'<script src="vendor/qrcode\.js"></script>')
    if not tag.search(page):
        sys.exit('page.html has no <script src="vendor/qrcode.js"></script>')
    page = tag.sub(lambda _: "<script>\n" + qr.rstrip() + "\n</script>", page, count=1)

    with open(OUTPUT, "w", encoding="utf-8") as handle:
        handle.write(page)
    print("wrote %s (%.0f KB, %d prices inlined)" % (OUTPUT, len(page.encode()) / 1024,
                                                    len([k for k in prices if not k.startswith("_")])))


if __name__ == "__main__":
    main()
