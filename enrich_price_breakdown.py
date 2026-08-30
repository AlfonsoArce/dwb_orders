#!/usr/bin/env python3
"""Enrich stored Orders with Charges from a History export. See dwb/enrich.py."""

import sys

from dwb.enrich import main

if __name__ == "__main__":
    sys.exit(main())
