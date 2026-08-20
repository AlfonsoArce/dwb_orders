#!/usr/bin/env python3
"""Check the database against the JSON archive. See dwb/verify.py."""

import sys

from dwb.verify import main

if __name__ == "__main__":
    sys.exit(main())
