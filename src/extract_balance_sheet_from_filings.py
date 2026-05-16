from pathlib import Path
import re
import html
import pandas as pd


# ============================================================
# CONFIG
# ============================================================

FILINGS_DIR = Path("sec_filings/AAL")
OUTPUT_CSV = Path("AAL_balance_sheet_from_filings.csv")

# IMPORTANT:
# Apple's rendered statement tables usually show values in millions,
# so table-extracted values need this multiplier.
# Inline XBRL facts, however, are usually raw dollars OR have a scale
# attribute. The XBRL helper below handles that separately.
VALUE_MULTIPLIER = 1_000_000


OUTPUT_COLUMNS = [
    "date",
    "totalStockholdersEquity",
    "totalAssets",
    "totalLiabilities",
    "cashAndCashEquivalents",
    "totalDebt",
    "netDebt",
    "retainedEarnings",
    "inventory",
    "propertyPlantEquipmentNet",
]


# ============================================================
# LABEL PATTERNS
# ============================================================

FIELD_PATTERNS = {
    "cashAndCashEquivalents": [
        r"cash\s+and\s+cash\s+equivalents",
        r"cash\s+and\s+equivalents",
    ],
    "totalAssets": [
        r"total\s+assets",
    ],
    "totalLiabilities": [
        r"^total\s+liabilities$",
    ],
    "retainedEarnings": [
        r"retained\s+earnings",
        r"accumulated\s+deficit",
    ],
    "inventory": [
        r"\binventory\b",
        r"\binventories\b",
    ],
    "propertyPlantEquipmentNet": [
        # label_matches removes punctuation first, so include punctuation-free versions.
        r"property\s+plant\s+and\s+equipment\s+net",
        r"property\s+and\s+equipment\s+net",
        r"property\s+plant\s+and\s+equipment",
        r"property\s+and\s+equipment",
    ],
    "totalStockholdersEquity": [
        r"total\s+stockholders'? equity",
        r"total\s+shareholders'? equity",
        r"total\s+stockholders\s+equity",
        r"total\s+shareholders\s+equity",
    ],
}


# Table fallback only. XBRL debt extraction below is preferred.
DEBT_COMPONENT_PATTERNS = {
    "commercialPaper": [
        r"^commercial\s+paper$",
    ],
    "shortTermDebt": [
        r"^short\s+term\s+debt\b",
        r"^current\s+debt\b",
        r"^current\s+portion\s+of\s+long\s+term\s+debt\b",
        r"^current\s+portion\s+of\s+term\s+debt\b",
        r"^current\s+portion\s+of\s+notes\s+payable\b",
        r"^notes\s+payable\s+current\b",
    ],
    "longTermDebt": [
        r"^long\s+term\s+debt\b",
        r"^long\s+term\s+debt\s+excluding\s+current\s+portion\b",
        r"^term\s+debt\s+non\s+current\b",
        r"^notes\s+payable\s+non\s+current\b",
        r"^unsecured\s+notes\b",
    ],
}


# ============================================================
# XBRL TAG MAPS
# ============================================================

# These are exact XBRL concepts commonly used for Apple / US GAAP balance sheets.
# We use XBRL first because the rendered table can have ambiguous repeated labels
# such as "Term debt" in both current and non-current liabilities.
XBRL_FIELD_TAGS = {
    "cashAndCashEquivalents": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ],
    "totalAssets": [
        "Assets",
    ],
    "totalLiabilities": [
        "Liabilities",
    ],
    "retainedEarnings": [
        "RetainedEarningsAccumulatedDeficit",
    ],
    "inventory": [
        "InventoryNet",
        "InventoryFinishedGoodsNetOfReserves",
        "InventoryRawMaterialsAndPurchasedPartsNetOfReserves",
        "InventoryWorkInProcessAndFinishedGoodsNetOfReserves",
    ],
    "propertyPlantEquipmentNet": [
        "PropertyPlantAndEquipmentNet",
        "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization",
    ],
    "totalStockholdersEquity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
}

# Debt bucket tags. We choose one value per bucket to avoid double counting aliases.
XBRL_DEBT_BUCKET_TAGS = {
    "commercialPaper": [
        "CommercialPaper",
    ],
    "shortTermBorrowings": [
        "ShortTermBorrowings",
        "ShortTermDebt",
    ],
    "currentDebt": [
        "LongTermDebtCurrent",
        "CurrentPortionOfLongTermDebt",
        "LongTermDebtAndFinanceLeaseObligationsCurrent",
        "FinanceLeaseLiabilityCurrent",
    ],
    "noncurrentDebt": [
        "LongTermDebtNoncurrent",
        "LongTermDebtAndFinanceLeaseObligationsNoncurrent",
        "FinanceLeaseLiabilityNoncurrent",
    ],
}

XBRL_TOTAL_DEBT_TAGS = [
    # Use these only if component buckets are not available.
    "DebtAndFinanceLeaseObligations",
    "LongTermDebtAndFinanceLeaseObligations",
    "DebtCurrent",
]


# ============================================================
# HELPERS
# ============================================================

def normalize_text(x) -> str:
    if pd.isna(x):
        return ""

    x = str(x)
    x = html.unescape(x)
    x = x.replace("\xa0", " ")
    x = x.replace("’", "'")
    x = x.replace("`", "'")
    x = x.replace("—", "-")
    x = x.replace("–", "-")
    re.sub(r"\s+", " ", x)
    x = re.sub(r"\s+", " ", x)
    return x.strip()


def normalize_label(x) -> str:
    x = normalize_text(x).lower()
    x = x.replace(":", "")
    x = re.sub(r"\s+", " ", x)
    return x.strip()


def label_matches(label: str, patterns: list[str]) -> bool:
    label = normalize_label(label)

    # Remove punctuation before matching, so patterns should generally be
    # punctuation-free.
    label = re.sub(r"[^a-z0-9\s']", " ", label)
    label = re.sub(r"\s+", " ", label).strip()

    return any(re.search(pattern, label) for pattern in patterns)


def parse_report_date_from_filename(path: Path) -> str | None:
    m = re.match(r"(\d{4}-\d{2}-\d{2})_", path.name)
    return m.group(1) if m else None


def parse_form_from_filename(path: Path) -> str | None:
    name = path.name.upper()
    if "_10-K_" in name:
        return "10-K"
    if "_10-Q_" in name:
        return "10-Q"
    return None


def parse_number(x):
    if x is None or pd.isna(x):
        return None

    s = normalize_text(x)

    if not re.search(r"\d", s):
        return None

    is_negative = "(" in s and ")" in s

    s = s.replace("$", "")
    s = s.replace(",", "")
    s = s.replace("(", "")
    s = s.replace(")", "")
    s = re.sub(r"[^0-9.\-]", "", s)

    if s in {"", "-", ".", "-."}:
        return None

    try:
        value = float(s)
    except ValueError:
        return None

    if is_negative:
        value = -value

    return value


def is_year_like(value: float) -> bool:
    return 1900 <= abs(value) <= 2100 and float(value).is_integer()


def get_attr(attrs_text: str, attr_name: str) -> str | None:
    # Works for attributes like name="us-gaap:Assets" or scale="6"
    m = re.search(rf'\b{re.escape(attr_name)}="([^"]*)"', attrs_text)
    return html.unescape(m.group(1)) if m else None


def local_name(qname: str | None) -> str:
    if not qname:
        return ""
    return qname.split(":")[-1]


def strip_tags(x: str) -> str:
    x = re.sub(r"<[^>]+>", "", x)
    return html.unescape(x).strip()


def parse_inline_xbrl_number(attrs_text: str, raw_text: str) -> float | None:
    value = parse_number(strip_tags(raw_text))
    if value is None:
        return None

    # Inline XBRL visible facts often store table values with scale="6",
    # e.g., text "91,807" and scale 6 means 91,807,000,000.
    scale_text = get_attr(attrs_text, "scale")
    if scale_text not in {None, ""}:
        try:
            value *= 10 ** int(scale_text)
        except ValueError:
            pass

    # Some facts use sign="-" rather than parentheses.
    if get_attr(attrs_text, "sign") == "-":
        value = -abs(value)

    return value


def parse_contexts_by_report_date(filing_text: str, report_date: str) -> set[str]:
    """
    Return context IDs with instant == report_date and no dimensional segment.
    We avoid dimensional contexts because debt footnote tables contain many
    instrument-level facts that should not be summed as company-level totals.
    """
    context_ids = set()

    context_re = re.compile(
        r"<xbrli:context\b(?P<attrs>[^>]*)>(?P<body>.*?)</xbrli:context>",
        flags=re.IGNORECASE | re.DOTALL,
    )

    for m in context_re.finditer(filing_text):
        attrs = m.group("attrs")
        body = m.group("body")
        ctx_id = get_attr(attrs, "id")
        if not ctx_id:
            continue

        instant_m = re.search(
            r"<xbrli:instant>\s*([^<]+)\s*</xbrli:instant>",
            body,
            flags=re.IGNORECASE,
        )
        if not instant_m:
            continue

        instant = instant_m.group(1).strip()
        has_dimension = bool(re.search(r"<xbrldi:explicitMember\b", body, flags=re.IGNORECASE))

        if instant == report_date and not has_dimension:
            context_ids.add(ctx_id)

    return context_ids


def decimals_precision_score(attrs_text: str) -> int:
    """
    Higher score = more precise fact. Example:
    decimals="-6" is more precise than decimals="-8".
    decimals="INF" is treated as very precise.
    """
    dec = get_attr(attrs_text, "decimals")
    if dec is None:
        return -999
    if dec.upper() == "INF":
        return 999
    try:
        return int(dec)
    except ValueError:
        return -999


def extract_xbrl_facts_for_date(path: Path, report_date: str) -> dict[str, list[dict]]:
    """
    Extract inline XBRL numeric facts for the report-date instant contexts.
    Returns local XBRL tag name -> list of {"value", "precision"} dictionaries.

    Some filings contain both exact statement-table facts and rounded footnote
    facts for the same XBRL tag. We keep precision so the exact value wins.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return {}

    valid_contexts = parse_contexts_by_report_date(text, report_date)
    if not valid_contexts:
        return {}

    facts: dict[str, list[dict]] = {}

    fact_re = re.compile(
        r"<ix:nonFraction\b(?P<attrs>[^>]*)>(?P<body>.*?)</ix:nonFraction>",
        flags=re.IGNORECASE | re.DOTALL,
    )

    for m in fact_re.finditer(text):
        attrs = m.group("attrs")
        body = m.group("body")

        ctx = get_attr(attrs, "contextRef")
        if ctx not in valid_contexts:
            continue

        tag = local_name(get_attr(attrs, "name"))
        if not tag:
            continue

        value = parse_inline_xbrl_number(attrs, body)
        if value is None:
            continue

        facts.setdefault(tag, []).append(
            {
                "value": value,
                "precision": decimals_precision_score(attrs),
            }
        )

    return facts


def first_available_fact(facts: dict[str, list[dict]], tags: list[str]) -> float | None:
    for tag in tags:
        vals = facts.get(tag)
        if vals:
            # Prefer the most precise duplicate fact. Tie-break by absolute value.
            best = sorted(vals, key=lambda d: (d["precision"], abs(d["value"])), reverse=True)[0]
            return best["value"]
    return None


def extract_fields_from_xbrl(path: Path, report_date: str) -> dict:
    facts = extract_xbrl_facts_for_date(path, report_date)
    extracted = {}

    if not facts:
        return extracted

    # Normal balance sheet fields.
    for field, tags in XBRL_FIELD_TAGS.items():
        value = first_available_fact(facts, tags)
        if value is not None:
            extracted[field] = value

    # Debt components.
    debt_buckets = {}
    for bucket, tags in XBRL_DEBT_BUCKET_TAGS.items():
        value = first_available_fact(facts, tags)
        if value is not None:
            debt_buckets[bucket] = value

    if debt_buckets:
        extracted["totalDebt"] = sum(debt_buckets.values())
        extracted["_debt_components"] = debt_buckets
    else:
        total_debt = first_available_fact(facts, XBRL_TOTAL_DEBT_TAGS)
        if total_debt is not None:
            extracted["totalDebt"] = total_debt
            extracted["_debt_components"] = {"totalDebtDirect": total_debt}

    return extracted


def is_numeric_cell(x) -> bool:
    v = parse_number(x)
    if v is None:
        return False
    if is_year_like(v):
        return False
    return True


def get_row_label_and_numeric_cells(row) -> tuple[str, list[tuple[int, float]]]:
    """
    Split one table row into:
    - label text before the first real numeric value
    - numeric cells with their column indices

    This avoids matching random text later in the row.
    """
    label_parts = []
    numeric_cells = []
    first_numeric_seen = False

    for col_idx, cell in enumerate(row.tolist()):
        value = parse_number(cell)

        if value is not None and not is_year_like(value):
            first_numeric_seen = True
            numeric_cells.append((col_idx, value))
        else:
            if not first_numeric_seen:
                text = normalize_text(cell)
                if text and text.lower() not in {"nan", "none"}:
                    label_parts.append(text)

    label = " ".join(label_parts)
    label = normalize_label(label)

    # Remove leading note markers or symbols.
    label = re.sub(r"^[\$\s\(\)\-]+", "", label).strip()

    return label, numeric_cells


def report_date_tokens(report_date: str) -> list[str]:
    """
    Generate date tokens used to identify the correct reporting-period column.
    Windows-safe version: avoids %-m and %-d.
    """
    dt = pd.to_datetime(report_date)

    month_full = dt.strftime("%B").lower()
    month_abbr = dt.strftime("%b").lower()
    day = str(dt.day)
    month = str(dt.month)
    year = str(dt.year)

    tokens = [
        year,
        month_full,
        month_abbr,
        f"{month_abbr}.",
        day,
        f"{month}/{day}/{year}",
        f"{dt.month:02d}/{dt.day:02d}/{year}",
        dt.strftime("%Y-%m-%d").lower(),
    ]

    return [t.lower() for t in tokens if t]


def score_columns_for_report_date(table: pd.DataFrame, report_date: str) -> dict[int, int]:
    """
    Try to find which numeric column corresponds to the report date.

    SEC balance sheets often have two columns:
    current period and prior fiscal year.
    This function looks at the first few header rows to see which column
    mentions the report date.
    """
    tokens = report_date_tokens(report_date)
    year = pd.to_datetime(report_date).strftime("%Y")
    month_abbr = pd.to_datetime(report_date).strftime("%b").lower()
    month_full = pd.to_datetime(report_date).strftime("%B").lower()

    scores = {}

    n_header_rows = min(8, len(table))

    for col in range(table.shape[1]):
        parts = []

        # Column name may contain header info.
        parts.append(normalize_text(table.columns[col]))

        # First few rows may contain date headers.
        for r in range(n_header_rows):
            parts.append(normalize_text(table.iloc[r, col]))

        text = " ".join(parts).lower()

        score = 0

        if year in text:
            score += 3

        if month_abbr in text or month_full in text:
            score += 2

        # Also reward exact-ish full tokens.
        for token in tokens:
            if token in text:
                score += 1

        scores[col] = score

    return scores


def choose_value_from_numeric_cells(
    numeric_cells: list[tuple[int, float]],
    col_scores: dict[int, int],
) -> float | None:
    if not numeric_cells:
        return None

    # Prefer column that best matches the report date.
    scored = [
        (col_scores.get(col_idx, 0), col_idx, value)
        for col_idx, value in numeric_cells
    ]

    scored = sorted(scored, key=lambda x: (x[0], x[1]), reverse=True)

    best_score, _, best_value = scored[0]

    if best_score > 0:
        return best_value

    # Fallback: use the rightmost numeric value.
    # This works for many old text filings where the current period is rightmost.
    return numeric_cells[-1][1]


# ============================================================
# TABLE READING
# ============================================================

def read_tables(path: Path) -> list[pd.DataFrame]:
    try:
        return pd.read_html(path, flavor="lxml")
    except Exception as e:
        print(f"[read_html failed] {path.name}: {e}")
        return []


def table_score(table: pd.DataFrame) -> int:
    text = " ".join(
        normalize_text(x).lower()
        for x in table.astype(str).fillna("").values.flatten()
    )

    score = 0

    for keyword in [
        "balance sheets",
        "cash and equivalents",
        "cash and cash equivalents",
        "total assets",
        "retained earnings",
        "stockholders",
        "shareholders",
        "property plant and equipment",
        "property and equipment",
        "total liabilities",
    ]:
        if keyword in text:
            score += 1

    return score


def choose_balance_sheet_table(tables: list[pd.DataFrame]) -> pd.DataFrame | None:
    if not tables:
        return None

    scored = [(table_score(t), i, t) for i, t in enumerate(tables)]
    scored = sorted(scored, key=lambda x: x[0], reverse=True)

    best_score, best_idx, best_table = scored[0]

    if best_score < 3:
        return None

    return best_table


# ============================================================
# TABLE EXTRACTION FALLBACK
# ============================================================

def table_has_explicit_debt_line(table: pd.DataFrame) -> bool:
    """Return True if the selected balance-sheet table has a real debt line.

    This is used to distinguish "missing because the company reports no debt"
    from "missing because parsing failed". We intentionally inspect row labels
    rather than searching the whole table text, because Apple uses labels such as
    "Non-current debt and equity investments" for investment assets, which are
    not borrowings.
    """
    for _, row in table.iterrows():
        label, numeric_cells = get_row_label_and_numeric_cells(row)
        if not label or not numeric_cells:
            continue

        # Exclude asset-side investment labels that contain the word "debt".
        if re.search(r"debt\s+and\s+equity\s+investments", normalize_label(label)):
            continue

        for patterns in DEBT_COMPONENT_PATTERNS.values():
            if label_matches(label, patterns):
                return True
    return False


def extract_fields_from_table(table: pd.DataFrame, report_date: str) -> dict:
    extracted = {}
    debt_components = {}

    col_scores = score_columns_for_report_date(table, report_date)

    previous_label = ""

    for _, row in table.iterrows():
        label, numeric_cells = get_row_label_and_numeric_cells(row)

        if not label:
            continue

        # Normal fields.
        if numeric_cells:
            for field, patterns in FIELD_PATTERNS.items():
                if field in extracted:
                    continue

                if label_matches(label, patterns):
                    value = choose_value_from_numeric_cells(numeric_cells, col_scores)

                    if value is not None:
                        extracted[field] = value * VALUE_MULTIPLIER

        # Debt components table fallback.
        if numeric_cells:
            for debt_field, patterns in DEBT_COMPONENT_PATTERNS.items():
                if debt_field in debt_components:
                    continue

                if label_matches(label, patterns):
                    value = choose_value_from_numeric_cells(numeric_cells, col_scores)

                    if value is not None:
                        debt_components[debt_field] = value * VALUE_MULTIPLIER

            # Apple sometimes has repeated label "Term debt" under current and
            # non-current liability sections. Use surrounding row text as a hint.
            if label_matches(label, [r"^term\s+debt$"]):
                value = choose_value_from_numeric_cells(numeric_cells, col_scores)
                if value is not None:
                    if "current liabilities" in previous_label and "shortTermDebt" not in debt_components:
                        debt_components["shortTermDebt"] = value * VALUE_MULTIPLIER
                    elif "non current liabilities" in previous_label and "longTermDebt" not in debt_components:
                        debt_components["longTermDebt"] = value * VALUE_MULTIPLIER
                    elif value * VALUE_MULTIPLIER > 20_000_000_000 and "longTermDebt" not in debt_components:
                        debt_components["longTermDebt"] = value * VALUE_MULTIPLIER
                    elif "shortTermDebt" not in debt_components:
                        debt_components["shortTermDebt"] = value * VALUE_MULTIPLIER

        previous_label = label

    # Build total debt only from explicit debt components.
    if debt_components:
        extracted["totalDebt"] = sum(debt_components.values())
        extracted["_debt_components"] = debt_components

    return extracted




# ============================================================
# PLAIN-TEXT SEC TABLE FALLBACK
# ============================================================

def clean_plain_text_line(line: str) -> str:
    line = html.unescape(line)
    line = line.replace("\xa0", " ")
    line = line.replace("&#151;", "-").replace("&mdash;", "-")
    return line.rstrip("\n")


def strip_inline_tags_keep_text(line: str) -> str:
    """Remove simple HTML tags but keep the visible fixed-width text."""
    line = re.sub(r"<[^>]+>", " ", line)
    line = html.unescape(line)
    line = line.replace("\xa0", " ")
    line = re.sub(r"\s+", " ", line)
    return line.rstrip()


def extract_balance_sheet_text_block(text: str) -> str | None:
    lines = [clean_plain_text_line(x) for x in text.splitlines()]
    title_re = re.compile(r"balance\s+sheets?", re.I)
    candidates = []

    for i, line in enumerate(lines):
        window_title = " ".join(lines[i:i + 4])
        if not title_re.search(window_title):
            continue
        lookahead = "\n".join(lines[i:i + 100]).lower()
        score = 0
        for keyword in [
            "cash and cash equivalents", "total assets", "current liabilities",
            "total liabilities", "retained earnings", "shareholders' equity",
            "stockholders' equity", "property", "inventories", "inventory",
        ]:
            if keyword in lookahead:
                score += 1
        prev = "\n".join(lines[max(0, i - 20):i]).lower()
        if "index" in prev or "table of contents" in prev:
            score -= 3
        candidates.append((score, i))

    if not candidates:
        return None

    candidates = sorted(candidates, key=lambda x: (x[0], x[1]), reverse=True)
    start_idx = candidates[0][1]

    end_idx = len(lines)
    end_re = re.compile(r"statements?\s+of\s+cash\s+flows?|cash\s+flows?|statements?\s+of\s+operations|statements?\s+of\s+income|notes\s+to", re.I)
    for j in range(start_idx + 20, len(lines)):
        if end_re.search(lines[j]):
            end_idx = j
            break

    return "\n".join(lines[start_idx:end_idx])


def plain_text_date_variants(report_date: str) -> list[str]:
    dt = pd.to_datetime(report_date)
    month_full = dt.strftime("%B")
    month_abbr = dt.strftime("%b")
    day_no_zero = str(dt.day)
    day_zero = dt.strftime("%d")
    year = str(dt.year)
    return [
        f"{month_full} {day_no_zero}, {year}",
        f"{month_full} {day_zero}, {year}",
        f"{month_abbr}. {day_no_zero}, {year}",
        f"{month_abbr}. {day_zero}, {year}",
        f"{month_abbr} {day_no_zero}, {year}",
        f"{month_abbr} {day_zero}, {year}",
        report_date,
        year,
    ]


def find_plain_text_target_position(block: str, report_date: str) -> int | None:
    lines = block.splitlines()[:45]
    variants = plain_text_date_variants(report_date)
    report_year = str(pd.to_datetime(report_date).year)
    candidates: list[tuple[int, int]] = []

    for line in lines:
        search_lines = [html.unescape(line).lower(), strip_inline_tags_keep_text(line).lower()]
        for variant in variants:
            if not variant or variant == report_year:
                continue
            for search_line in search_lines:
                pos = search_line.find(variant.lower())
                if pos >= 0:
                    candidates.append((30, pos + len(variant) // 2))

    for line in lines:
        clean_low = strip_inline_tags_keep_text(line).lower()
        for m in re.finditer(rf"\b{re.escape(report_year)}\b", clean_low):
            candidates.append((5, m.start() + 2))

    if candidates:
        candidates = sorted(candidates, key=lambda x: (x[0], -x[1]), reverse=True)
        return candidates[0][1]
    return None


def parse_plain_text_number_token(token: str) -> float | None:
    token = normalize_text(token)
    if not token:
        return None
    if token in {"-", "--", "—"}:
        return 0.0
    if not re.search(r"\d", token):
        return None
    is_negative = "(" in token and ")" in token
    s = token.replace("$", "").replace(",", "").replace("(", "").replace(")", "")
    s = re.sub(r"[^0-9.\-]", "", s)
    if s in {"", "-", ".", "-."}:
        return None
    try:
        value = float(s)
    except ValueError:
        return None
    return -abs(value) if is_negative else value


def extract_number_spans_from_plain_line(line: str) -> list[tuple[int, int, float]]:
    token_re = re.compile(
        r"(?<![A-Za-z])(?:\$\s*)?(?:\([0-9][0-9,]*(?:\.[0-9]+)?\)|[0-9][0-9,]*(?:\.[0-9]+)?|--|-)(?![A-Za-z])"
    )
    spans = []
    for m in token_re.finditer(line):
        raw = m.group(0).strip()
        value = parse_plain_text_number_token(raw)
        if value is None:
            continue
        # Do not drop year-like values here. Old Apple balance sheets can have
        # legitimate values such as retained earnings = 2,090. Header/date rows
        # are ignored later because their labels do not match target fields.
        spans.append((m.start(), m.end(), value))
    return spans


def row_label_from_plain_line(line: str, spans: list[tuple[int, int, float]]) -> str:
    if not spans:
        return ""
    first_start = spans[0][0]
    label = line[:first_start]
    label = strip_inline_tags_keep_text(label)
    label = re.sub(r"^[\s\-]+", "", label)
    label = re.sub(r"[\s\-]+$", "", label)
    return normalize_label(label)


def choose_plain_text_value(spans: list[tuple[int, int, float]], target_pos: int | None) -> float | None:
    if not spans:
        return None
    if target_pos is not None:
        return min(spans, key=lambda x: abs(((x[0] + x[1]) // 2) - target_pos))[2]
    return spans[0][2]


def plain_text_has_explicit_debt_line(block: str) -> bool:
    for raw_line in block.splitlines():
        line = strip_inline_tags_keep_text(clean_plain_text_line(raw_line))
        spans = extract_number_spans_from_plain_line(line)
        if not spans:
            continue
        label = row_label_from_plain_line(line, spans)
        if not label:
            continue
        if re.search(r"debt\s+and\s+equity\s+investments", normalize_label(label)):
            continue
        for patterns in DEBT_COMPONENT_PATTERNS.values():
            if label_matches(label, patterns):
                return True
    return False


def extract_fields_from_plain_text(path: Path, report_date: str) -> dict:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return {}

    block = extract_balance_sheet_text_block(text)
    if not block:
        return {}

    target_pos = find_plain_text_target_position(block, report_date)
    extracted: dict[str, float] = {}
    debt_components: dict[str, float] = {}

    for raw_line in block.splitlines():
        line = strip_inline_tags_keep_text(clean_plain_text_line(raw_line))
        if not line.strip():
            continue
        if re.match(r"^[\-=]{5,}$", line.strip()):
            continue

        spans = extract_number_spans_from_plain_line(line)
        if not spans:
            continue

        label = row_label_from_plain_line(line, spans)
        if not label:
            continue

        is_debt_investment_asset = bool(re.search(r"debt\s+and\s+equity\s+investments", normalize_label(label)))

        for field, patterns in FIELD_PATTERNS.items():
            if field in extracted:
                continue
            if label_matches(label, patterns):
                value = choose_plain_text_value(spans, target_pos)
                if value is not None:
                    extracted[field] = value * VALUE_MULTIPLIER
                break

        if not is_debt_investment_asset:
            for debt_field, patterns in DEBT_COMPONENT_PATTERNS.items():
                if debt_field in debt_components:
                    continue
                if label_matches(label, patterns):
                    value = choose_plain_text_value(spans, target_pos)
                    if value is not None:
                        debt_components[debt_field] = value * VALUE_MULTIPLIER
                    break

    if debt_components:
        extracted["totalDebt"] = sum(debt_components.values())
        extracted["_debt_components"] = debt_components
    elif extracted.get("totalLiabilities") is not None and not plain_text_has_explicit_debt_line(block):
        extracted["totalDebt"] = 0.0
        extracted["_debt_components"] = {"noDebtLineOnParsedPlainTextBalanceSheet": 0.0}

    return extracted


def extract_balance_sheet_from_filing(path: Path) -> dict:
    report_date = parse_report_date_from_filename(path)

    result = {
        "date": report_date,
        "totalStockholdersEquity": pd.NA,
        "totalAssets": pd.NA,
        "totalLiabilities": pd.NA,
        "cashAndCashEquivalents": pd.NA,
        "totalDebt": pd.NA,
        "netDebt": pd.NA,
        "retainedEarnings": pd.NA,
        "inventory": pd.NA,
        "propertyPlantEquipmentNet": pd.NA,
    }

    if report_date is None:
        return result

    # ------------------------------------------------------------
    # 1) Prefer XBRL facts.
    # ------------------------------------------------------------
    extracted_xbrl = extract_fields_from_xbrl(path, report_date)
    debt_components = extracted_xbrl.pop("_debt_components", None)

    for key, value in extracted_xbrl.items():
        if key in result:
            result[key] = value

    # ------------------------------------------------------------
    # 2) Table fallback for anything XBRL did not find.
    # ------------------------------------------------------------
    tables = read_tables(path)
    table = choose_balance_sheet_table(tables)

    if table is None:
        # Plain-text SEC fallback for old .txt filings where pandas.read_html()
        # returns no usable balance-sheet table.
        extracted_plain = extract_fields_from_plain_text(path, report_date)
        plain_debt_components = extracted_plain.pop("_debt_components", None) if extracted_plain else None

        if extracted_plain:
            print(f"[plain-text balance-sheet fallback used] {path.name}")
            for key, value in extracted_plain.items():
                if key in result and pd.isna(result[key]):
                    result[key] = value
            if debt_components is None and plain_debt_components is not None:
                debt_components = plain_debt_components
        else:
            print(f"[no balance sheet table found] {path.name}")
    else:
        extracted_table = extract_fields_from_table(table, report_date)
        table_debt_components = extracted_table.pop("_debt_components", None)

        for key, value in extracted_table.items():
            if key in result and pd.isna(result[key]):
                result[key] = value

        if debt_components is None and table_debt_components is not None:
            debt_components = table_debt_components

        # If the balance sheet was parsed successfully and contains no explicit
        # debt line, treat missing debt as true zero. This handles Apple years
        # such as 2004-2013Q1, where the liabilities section has no current debt,
        # long-term debt, notes payable, or commercial paper line.
        #
        # We do this only after checking the selected balance-sheet table for
        # debt labels, so older rows with explicit debt lines are not zero-filled.
        if (
            debt_components is None
            and pd.isna(result.get("totalDebt"))
            and table is not None
            and pd.notna(result.get("totalLiabilities"))
            and not table_has_explicit_debt_line(table)
        ):
            result["totalDebt"] = 0.0
            debt_components = {"noDebtLineOnParsedBalanceSheet": 0.0}

    if debt_components:
        print(f"Debt components used: {debt_components}")

    # ------------------------------------------------------------
    # Accounting identity repair / validation
    # Assets = Liabilities + Equity
    # ------------------------------------------------------------

    if (
        pd.notna(result["totalAssets"])
        and pd.notna(result["totalStockholdersEquity"])
    ):
        implied_liabilities = (
            result["totalAssets"] - result["totalStockholdersEquity"]
        )

        # If liabilities are missing, or if extracted liabilities do not
        # satisfy the accounting identity, use implied liabilities.
        if (
            pd.isna(result["totalLiabilities"])
            or abs(result["totalLiabilities"] - implied_liabilities) > 1_000_000
        ):
            result["totalLiabilities"] = implied_liabilities

    elif (
        pd.isna(result["totalAssets"])
        and pd.notna(result["totalLiabilities"])
        and pd.notna(result["totalStockholdersEquity"])
    ):
        result["totalAssets"] = (
            result["totalLiabilities"] + result["totalStockholdersEquity"]
        )

    elif (
        pd.isna(result["totalStockholdersEquity"])
        and pd.notna(result["totalAssets"])
        and pd.notna(result["totalLiabilities"])
    ):
        result["totalStockholdersEquity"] = (
            result["totalAssets"] - result["totalLiabilities"]
        )

    if (
        pd.notna(result.get("totalDebt"))
        and pd.notna(result.get("cashAndCashEquivalents"))
    ):
        result["netDebt"] = result["totalDebt"] - result["cashAndCashEquivalents"]

    return result


# ============================================================
# FOLDER LEVEL
# ============================================================

def list_filing_files(filings_dir: Path) -> list[Path]:
    files = []

    for suffix in ["*.htm", "*.html", "*.txt"]:
        files.extend(filings_dir.glob(suffix))

    return sorted(set(files))


def extract_balance_sheets_from_folder(filings_dir: Path) -> pd.DataFrame:
    files = list_filing_files(filings_dir)
    print(f"Found {len(files)} filing files in {filings_dir}")

    rows = []

    for path in files:
        print(f"\nParsing: {path.name}")

        row = extract_balance_sheet_from_filing(path)
        row["source_file"] = path.name

        extracted_fields = [
            col for col in OUTPUT_COLUMNS
            if col != "date" and pd.notna(row.get(col))
        ]

        print(f"Extracted fields: {extracted_fields}")

        rows.append(row)

    df = pd.DataFrame(rows)

    if df.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS + ["source_file"])

    df = df[df["date"].notna()].copy()
    df = df[OUTPUT_COLUMNS + ["source_file"]]
    df = df.sort_values("date", ascending=False).reset_index(drop=True)

    return df


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    df = extract_balance_sheets_from_folder(FILINGS_DIR)

    # ------------------------------------------------------------
    # Compute net debt at dataframe level
    # netDebt = totalDebt - cashAndCashEquivalents
    #
    # Missing totalDebt is zero-filled earlier only when the parsed
    # balance-sheet table has no explicit debt line. This avoids treating
    # old Apple rows with current/long-term debt as zero.
    # ------------------------------------------------------------

    if "totalDebt" in df.columns and "cashAndCashEquivalents" in df.columns:
        mask = (
            df["totalDebt"].notna()
            & df["cashAndCashEquivalents"].notna()
        )

        df.loc[mask, "netDebt"] = (
            df.loc[mask, "totalDebt"]
            - df.loc[mask, "cashAndCashEquivalents"]
        )

    df.to_csv(OUTPUT_CSV, index=False)

    print("\nDone.")
    print(f"Saved to: {OUTPUT_CSV}")

    print("\nMissing counts:")
    print(df[OUTPUT_COLUMNS].isna().sum())

    print("\nPreview:")
    print(df.head(20).to_string(index=False))
