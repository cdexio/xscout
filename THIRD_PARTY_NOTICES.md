# Third-party notices

xscout vendors (copies and adapts) code from the following MIT-licensed projects.

## twscrape

- Source: https://github.com/vladkens/twscrape (commit c1500f2, 2026-09-23)
- Used in: `src/xscout/xweb/tid.py` (transaction-id key derivation),
  `src/xscout/xweb/discovery.py` (script list parsing), and the phase 0 probe
  `tools/probe/xclid.py`.

```
MIT License

Copyright (c) 2023 vladkens

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## XClientTransaction

- Source: https://github.com/iSarabjitDhiman/XClientTransaction (MIT)
- The transaction-id algorithm in twscrape's `xclid.py` originates here.

## x-client-transaction-id-pair-dict

- Source: https://github.com/fa0311/x-client-transaction-id-pair-dict
- Not vendored; its published `pair.json` is fetched at runtime as the
  second transaction-id layer.
