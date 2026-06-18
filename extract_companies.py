#!/usr/bin/env python3
"""Extract a normalized, deduplicated list of (company, address) pairs from order JSONs.

Reads order_*.json files produced by get_orders.py and runs three explicit phases:

  Phase 1 — Address standardization. Every route_stop's address is normalized
            with usaddress-scourgify (USPS-style: uppercase, abbreviated suffixes,
            suite extracted from the street line). Falls back to a regex
            normalizer when scourgify can't parse.

  Phase 2 — Company-name standardization. Once addresses are locked in, company
            names are clustered globally using rapidfuzz (token_set_ratio).
            The most-frequent display variant in each cluster becomes the
            canonical name; raw variants are preserved for audit.

  Phase 3 — Aggregation. Rows are deduped on
            (canonical_anchor, address_line_1, address_line_2, city, state, zip5,
            country). Suite/unit is part of identity — same building can host
            many companies on different suites.

Output is a single .xlsx workbook.

    python3 extract_companies.py
    python3 extract_companies.py --cluster-threshold 95
    python3 extract_companies.py --no-cluster-names    # skip phase 2
"""

import argparse
import json
import logging
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime

import xlsxwriter
from tqdm import tqdm

try:
    from scourgify import normalize_address_record
except ImportError:  # pragma: no cover
    normalize_address_record = None

try:
    from rapidfuzz import fuzz, process as rf_process
except ImportError:  # pragma: no cover
    fuzz = None
    rf_process = None

logger = logging.getLogger("dwb_companies")

WS = re.compile(r"\s+")

# Suite designators we want to detect inside the street line so we can split them
# out even when scourgify can't parse the rest of the address.
SUITE_DESIGNATORS = {
    "SUITE": "STE", "STE": "STE",
    "UNIT": "UNIT",
    "APARTMENT": "APT", "APT": "APT",
    "BUILDING": "BLDG", "BLDG": "BLDG",
    "FLOOR": "FL",
    "ROOM": "RM", "RM": "RM",
    "DEPARTMENT": "DEPT", "DEPT": "DEPT",
    "LOT": "LOT",
    "SPACE": "SPC", "SPC": "SPC",
    "TRAILER": "TRLR", "TRLR": "TRLR",
}

# Matches a designator + identifier (e.g. "SUITE 200", "STE. A-12", "#100").
SUITE_RE = re.compile(
    r"\b(?:SUITE|STE|UNIT|APARTMENT|APT|BUILDING|BLDG|FLOOR|FL|ROOM|RM|DEPARTMENT|DEPT|LOT|SPACE|SPC|TRAILER|TRLR)\.?\s*([A-Z0-9][A-Z0-9\-]*)\b",
    re.IGNORECASE,
)
HASH_SUITE_RE = re.compile(r"#\s*([A-Z0-9][A-Z0-9\-]*)", re.IGNORECASE)

# Lightweight USPS-style fallbacks for the rare cases scourgify can't parse.
USPS_SUFFIX = {
    "STREET": "ST", "AVENUE": "AVE", "BOULEVARD": "BLVD", "ROAD": "RD",
    "DRIVE": "DR", "LANE": "LN", "COURT": "CT", "PLACE": "PL",
    "HIGHWAY": "HWY", "PARKWAY": "PKWY", "TERRACE": "TER", "CIRCLE": "CIR",
    "TRAIL": "TRL", "SQUARE": "SQ", "EXPRESSWAY": "EXPY",
}
USPS_DIRECTIONAL = {
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW",
}

# Common business-entity suffixes — kept on `company_norm` for traceability, also
# stripped into `company_key` so "ACME INC" and "ACME, INC." collapse.
COMPANY_SUFFIX_RE = re.compile(
    r"[,\s]+(?:INC|INC\.|LLC|L\.L\.C\.|L\.L\.C|LTD|LTD\.|CORP|CORP\.|CORPORATION|CO|CO\.|COMPANY|PLC|LP|LLP|PA|PC)\.?$",
    re.IGNORECASE,
)

# Order time format from the API, e.g. "Fri, 01 Jan 2021 09:40:29".
ORDER_TIME_FMT = "%a, %d %b %Y %H:%M:%S"


def setup_logging(level, log_file):
    lvl = getattr(logging, str(level).upper(), logging.INFO)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")

    class TqdmHandler(logging.Handler):
        def emit(self, record):
            try:
                tqdm.write(self.format(record), file=sys.stderr)
            except Exception:
                self.handleError(record)

    console = TqdmHandler()
    console.setLevel(lvl)
    console.setFormatter(fmt)
    logger.addHandler(console)

    if log_file:
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(lvl)
        fh.setFormatter(fmt)
        logger.addHandler(fh)


def list_order_files(input_dir):
    def key(name):
        stem = name[:-5] if name.endswith(".json") else name
        num = stem.rsplit("_", 1)[-1]
        try:
            return (0, int(num))
        except ValueError:
            return (1, name)

    entries = []
    with os.scandir(input_dir) as it:
        for e in it:
            if e.is_file() and e.name.endswith(".json"):
                entries.append(e.name)
    entries.sort(key=key)
    return [os.path.join(input_dir, n) for n in entries]


def cluster_company_names(name_counts, threshold=92, min_name_len=4):
    """Greedy-cluster company keys by rapidfuzz token_set_ratio.

    Seeds clusters in descending frequency order so the most common variant
    becomes the cluster anchor. Returns dict: company_key -> anchor_key.

    Token-set 100 happens when one name's tokens are a subset of the other's
    (e.g. "FEDEX" vs "FEDEX TRADE NETWORKS"). To avoid silently absorbing a
    short brand into a longer subsidiary, we require either a reasonable
    Levenshtein ratio OR a length delta <= 2 chars before accepting.
    """
    if fuzz is None or rf_process is None:
        logger.warning("rapidfuzz not importable — skipping name clustering")
        return {k: k for k in name_counts}

    sorted_names = [n for n, _ in name_counts.most_common()]
    anchors = []
    mapping = {}

    for name in sorted_names:
        if len(name) < min_name_len or not anchors:
            mapping[name] = name
            anchors.append(name)
            continue
        match = rf_process.extractOne(name, anchors, scorer=fuzz.token_set_ratio)
        if match is None:
            mapping[name] = name
            anchors.append(name)
            continue
        cand, score, _ = match
        accept = score >= threshold and (
            fuzz.ratio(name, cand) >= 75 or abs(len(name) - len(cand)) <= 2
        )
        if accept:
            mapping[name] = cand
        else:
            mapping[name] = name
            anchors.append(name)
    return mapping


def norm_company(raw):
    if not raw:
        return "", ""
    s = WS.sub(" ", str(raw).strip().upper())
    s = re.sub(r"[‘’“”]", "'", s)
    s = re.sub(r"\s*[,;]\s*", ", ", s).strip(", ")
    key = COMPANY_SUFFIX_RE.sub("", s).strip(", ")
    key = re.sub(r"[^\w\s&/-]", "", key)
    key = WS.sub(" ", key).strip()
    return s, key


def normalize_suite_token(text):
    """Take whatever was extracted as suite info and return canonical 'STE 100' style."""
    if not text:
        return ""
    s = text.upper().replace(".", " ")
    s = WS.sub(" ", s).strip()
    if not s:
        return ""
    # "#100" → "STE 100"
    m = HASH_SUITE_RE.match(s)
    if m:
        return f"STE {m.group(1)}"
    tokens = s.split()
    if tokens[0] in SUITE_DESIGNATORS:
        designator = SUITE_DESIGNATORS[tokens[0]]
        value = " ".join(tokens[1:]) if len(tokens) > 1 else ""
        return f"{designator} {value}".strip()
    # Bare value (e.g. "100", "A") — assume STE
    return f"STE {s}"


def extract_suite_from_address(address_upper):
    """Return (cleaned_address, suite_string) by stripping any suite designator inside."""
    if not address_upper:
        return "", ""
    m = SUITE_RE.search(address_upper)
    if m:
        suite = m.group(0)
        cleaned = (address_upper[: m.start()] + address_upper[m.end():]).strip(" ,;-")
        cleaned = WS.sub(" ", cleaned)
        return cleaned, normalize_suite_token(suite)
    m = HASH_SUITE_RE.search(address_upper)
    if m:
        suite = m.group(0)
        cleaned = (address_upper[: m.start()] + address_upper[m.end():]).strip(" ,;-")
        cleaned = WS.sub(" ", cleaned)
        return cleaned, normalize_suite_token(suite)
    return address_upper, ""


def fallback_normalize_street(s):
    """Used only when scourgify rejects the input — best-effort USPS-style cleanup."""
    if not s:
        return ""
    s = s.upper()
    s = re.sub(r"[.,;]", " ", s)
    # N.W. / N.E. style — collapse before tokenizing.
    s = re.sub(r"\b([NSEW])\s*\.\s*([NSEW])\b", r"\1\2", s)
    tokens = WS.split(s.strip())
    out = []
    for tok in tokens:
        if tok in USPS_DIRECTIONAL:
            out.append(USPS_DIRECTIONAL[tok])
        elif tok in USPS_SUFFIX:
            out.append(USPS_SUFFIX[tok])
        else:
            out.append(tok)
    return WS.sub(" ", " ".join(out)).strip()


def normalize_stop(stop):
    """Return a dict of normalized fields for one route_stop, plus parse_ok/parse_error."""
    company_raw = (stop.get("company") or "").strip()
    address_raw = (stop.get("address") or "").strip()
    suite_raw = (stop.get("suite") or "").strip()
    city_raw = (stop.get("city") or "").strip()
    state_raw = (stop.get("state") or "").strip()
    zip_raw = (stop.get("postal_code") or "").strip()
    country_raw = (stop.get("country") or "").strip()

    parse_ok = False
    parse_err = ""
    line1 = line2 = city = state = zip_full = ""

    if normalize_address_record and (address_raw or city_raw or zip_raw):
        try:
            result = normalize_address_record({
                "address_line_1": address_raw or "",
                "address_line_2": suite_raw or "",
                "city": city_raw or "",
                "state": state_raw or "",
                "postal_code": zip_raw or "",
            })
            line1 = (result.get("address_line_1") or "").upper()
            line2 = (result.get("address_line_2") or "").upper()
            city = (result.get("city") or "").upper()
            state = (result.get("state") or "").upper()
            zip_full = (result.get("postal_code") or "").upper()
            parse_ok = True
        except Exception as e:
            parse_err = type(e).__name__ + ": " + str(e)[:200]

    if not parse_ok:
        cleaned, suite_from_line = extract_suite_from_address(address_raw.upper())
        line1 = fallback_normalize_street(cleaned)
        line2 = normalize_suite_token(suite_raw) or suite_from_line
        city = city_raw.upper()
        state = state_raw.upper()
        zip_full = zip_raw

    # Even when scourgify succeeded, double-check that no suite-shaped token
    # leaked into line1 (it usually splits, but defensive).
    if line1:
        line1, leaked = extract_suite_from_address(line1)
        if leaked and not line2:
            line2 = leaked

    zip5 = (zip_full or "").split("-")[0][:5]
    country = (country_raw or "UNITED STATES").upper()
    company_norm, company_key = norm_company(company_raw)

    return {
        "company_raw": company_raw,
        "company_norm": company_norm,
        "company_key": company_key,
        "address_raw": address_raw,
        "address_line_1": line1,
        "suite_raw": suite_raw,
        "address_line_2": line2,
        "city": city,
        "state": state,
        "zip5": zip5,
        "zip_full": zip_full,
        "country": country,
        "parse_ok": parse_ok,
        "parse_error": parse_err,
    }


def parse_order_time(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, ORDER_TIME_FMT)
    except (ValueError, TypeError):
        return None


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input-dir", default="output/orders")
    p.add_argument("--output", default="output/companies.xlsx")
    p.add_argument("--limit", type=int, default=0, help="Process only first N order files (0 = all)")
    p.add_argument("--cluster-threshold", type=int, default=92,
                   help="token_set_ratio threshold for merging company-name variants (default 92; set 100 to disable)")
    p.add_argument("--no-cluster-names", action="store_true",
                   help="Skip phase 2 — do not fuzzy-cluster company names")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--log-file", default="logs/extract_companies.log")
    return p.parse_args(argv)


COLUMNS = [
    "company_canonical",
    "company_anchor_key",
    "address_line_1",
    "address_line_2",
    "city",
    "state",
    "zip5",
    "country",
    "occurrence_count",
    "first_seen",
    "last_seen",
    "parse_ok",
    "company_raw_variants",
    "address_raw_variants",
    "suite_raw_variants",
    "cost_centers",
    "contact_names",
    "contact_phones",
    "zip_full_variants",
    "sample_order_numbers",
    "parse_errors",
]


def main(argv=None):
    args = parse_args(argv)
    setup_logging(args.log_level, args.log_file)

    if normalize_address_record is None:
        logger.warning("usaddress-scourgify not importable — running with regex fallback only.")

    if not os.path.isdir(args.input_dir):
        logger.error("Input directory not found: %s", args.input_dir)
        return 2

    files = list_order_files(args.input_dir)
    if args.limit:
        files = files[: args.limit]
    logger.info("Found %d order files in %s", len(files), args.input_dir)
    if not files:
        return 0

    # ----- Phase 1: address standardization -----
    # Normalize every stop's address up front so phase 2 can cluster names
    # against a stable, USPS-style address identity. We hold all stops in
    # memory; at ~300k stops × ~16 small fields this stays well under 200 MB.
    all_stops = []
    total_stops = 0
    parse_failures = 0
    failed_files = 0

    for path in tqdm(files, desc="Phase 1: addresses", unit="order"):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                order = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            failed_files += 1
            logger.warning("Skipping %s: %s", path, e)
            continue

        order_dt = parse_order_time(order.get("time"))
        order_number = order.get("order_number")
        cost_center = (order.get("cost_center") or "").strip()

        for stop in order.get("route_stops") or []:
            total_stops += 1
            n = normalize_stop(stop)
            if not n["parse_ok"]:
                parse_failures += 1
            contact = stop.get("contact") or {}
            all_stops.append({
                **n,
                "contact_name": (contact.get("name") or "").strip(),
                "contact_phone": (contact.get("phone") or "").strip(),
                "cost_center": cost_center,
                "order_number": order_number,
                "order_dt": order_dt,
            })

    logger.info(
        "Phase 1 done: %d stops normalized | parse failures: %d | failed files: %d",
        total_stops, parse_failures, failed_files,
    )

    # ----- Phase 2: name clustering -----
    # Group company keys into clusters so typos and minor variants
    # (e.g. LUFTHANSA TECHNIK vs LUFTHANSA TECHNIKA) share a single
    # canonical display name across the whole dataset.
    key_counts = Counter(s["company_key"] for s in all_stops if s["company_key"])

    if args.no_cluster_names or args.cluster_threshold >= 100:
        anchor_map = {k: k for k in key_counts}
        logger.info("Phase 2 skipped (name clustering disabled)")
    else:
        anchor_map = cluster_company_names(key_counts, threshold=args.cluster_threshold)
        merged = sum(1 for k, a in anchor_map.items() if k != a)
        n_anchors = len(set(anchor_map.values()))
        logger.info(
            "Phase 2 done: %d unique company keys → %d clusters (%d keys merged at threshold %d)",
            len(key_counts), n_anchors, merged, args.cluster_threshold,
        )

    # Canonical display form per anchor = most-frequent `company_norm` across
    # all keys that landed in that cluster.
    canonical_norm_counts = defaultdict(Counter)
    for s in all_stops:
        if not s["company_key"]:
            continue
        anchor = anchor_map.get(s["company_key"], s["company_key"])
        canonical_norm_counts[anchor][s["company_norm"]] += 1
    anchor_to_canonical = {
        anchor: counter.most_common(1)[0][0]
        for anchor, counter in canonical_norm_counts.items()
    }

    # ----- Phase 3: aggregate -----
    agg = {}
    for s in all_stops:
        anchor = anchor_map.get(s["company_key"], s["company_key"])
        canonical = anchor_to_canonical.get(anchor, s["company_norm"])
        key = (
            anchor,
            s["address_line_1"],
            s["address_line_2"],
            s["city"],
            s["state"],
            s["zip5"],
            s["country"],
        )
        entry = agg.get(key)
        if entry is None:
            entry = {
                "company_canonical": canonical,
                "company_anchor_key": anchor,
                "address_line_1": s["address_line_1"],
                "address_line_2": s["address_line_2"],
                "city": s["city"],
                "state": s["state"],
                "zip5": s["zip5"],
                "country": s["country"],
                "occurrence_count": 0,
                "first_seen": None,
                "last_seen": None,
                "parse_ok_count": 0,
                "company_raw_variants": set(),
                "address_raw_variants": set(),
                "suite_raw_variants": set(),
                "cost_centers": set(),
                "contact_names": set(),
                "contact_phones": set(),
                "zip_full_variants": set(),
                "sample_order_numbers": [],
                "parse_errors": set(),
            }
            agg[key] = entry

        entry["occurrence_count"] += 1
        if s["parse_ok"]:
            entry["parse_ok_count"] += 1
        if s["company_raw"]:
            entry["company_raw_variants"].add(s["company_raw"])
        if s["address_raw"]:
            entry["address_raw_variants"].add(s["address_raw"])
        if s["suite_raw"]:
            entry["suite_raw_variants"].add(s["suite_raw"])
        if s["cost_center"]:
            entry["cost_centers"].add(s["cost_center"])
        if s["contact_name"]:
            entry["contact_names"].add(s["contact_name"])
        if s["contact_phone"]:
            entry["contact_phones"].add(s["contact_phone"])
        if s["zip_full"]:
            entry["zip_full_variants"].add(s["zip_full"])
        if s["order_number"] is not None and len(entry["sample_order_numbers"]) < 5:
            entry["sample_order_numbers"].append(s["order_number"])
        if s["parse_error"]:
            entry["parse_errors"].add(s["parse_error"])
        odt = s["order_dt"]
        if odt:
            if entry["first_seen"] is None or odt < entry["first_seen"]:
                entry["first_seen"] = odt
            if entry["last_seen"] is None or odt > entry["last_seen"]:
                entry["last_seen"] = odt

    logger.info("Phase 3 done: %d unique (company × address) entries", len(agg))

    # Sort by count desc, then anchor key + address.
    rows = sorted(
        agg.values(),
        key=lambda e: (-e["occurrence_count"], e["company_anchor_key"], e["address_line_1"], e["address_line_2"]),
    )

    def to_row(e):
        first = e["first_seen"].strftime("%Y-%m-%d") if e["first_seen"] else ""
        last = e["last_seen"].strftime("%Y-%m-%d") if e["last_seen"] else ""
        return [
            e["company_canonical"],
            e["company_anchor_key"],
            e["address_line_1"],
            e["address_line_2"],
            e["city"],
            e["state"],
            e["zip5"],
            e["country"],
            e["occurrence_count"],
            first,
            last,
            e["parse_ok_count"] == e["occurrence_count"],
            " | ".join(sorted(e["company_raw_variants"])),
            " | ".join(sorted(e["address_raw_variants"])),
            " | ".join(sorted(e["suite_raw_variants"])),
            ", ".join(sorted(e["cost_centers"])),
            " | ".join(sorted(e["contact_names"])),
            " | ".join(sorted(e["contact_phones"])),
            " | ".join(sorted(e["zip_full_variants"])),
            ", ".join(str(n) for n in e["sample_order_numbers"]),
            " | ".join(sorted(e["parse_errors"])),
        ]

    # xlsx
    out_dir = os.path.dirname(os.path.abspath(args.output))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    workbook = xlsxwriter.Workbook(args.output, {"constant_memory": True, "strings_to_urls": False})
    sheet = workbook.add_worksheet("companies")
    header_fmt = workbook.add_format({"bold": True})
    sheet.write_row(0, 0, COLUMNS, header_fmt)
    sheet.freeze_panes(1, 0)
    EXCEL_LIMIT = 32_767
    for i, e in enumerate(rows, start=1):
        row = to_row(e)
        for j, v in enumerate(row):
            if isinstance(v, str) and len(v) > EXCEL_LIMIT:
                v = v[: EXCEL_LIMIT - 12] + "…[truncated]"
            sheet.write(i, j, v)
    workbook.close()
    logger.info("Excel: %s", os.path.abspath(args.output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
