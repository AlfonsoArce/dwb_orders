#!/usr/bin/env python3
"""Parse the PriceBreakdown column of a History export and validate it.

Every row's PriceBreakdown is parsed into individual charges (see
dwb/price_breakdown.py for the format), and the parse is considered valid
when the charge amounts sum to the row's FinalPrice. Prints the tally and
any rows that fail, so a format drift in a future export is visible
immediately.

With --text, skips the workbook entirely: parses one PriceBreakdown string
and prints the breakdown as JSON, for callers that are not Python.

    python3 parse_price_breakdown.py
    python3 parse_price_breakdown.py --input input/History-2025-04-12.xlsx --show-failures 20
    python3 parse_price_breakdown.py --text 'Van: Miami to Miami~51~565~5AP~6~6245465'
    echo 'Van: ...~51~565~5AP~6~6245465' | python3 parse_price_breakdown.py --text - --final-price 65

Exit status: 0 when every row's charges sum to its FinalPrice, 1 when any row
fails — so this works as a scripted check that a new export still parses.
Reads the export and nothing else: no database, no writes.
"""

import argparse
import json
import sys

from dwb.enrich import read_export
from dwb.price_breakdown import parse


def validate_workbook(path):
    """Yield (row_id, breakdown, final_price, matches) per data row."""
    for record_id, _text, breakdown, final_price in read_export(path):
        yield record_id, breakdown, final_price, breakdown.matches(final_price)


def main(argv=None):
    """Validate a workbook, or parse one string with --text.

    Exits non-zero when any row's charges fail to sum to its FinalPrice, so a
    format drift in a future export fails a scripted check rather than only
    printing. Reads the export file and nothing else — no database, no
    writes — so it is safe to point at a production export.
    """
    parser = argparse.ArgumentParser(
        description="Parse and validate the PriceBreakdown column of a History export."
    )
    parser.add_argument(
        "--input",
        default="input/History-2025-04-12.xlsx",
        help="Path to the History .xlsx export",
    )
    parser.add_argument(
        "--show-failures",
        type=int,
        default=10,
        metavar="N",
        help="How many failing rows to print in full (default 10)",
    )
    parser.add_argument(
        "--text",
        metavar="BREAKDOWN",
        help="Parse this one PriceBreakdown string and print JSON"
        " instead of validating the workbook ('-' reads stdin)",
    )
    parser.add_argument(
        "--final-price",
        metavar="PRICE",
        help="With --text: also check that the charges sum to this price",
    )
    args = parser.parse_args(argv)

    if args.text is not None:
        text = sys.stdin.read() if args.text == "-" else args.text
        result = parse(text.strip("\n")).as_dict(args.final_price)
        print(json.dumps(result, indent=2))
        return 0 if result.get("matches_final_price", True) else 1

    total = valid = 0
    failures = []
    for row_id, breakdown, final_price, matches in validate_workbook(args.input):
        total += 1
        if matches:
            valid += 1
        else:
            failures.append((row_id, breakdown, final_price))

    print(f"{args.input}: {total} rows, {valid} valid, {len(failures)} failed")
    for row_id, breakdown, final_price in failures[: args.show_failures]:
        print(f"  ID {row_id}: FinalPrice={final_price} sum={breakdown.total}")
        for charge in breakdown.charges:
            print(
                f"    {charge.description!r}: {charge.quantity} x {charge.rate}"
                f" = {charge.amount} [{charge.code}]"
            )
    if len(failures) > args.show_failures:
        print(f"  … and {len(failures) - args.show_failures} more")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
