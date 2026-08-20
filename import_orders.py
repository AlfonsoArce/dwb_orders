#!/usr/bin/env python3
"""Load the JSON Order archive into Postgres. See dwb/importer.py."""

import sys

from dwb.importer import main

if __name__ == "__main__":
    sys.exit(main())
