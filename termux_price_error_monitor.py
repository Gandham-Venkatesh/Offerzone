#!/usr/bin/env python3
"""Same command as before; the monitor itself now lives in the `lootwatch` package next to this file.

    python termux_price_error_monitor.py --doctor
    python termux_price_error_monitor.py --test-ntfy
    python termux_price_error_monitor.py --watchlist-file watchlist.txt

is identical to `python -m lootwatch ...`.
"""

import sys

from lootwatch.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
