"""Enriching stored Orders with Charges from a History export."""

from decimal import Decimal

import openpyxl
import psycopg
import pytest
from conftest import make_order, one, rows

from dwb import ingest
from dwb.enrich import enrich

# Only the columns enrichment reads; the real export carries ninety more.
HEADER = ["ID", "OrderNumber", "PriceBreakdown", "FinalPrice"]

TWO_CHARGES = ("Cargo Van: Medley to Miami~51~565~5AP~6~6391490"
               "~4Waiting Time~573~5.45~5M~6~6391490")


def write_export(path, export_rows):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(HEADER)
    for row in export_rows:
        sheet.append(row)
    workbook.save(path)
    return str(path)


@pytest.fixture
def stored_orders(database):
    """Two Orders in the store; make_order derives dispatch id as number - 10301.

    Seeded over its own transactional connection: ingest stages into temp
    tables that empty on commit, which autocommit would trigger per statement.
    """
    with psycopg.connect(database) as seed:
        result = ingest.ingest_orders(
            seed, [make_order(order_number=401791), make_order(order_number=401792)])
        seed.commit()
    assert result.orders_written == 2
    return {401791: 391490, 401792: 391491}


def test_charges_are_written_with_amounts_derived(conn, stored_orders, tmp_path):
    export = write_export(tmp_path / "history.xlsx",
                          [(391490, 401791, TWO_CHARGES, 97.85)])

    summary = enrich(conn, export)

    assert summary.rows_read == 1
    assert summary.orders_enriched == 1
    assert summary.charges_written == 2
    assert summary.sum_mismatches == 0
    assert summary.unmatched == []

    charges = rows(conn, """
        select c.charge_position, c.description, c.quantity, c.rate, c.amount,
               c.pricing_code, c.refs
        from order_charges c
        join orders o using (order_id)
        where o.order_number = 401791
        order by c.charge_position""")
    assert charges == [
        (1, "Cargo Van: Medley to Miami", Decimal(1), Decimal(65),
         Decimal(65), "AP", ["", "391490"]),
        (2, "Waiting Time", Decimal(73), Decimal("0.45"),
         Decimal("32.85"), "M", ["", "391490"]),
    ]

    breakdown, source_file, export_price, matches = one(conn, """
        select b.breakdown, b.source_file, b.export_final_price,
               b.matches_final_price
        from order_price_breakdowns b
        join orders o using (order_id)
        where o.order_number = 401791""")
    assert breakdown == TWO_CHARGES
    assert source_file == "history.xlsx"
    assert export_price == Decimal("97.85")
    assert matches is True


def test_rerunning_replaces_charges_instead_of_accumulating(conn, stored_orders,
                                                            tmp_path):
    write_export(tmp_path / "history.xlsx",
                 [(391490, 401791, TWO_CHARGES, 97.85)])
    enrich(conn, str(tmp_path / "history.xlsx"))

    # A corrected export drops the waiting time; the store must follow it.
    corrected = write_export(
        tmp_path / "corrected.xlsx",
        [(391490, 401791, "Cargo Van: Medley to Miami~51~565~5AP~6~6391490", 65)])
    enrich(conn, corrected)

    assert one(conn, "select count(*) from order_price_breakdowns") == (1,)
    assert one(conn, "select description, source_file from order_charges c "
                     "join order_price_breakdowns using (order_id)") == \
        ("Cargo Van: Medley to Miami", "corrected.xlsx")


def test_matching_is_by_dispatch_record_id_never_order_number(conn, stored_orders,
                                                              tmp_path):
    # The Order Number exists in the store, but the export row names a record
    # the store never kept — a reissued number's lost twin. It must not attach
    # to the surviving Order.
    export = write_export(tmp_path / "history.xlsx",
                          [(999999, 401791, TWO_CHARGES, 97.85)])

    summary = enrich(conn, export)

    assert summary.orders_enriched == 0
    assert summary.unmatched == [999999]
    assert one(conn, "select count(*) from order_charges") == (0,)


def test_a_sum_that_misses_final_price_is_stored_and_flagged(conn, stored_orders,
                                                             tmp_path):
    export = write_export(tmp_path / "history.xlsx",
                          [(391490, 401791, TWO_CHARGES, 120)])

    summary = enrich(conn, export)

    assert summary.sum_mismatches == 1
    assert one(conn, "select matches_final_price from order_price_breakdowns") == \
        (False,)
    # The Charges themselves are still there for the audit.
    assert one(conn, "select count(*) from order_charges") == (2,)


def test_batches_smaller_than_the_export_cover_every_row(conn, stored_orders,
                                                         tmp_path):
    export = write_export(tmp_path / "history.xlsx", [
        (391490, 401791, TWO_CHARGES, 97.85),
        (391491, 401792, "Van AH: Ft. Lauderdale to Miami~51~574~5AP~6~6391491", 74),
        (999999, 999999, "Van~51~510~5AP~6~6999999", 10),
    ])

    summary = enrich(conn, export, batch_size=1)

    assert summary.rows_read == 3
    assert summary.orders_enriched == 2
    assert summary.charges_written == 3
    assert summary.unmatched == [999999]


def test_the_command_line_entry_point(run_cli, conn, stored_orders, database,
                                      tmp_path):
    export = write_export(tmp_path / "history.xlsx",
                          [(391490, 401791, TWO_CHARGES, 97.85)])

    result = run_cli("enrich_price_breakdown.py", "--input", export,
                     "--dsn", database, "--no-progress")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Rows read:         1" in result.stdout
    assert "Orders enriched:   1" in result.stdout
    assert "Charges written:   2" in result.stdout
    assert one(conn, "select count(*) from order_charges") == (2,)


def test_a_missing_export_file_fails_before_touching_the_store(run_cli, database):
    result = run_cli("enrich_price_breakdown.py", "--input", "no/such/file.xlsx",
                     "--dsn", database, "--no-progress")
    assert result.returncode == 2
    assert "No such file" in result.stderr
