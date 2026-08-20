#!/usr/bin/env python3
"""Apply pending database migrations. See dwb/migrate.py."""

import sys

from dwb.migrate import main

if __name__ == "__main__":
    sys.exit(main())
