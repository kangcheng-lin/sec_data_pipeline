from pathlib import Path
import re
import html
import pandas as pd


# ============================================================
# CONFIG
# ============================================================

FILINGS_DIR = Path("sec_filings/AAPL")
OUTPUT_CSV = Path("AAPL_income_statement_direct_from_filings.csv")

# Rendered SEC statement tables usually show values in millions.
# Inline XBRL facts are handled separately through scale/decimals.
VALUE_MULTIPLIER = 1_000_000

# For 10-Q income statements, prefer the three-month period when available.
PREFER_QUARTERLY_FOR_10Q = True

# Directly reported income-statement fields only.
# Derived / structurally inconsistent fields were removed: grossProfit,
# operatingExpenses, totalOtherIncomeExpensesNet, EBIT, EBITDA, costAndExpenses,
# netInterestIncome, discontinued ops, deductions, etc.
# Selling/marketing, G&A, and combined SG&A were removed because
# they are not needed for the Value strategy and reporting formats vary.
OUTPUT_COLUMNS = [
    "date",
    "revenue",
    "costOfRevenue",
    "researchAndDevelopmentExpenses",
    "operatingIncome",
    "incomeBeforeTax",
    "incomeTaxExpense",
    "netIncome",
    "bottomLineNetIncome",
    "eps",
    "epsDiluted",
    "weightedAverageShsOut",
    "weightedAverageShsOutDil",
]

EPS_FIELDS = {"eps", "epsDiluted"}
SHARE_FIELDS = {"weightedAverageShsOut", "weightedAverageShsOutDil"}
PER_SHARE_OR_SHARE_FIELDS = EPS_FIELDS | SHARE_FIELDS


# ============================================================
# XBRL TAG MAPS
# ============================================================

XBRL_FIELD_TAGS = {
    "revenue": [
        ("RevenueFromContractWithCustomerExcludingAssessedTax", 1),
        ("SalesRevenueNet", 1),
        ("Revenues", 1),
    ],
    "costOfRevenue": [
        ("CostOfRevenue", 1),
        ("CostOfGoodsAndServicesSold", 1),
    ],
    "researchAndDevelopmentExpenses": [
        ("ResearchAndDevelopmentExpense", 1),
    ],
    "operatingIncome": [
        ("OperatingIncomeLoss", 1),
    ],
    "incomeBeforeTax": [
        ("IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest", 1),
        ("IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments", 1),
        ("IncomeLossFromContinuingOperationsBeforeIncomeTaxes", 1),
    ],
    "incomeTaxExpense": [
        ("IncomeTaxExpenseBenefit", 1),
    ],
    "netIncome": [
        ("NetIncomeLoss", 1),
        ("ProfitLoss", 1),
    ],
    # Direct alias for the reported bottom-line net income/loss.
    "bottomLineNetIncome": [
        ("NetIncomeLoss", 1),
        ("ProfitLoss", 1),
    ],
    "eps": [
        ("EarningsPerShareBasic", 1),
    ],
    "epsDiluted": [
        ("EarningsPerShareDiluted", 1),
    ],
    "weightedAverageShsOut": [
        ("WeightedAverageNumberOfSharesOutstandingBasic", 1),
        ("WeightedAverageNumberOfShareOutstandingBasicAndDiluted", 1),
    ],
    "weightedAverageShsOutDil": [
        ("WeightedAverageNumberOfDilutedSharesOutstanding", 1),
        ("WeightedAverageNumberOfSharesOutstandingDiluted", 1),
    ],
}


# ============================================================
# TABLE FALLBACK LABEL PATTERNS
# ============================================================

FIELD_PATTERNS = {
    "revenue": [
        r"^revenue$",
        r"^revenues$",
        r"^total\s+revenue$",
        r"^net\s+sales$",
        r"^total\s+net\s+sales$",
        r"^sales$",
    ],
    "costOfRevenue": [
        r"^cost\s+of\s+revenue\b",
        r"^total\s+cost\s+of\s+revenue\b",
        r"^cost\s+of\s+sales\b",
        r"^total\s+cost\s+of\s+sales\b",
        r"^cost\s+of\s+goods",
    ],
    "researchAndDevelopmentExpenses": [
        r"^research\s+and\s+development\b",
        r"research\s+and\s+development\s+expense",
    ],
    "operatingIncome": [
        r"^operating\s+income$",
        r"^operating\s+loss$",
        r"^operating\s+income\s+loss$",
        r"^income\s+from\s+operations$",
    ],
    "incomeBeforeTax": [
        r"^income\s+before\s+income\s+taxes$",
        r"^income\s+before\s+taxes$",
        r"^income\s+loss\s+before\s+income\s+taxes$",
        r"^income\s+before\s+provision\s+for\s+income\s+taxes$",
        r"^income\s+loss\s+before\s+provision\s+for\s+income\s+taxes$",
        r"^income\s+loss\s+before\s+provision\s+for\s+benefit\s+from\s+income\s+taxes$",
        r"^income\s+loss\s+before\s+provision\s+benefit\s+from\s+income\s+taxes$",
        r"^income\s+loss\s+before\s+provision\s+benefit\s+for\s+income\s+taxes$",
        r"^income\s+before\s+provision\s+for\s+benefit\s+from\s+income\s+taxes$",
    ],
    "incomeTaxExpense": [
        r"^provision\s+for\s+income\s+taxes$",
        r"^benefit\s+for\s+income\s+taxes$",
        r"^provision\s+benefit\s+for\s+income\s+taxes$",
        r"^provision\s+for\s+benefit\s+from\s+income\s+taxes$",
        r"^provision\s+benefit\s+from\s+income\s+taxes$",
        r"^income\s+tax\s+expense$",
    ],
    "netIncome": [r"^net\s+income$", r"^net\s+loss$", r"^net\s+income\s+loss$"],
}



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
    x = re.sub(r"\s+", " ", x)
    return x.strip()


def normalize_label(x) -> str:
    x = normalize_text(x).lower()
    x = x.replace(":", "")
    x = re.sub(r"[^a-z0-9\s']", " ", x)
    x = re.sub(r"\s+", " ", x)
    return x.strip()


def label_matches(label: str, patterns: list[str]) -> bool:
    normalized = normalize_label(label)
    # Labels may contain repeated/continued pieces separated by a pipe before
    # normalization; also test each original piece individually.
    pieces = [normalized]
    for part in str(label).split("|"):
        part_norm = normalize_label(part)
        if part_norm and part_norm not in pieces:
            pieces.append(part_norm)
    return any(re.search(pattern, piece) for piece in pieces for pattern in patterns)


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


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def parse_number(x):
    if x is None or pd.isna(x):
        return None
    s = normalize_text(x)
    if not re.search(r"\d", s):
        return None
    is_negative = "(" in s and ")" in s
    s = s.replace("$", "").replace(",", "").replace("(", "").replace(")", "")
    s = re.sub(r"[^0-9.\-]", "", s)
    if s in {"", "-", ".", "-."}:
        return None
    try:
        value = float(s)
    except ValueError:
        return None
    if is_negative:
        value = -abs(value)
    return value


def parse_table_number_from_neighbors(cells: list[str], idx: int) -> float | None:
    """Handle SEC tables where '$', '(' and ')' are split across cells."""
    s = normalize_text(cells[idx])
    if not re.search(r"\d", s):
        return None
    # Do not parse row-label cells such as "Cost of sales (1)" as values.
    if cell_has_letters(s):
        return None
    left = normalize_text(cells[idx - 1]) if idx > 0 else ""
    right = normalize_text(cells[idx + 1]) if idx + 1 < len(cells) else ""
    is_negative = (
        "(" in s or ")" in s or left.strip() == "(" or right.strip() == ")" or right.strip().startswith(")")
    )
    cleaned = s.replace("$", "").replace(",", "").replace("(", "").replace(")", "")
    cleaned = re.sub(r"[^0-9.\-]", "", cleaned)
    if cleaned in {"", "-", ".", "-."}:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return -abs(value) if is_negative else value


def cell_has_letters(x: str) -> bool:
    return bool(re.search(r"[A-Za-z]", normalize_text(x)))


def get_attr(attrs_text: str, attr_name: str) -> str | None:
    m = re.search(rf'\b{re.escape(attr_name)}="([^"]*)"', attrs_text)
    return html.unescape(m.group(1)) if m else None


def local_name(qname: str | None) -> str:
    if not qname:
        return ""
    return qname.split(":")[-1]


def strip_tags(x: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", x)).strip()


def parse_inline_xbrl_number(attrs_text: str, raw_text: str) -> float | None:
    value = parse_number(strip_tags(raw_text))
    if value is None:
        return None
    scale_text = get_attr(attrs_text, "scale")
    if scale_text not in {None, ""}:
        try:
            value *= 10 ** int(scale_text)
        except ValueError:
            pass
    if get_attr(attrs_text, "sign") == "-":
        value = -abs(value)
    return value


def decimals_precision_score(attrs_text: str) -> int:
    dec = get_attr(attrs_text, "decimals")
    if dec is None:
        return -999
    if dec.upper() == "INF":
        return 999
    try:
        return int(dec)
    except ValueError:
        return -999


# ============================================================
# XBRL EXTRACTION
# ============================================================

def parse_duration_contexts(filing_text: str, report_date: str) -> dict[str, dict]:
    contexts = {}
    context_re = re.compile(r"<xbrli:context\b(?P<attrs>[^>]*)>(?P<body>.*?)</xbrli:context>", re.I | re.S)
    for m in context_re.finditer(filing_text):
        ctx_id = get_attr(m.group("attrs"), "id")
        body = m.group("body")
        if not ctx_id:
            continue
        if re.search(r"<xbrldi:explicitMember\b", body, re.I):
            continue
        start_m = re.search(r"<xbrli:startDate>\s*([^<]+)\s*</xbrli:startDate>", body, re.I)
        end_m = re.search(r"<xbrli:endDate>\s*([^<]+)\s*</xbrli:endDate>", body, re.I)
        if not start_m or not end_m:
            continue
        start = start_m.group(1).strip()
        end = end_m.group(1).strip()
        if end != report_date:
            continue
        try:
            days = (pd.to_datetime(end) - pd.to_datetime(start)).days + 1
        except Exception:
            continue
        contexts[ctx_id] = {"start": start, "end": end, "days": days}
    return contexts


def target_duration_days(form: str | None) -> int:
    if form == "10-K":
        return 365
    return 90 if PREFER_QUARTERLY_FOR_10Q else 270


def context_duration_score(days: int, form: str | None) -> int:
    return -abs(days - target_duration_days(form))


def extract_xbrl_duration_facts(path: Path, report_date: str, form: str | None) -> dict[str, list[dict]]:
    try:
        text = read_text(path)
    except Exception:
        return {}
    contexts = parse_duration_contexts(text, report_date)
    if not contexts:
        return {}
    facts = {}
    fact_re = re.compile(r"<ix:nonFraction\b(?P<attrs>[^>]*)>(?P<body>.*?)</ix:nonFraction>", re.I | re.S)
    for m in fact_re.finditer(text):
        attrs = m.group("attrs")
        ctx = get_attr(attrs, "contextRef")
        if ctx not in contexts:
            continue
        tag = local_name(get_attr(attrs, "name"))
        value = parse_inline_xbrl_number(attrs, m.group("body"))
        if not tag or value is None:
            continue
        days = contexts[ctx]["days"]
        facts.setdefault(tag, []).append({
            "value": value,
            "precision": decimals_precision_score(attrs),
            "days": days,
            "duration_score": context_duration_score(days, form),
        })
    return facts


def first_available_duration_fact(facts: dict[str, list[dict]], tags_with_sign: list[tuple[str, int]]) -> float | None:
    for tag, sign in tags_with_sign:
        vals = facts.get(tag)
        if not vals:
            continue
        best = sorted(vals, key=lambda d: (d["duration_score"], d["precision"], abs(d["value"])), reverse=True)[0]
        return best["value"] * sign
    return None


def extract_fields_from_xbrl(path: Path, report_date: str, form: str | None) -> dict:
    facts = extract_xbrl_duration_facts(path, report_date, form)
    extracted = {}
    for field, tags in XBRL_FIELD_TAGS.items():
        value = first_available_duration_fact(facts, tags)
        if value is not None:
            extracted[field] = value
    return extracted


# ============================================================
# HTML TABLE FALLBACK
# ============================================================

def read_tables(path: Path) -> list[pd.DataFrame]:
    try:
        return pd.read_html(path, flavor="lxml")
    except Exception as e:
        print(f"[read_html failed] {path.name}: {e}")
        return []


def table_score(table: pd.DataFrame) -> int:
    n_rows, n_cols = table.shape
    text = " ".join(normalize_text(x).lower() for x in table.astype(str).fillna("").values.flatten())
    label_text = normalize_label(text)
    score = 0

    if n_rows < 8:
        score -= 8

    # Strong identifiers for the real income statement.
    for keyword in [
        "income statements", "statement of income", "statements of operations", "statement of operations",
        "consolidated statements of income", "consolidated statements of operations",
    ]:
        if keyword in text:
            score += 8

    core_patterns = [
        r"total\s+revenue", r"net\s+sales", r"cost\s+of\s+revenue", r"total\s+cost\s+of\s+revenue", r"cost\s+of\s+sales",
        r"gross\s+margin", r"gross\s+profit", r"research\s+and\s+development",
        r"sales\s+and\s+marketing", r"selling\s+and\s+marketing", r"general\s+and\s+administrative", r"selling\s+general\s+and\s+administrative",
        r"operating\s+income", r"income\s+before\s+income\s+taxes", r"income\s+before\s+provision\s+for\s+income\s+taxes", r"provision\s+for\s+income\s+taxes",
        r"net\s+income", r"earnings\s+per\s+share", r"weighted\s+average\s+shares",
    ]
    hits = 0
    for pattern in core_patterns:
        if re.search(pattern, label_text):
            hits += 1
            score += 3
    if hits >= 7:
        score += 15

    # Penalize selected financial data / MD&A summary tables.
    if "selected financial data" in text:
        score -= 10
    if "percentage change" in text:
        score -= 8
    if "cash dividends declared per share" in text and hits < 8:
        score -= 6

    # Penalize obvious non-income statement tables.
    for bad in ["balance sheets", "cash flows", "stockholders equity", "comprehensive income"]:
        if bad in text:
            score -= 6

    return score


def choose_income_statement_tables(tables: list[pd.DataFrame]) -> list[pd.DataFrame]:
    if not tables:
        return []
    scored = sorted([(table_score(t), i, t) for i, t in enumerate(tables)], key=lambda x: x[0], reverse=True)
    candidates = [(s, i, t) for s, i, t in scored if s >= 8 and t.shape[0] >= 8]
    if not candidates:
        return []
    # Keep several high-quality candidates. Some filers place EPS/share rows
    # in a separate table from the main income statement. We fill missing
    # fields only, so this is safer than requiring every table to be close
    # to the single best score.
    candidates = candidates[:5]
    return [t for _, _, t in candidates]


def report_date_tokens(report_date: str) -> list[str]:
    dt = pd.to_datetime(report_date)
    return [
        str(dt.year), dt.strftime("%B").lower(), dt.strftime("%b").lower(),
        f"{dt.strftime('%b').lower()}.", str(dt.day), f"{dt.month}/{dt.day}/{dt.year}",
        f"{dt.month:02d}/{dt.day:02d}/{dt.year}", dt.strftime("%Y-%m-%d").lower(),
    ]


def score_columns_for_report_date(table: pd.DataFrame, report_date: str, form: str | None) -> dict[int, int]:
    tokens = report_date_tokens(report_date)
    dt = pd.to_datetime(report_date)
    report_year = str(dt.year)
    report_month_full = dt.strftime("%B").lower()
    report_month_abbr = dt.strftime("%b").lower()

    scores = {}
    n_header_rows = min(12, len(table))
    for col in range(table.shape[1]):
        own_parts = [normalize_text(table.columns[col])]
        neighbor_parts = []
        for r in range(n_header_rows):
            own_parts.append(normalize_text(table.iloc[r, col]))
            for c in range(max(0, col - 1), min(table.shape[1], col + 2)):
                if c != col:
                    neighbor_parts.append(normalize_text(table.iloc[r, c]))
        own_text = " ".join(own_parts).lower()
        neighbor_text = " ".join(neighbor_parts).lower()
        combined = own_text + " " + neighbor_text

        score = 0
        if report_year in own_text:
            score += 10
        if report_month_full in own_text or report_month_abbr in own_text:
            score += 5
        for token in tokens:
            if token in own_text:
                score += 3
        # Weak neighbor evidence for split date headers.
        if report_year in neighbor_text:
            score += 2
        if report_month_full in neighbor_text or report_month_abbr in neighbor_text:
            score += 1
        for token in tokens:
            if token in neighbor_text:
                score += 1

        if form == "10-Q":
            if "three months" in combined or "3 months" in combined:
                score += 10 if PREFER_QUARTERLY_FOR_10Q else 2
            if "six months" in combined or "6 months" in combined:
                score += 5
            if "nine months" in combined or "9 months" in combined:
                score += 4
        elif form == "10-K":
            if "year ended" in combined or "twelve months" in combined or "12 months" in combined:
                score += 8
        scores[col] = score
    return scores


def get_row_label_and_numeric_cells(row) -> tuple[str, list[tuple[int, float]]]:
    cells = [normalize_text(x) for x in row.tolist()]
    label_idx = None
    for i, cell in enumerate(cells):
        if not cell or cell.lower() in {"nan", "none"}:
            continue
        if cell_has_letters(cell):
            label_idx = i
            break
    if label_idx is None:
        return "", []

    label = normalize_label(cells[label_idx])

    # Append continuation text until values begin.
    j = label_idx + 1
    while j < len(cells):
        cell = cells[j]
        if not cell or cell.lower() in {"nan", "none"}:
            j += 1
            continue
        if re.search(r"\d", cell):
            break
        if cell_has_letters(cell):
            next_label = normalize_label(cell)
            # Old SEC tables often repeat the row label across several
            # adjacent columns before numeric values begin. Do not turn
            # "Net sales" into "Net sales Net sales ...".
            if next_label and next_label not in label.split(" | "):
                if next_label != label:
                    label = normalize_label(label + " | " + next_label)
        j += 1

    numeric_cells = []
    for col_idx in range(label_idx + 1, len(cells)):
        value = parse_table_number_from_neighbors(cells, col_idx)
        if value is not None:
            numeric_cells.append((col_idx, value))
    return label, numeric_cells


def choose_value_from_numeric_cells(numeric_cells: list[tuple[int, float]], col_scores: dict[int, int]) -> float | None:
    if not numeric_cells:
        return None
    scored = [(col_scores.get(col, 0), col, value) for col, value in numeric_cells]
    max_score = max(s for s, _, _ in scored)
    if max_score > 0:
        best = [item for item in scored if item[0] == max_score]
        # Current periods usually appear before prior periods once headers identify the current year/period.
        best = sorted(best, key=lambda x: x[1])
        return best[0][2]
    # Fallback: rightmost numeric value in old tables.
    return numeric_cells[-1][1]


def infer_section(label: str, current_section: str | None) -> str | None:
    l = normalize_label(label)
    # Check share-count section before EPS because Apple uses
    # "Shares used in computing earnings per share".
    if re.search(r"weighted\s+average\s+shares", l) or re.search(r"average\s+shares\s+outstanding", l) or re.search(r"shares\s+used\s+in\s+computing", l):
        return "shares"
    if re.search(r"earnings\s+per\s+(common\s+)?share", l) or re.search(r"earnings\s+loss\s+per\s+(common\s+)?share", l):
        return "eps"
    return current_section


def section_field(label: str, section: str | None) -> str | None:
    l = normalize_label(label)
    if section == "eps":
        # Older filings may use labels like "Basic (A/B)" and
        # "Diluted (A/C)" in an EPS note table.
        if re.search(r"^basic\b|^basic\s+earnings", l):
            return "eps"
        if re.search(r"^diluted\b|^diluted\s+earnings", l):
            return "epsDiluted"
        if re.search(r"^\$?$", l):
            return None
    if section == "shares":
        if re.search(r"\bbasic\b", l):
            return "weightedAverageShsOut"
        if re.search(r"\bdiluted\b", l):
            return "weightedAverageShsOutDil"
    # Direct single-line labels like "Basic earnings per share".
    if re.search(r"^basic\s+earnings.*per\s+(common\s+)?share", l):
        return "eps"
    if re.search(r"^diluted\s+earnings.*per\s+(common\s+)?share", l):
        return "epsDiluted"
    if re.search(r"weighted\s+average\s+shares.*basic", l):
        return "weightedAverageShsOut"
    if re.search(r"weighted\s+average\s+shares.*diluted", l):
        return "weightedAverageShsOutDil"
    return None


def output_multiplier(field: str) -> int:
    if field in EPS_FIELDS:
        return 1
    return VALUE_MULTIPLIER


def infer_table_share_multiplier(table: pd.DataFrame) -> int:
    """Infer share-count units from table-level text.

    Modern Apple filings often say "shares in thousands" in the table title,
    while the actual Basic/Diluted row labels do not repeat that unit. Without
    this table-level inference, share counts can be overstated by 1,000x.
    """
    text = normalize_label(" ".join(normalize_text(x) for x in table.astype(str).fillna("").values.flatten()))
    if re.search(r"shares?\s+in\s+thousands|in\s+thousands.*shares?", text):
        return 1_000
    if re.search(r"shares?\s+in\s+millions|in\s+millions.*shares?", text):
        return 1_000_000
    return VALUE_MULTIPLIER




def infer_plain_text_share_multiplier(block: str) -> int:
    """Infer share-count units from a plain-text/fixed-width statement block."""
    text = normalize_label(block)
    if re.search(r"shares?\s+in\s+thousands|in\s+thousands.*shares?", text):
        return 1_000
    if re.search(r"shares?\s+in\s+millions|in\s+millions.*shares?", text):
        return 1_000_000
    return VALUE_MULTIPLIER

def share_multiplier_from_label(label: str) -> int | None:
    l = normalize_label(label)
    if "in thousands" in l:
        return 1_000
    if "in millions" in l:
        return 1_000_000
    return None


def field_multiplier(field: str, share_multiplier: int | None = None) -> int:
    if field in EPS_FIELDS:
        return 1
    if field in SHARE_FIELDS:
        return share_multiplier or VALUE_MULTIPLIER
    return VALUE_MULTIPLIER


def scale_extracted_value(field: str, value: float, share_multiplier: int | None = None) -> float:
    """Scale table/plain-text values into full dollars or full share counts.

    Most rendered income-statement tables use dollars in millions. Share rows
    are less consistent: older filings often report shares in millions, while
    modern Apple filings report shares in thousands. If the table-level unit is
    lost by pandas.read_html(), a raw share value above 100,000 is almost always
    already in thousands, not millions.
    """
    if field in EPS_FIELDS:
        return value
    if field in SHARE_FIELDS:
        multiplier = share_multiplier or VALUE_MULTIPLIER
        if multiplier == VALUE_MULTIPLIER and abs(value) >= 100_000:
            multiplier = 1_000
        return value * multiplier
    return value * VALUE_MULTIPLIER


def is_basic_and_diluted_eps_label(label: str) -> bool:
    l = normalize_label(label)
    return bool(
        re.search(r"basic\s+and\s+diluted.*(earnings|loss).*per\s+(common\s+)?share", l)
        or re.search(r"(earnings|loss).*per\s+(common\s+)?share.*basic\s+and\s+diluted", l)
    )


def eps_context_priority(label: str, current_priority: int = 1) -> int:
    """Prefer bottom-line EPS when filings present before/after accounting-change sections.

    Older AAPL filings can show both:
      - EPS before accounting change
      - EPS after accounting change

    Since netIncome is bottom-line net income, EPS should align with the
    after-accounting-change section when it exists.
    """
    l = normalize_label(label)
    if re.search(r"after\s+accounting\s+change", l):
        return 3
    if re.search(r"before\s+accounting\s+change", l):
        return 1
    return current_priority


def maybe_store_eps(extracted: dict, eps_quality: dict, field: str, value: float, priority: int) -> None:
    if field not in EPS_FIELDS:
        extracted[field] = value
        return
    old_priority = eps_quality.get(field, -1)
    if field not in extracted or priority >= old_priority:
        extracted[field] = value
        eps_quality[field] = priority


def extract_fields_from_table(table: pd.DataFrame, report_date: str, form: str | None) -> dict:
    extracted = {}
    eps_quality: dict[str, int] = {}
    col_scores = score_columns_for_report_date(table, report_date, form)
    current_section = None
    current_share_multiplier = infer_table_share_multiplier(table)
    current_eps_field = None
    current_eps_priority = 1

    for _, row in table.iterrows():
        label, numeric_cells = get_row_label_and_numeric_cells(row)
        if not label:
            continue
        previous_section = current_section
        current_section = infer_section(label, current_section)
        if current_section == "shares" and previous_section != "shares":
            label_share_multiplier = share_multiplier_from_label(label)
            if label_share_multiplier is not None:
                current_share_multiplier = label_share_multiplier
            current_eps_field = None
        if current_section == "eps":
            current_eps_priority = eps_context_priority(label, current_eps_priority)
            if re.search(r"basic\s+earnings.*per\s+share|basic\s+loss.*per\s+share", label):
                current_eps_field = "eps"
            elif re.search(r"diluted\s+earnings.*per\s+share|diluted\s+loss.*per\s+share", label):
                current_eps_field = "epsDiluted"
        if not numeric_cells:
            continue

        # Some loss-period filings report one line for both basic and diluted EPS.
        if is_basic_and_diluted_eps_label(label):
            value = choose_value_from_numeric_cells(numeric_cells, col_scores)
            if value is not None:
                maybe_store_eps(extracted, eps_quality, "eps", value, current_eps_priority)
                maybe_store_eps(extracted, eps_quality, "epsDiluted", value, current_eps_priority)
            continue

        # Older MSFT/AAPL-style income statements sometimes put EPS values on
        # rows like "Before accounting change" / "After accounting change"
        # under a prior heading "Basic earnings per share:" / "Diluted earnings per share:".
        if current_section == "eps" and current_eps_field:
            if re.search(r"^(before|after)\s+accounting\s+change$|^income\s+(before|after)\s+accounting\s+change$", label):
                row_priority = eps_context_priority(label, current_eps_priority)
                value = choose_value_from_numeric_cells(numeric_cells, col_scores)
                if value is not None:
                    maybe_store_eps(
                        extracted,
                        eps_quality,
                        current_eps_field,
                        scale_extracted_value(current_eps_field, value, current_share_multiplier),
                        row_priority,
                    )
                continue

        # Section-aware ambiguous rows: Basic / Diluted.
        sfield = section_field(label, current_section)
        if sfield:
            value = choose_value_from_numeric_cells(numeric_cells, col_scores)
            if value is not None:
                scaled = scale_extracted_value(sfield, value, current_share_multiplier)
                if sfield in EPS_FIELDS:
                    maybe_store_eps(extracted, eps_quality, sfield, scaled, current_eps_priority)
                elif sfield not in extracted:
                    extracted[sfield] = scaled
            continue

        # Normal row label matching.
        for field, patterns in FIELD_PATTERNS.items():
            if field in extracted:
                continue
            if label_matches(label, patterns):
                value = choose_value_from_numeric_cells(numeric_cells, col_scores)
                if value is not None:
                    extracted[field] = scale_extracted_value(field, value, current_share_multiplier)
                break
    return extracted

# ============================================================
# PLAIN-TEXT SEC TABLE FALLBACK
# ============================================================

def clean_plain_text_line(line: str) -> str:
    line = html.unescape(line)
    line = line.replace("\xa0", " ")
    line = line.replace("&#151;", "-").replace("&mdash;", "-")
    return line.rstrip("\n")


def extract_income_statement_text_block(text: str) -> str | None:
    lines = [clean_plain_text_line(x) for x in text.splitlines()]
    title_re = re.compile(r"income\s+statements?|statements?\s+of\s+operations|statements?\s+of\s+income", re.I)
    candidates = []
    for i, line in enumerate(lines):
        window_title = " ".join(lines[i:i + 4])
        if not title_re.search(window_title):
            continue
        lookahead = "\n".join(lines[i:i + 100]).lower()
        score = 0
        for keyword in [
            "revenue", "cost of revenue", "research and development", "sales and marketing",
            "operating income", "income before income taxes", "net income", "earnings per share",
            "average shares outstanding",
        ]:
            if keyword in lookahead:
                score += 1
        if "index" in "\n".join(lines[max(0, i - 20):i + 5]).lower():
            score -= 4
        candidates.append((score, i))
    if not candidates:
        return None
    candidates = sorted(candidates, key=lambda x: (x[0], x[1]), reverse=True)
    start_idx = candidates[0][1]

    end_idx = len(lines)
    end_re = re.compile(r"balance\s+sheets?|cash\s+flows?|comprehensive\s+income|stockholders'?\s+equity|notes\s+to\s+financial", re.I)
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


def find_plain_text_target_position(block: str, report_date: str, form: str | None) -> int | None:
    lines = block.splitlines()[:35]
    variants = plain_text_date_variants(report_date)
    report_year = str(pd.to_datetime(report_date).year)
    candidates: list[tuple[int, int]] = []
    for line in lines:
        low = line.lower()
        for variant in variants:
            if not variant or variant == report_year:
                continue
            pos = low.find(variant.lower())
            if pos >= 0:
                candidates.append((30, pos + len(variant) // 2))

    # Better fallback for old 10-Q fixed-width tables:
    # columns are usually PY QTD, CY QTD, PY YTD, CY YTD. Prefer CY QTD.
    if form == "10-Q" and PREFER_QUARTERLY_FOR_10Q:
        header = "\n".join(lines).lower()
        year_positions = []
        for line in lines:
            for m in re.finditer(rf"\b{re.escape(report_year)}\b", line.lower()):
                year_positions.append(m.start() + 2)
        if year_positions:
            # Prefer the leftmost occurrence of current year if it is in the Three Months group.
            return sorted(year_positions)[0]

    for line in lines:
        low = line.lower()
        for m in re.finditer(rf"\b{re.escape(report_year)}\b", low):
            candidates.append((5, m.start() + 2))
    if candidates:
        candidates = sorted(candidates, key=lambda x: (x[0], -x[1]), reverse=True)
        return candidates[0][1]
    return None


def parse_plain_text_number_token(token: str) -> float | None:
    token = normalize_text(token)
    if not token or token in {"-", "--", "—"}:
        return 0.0 if token in {"-", "--", "—"} else None
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
    token_re = re.compile(r"(?<![A-Za-z])(?:\$\s*)?(?:\([0-9][0-9,]*(?:\.[0-9]+)?\)|[0-9][0-9,]*(?:\.[0-9]+)?|--|-)(?![A-Za-z])")
    spans = []
    for m in token_re.finditer(line):
        raw = m.group(0).strip()
        value = parse_plain_text_number_token(raw)
        if value is None:
            continue
        spans.append((m.start(), m.end(), value))
    return spans


def row_label_from_plain_line(line: str, spans: list[tuple[int, int, float]]) -> str:
    if not spans:
        return ""
    first_start = spans[0][0]
    label = line[:first_start]
    label = re.sub(r"^[\s\-]+", "", label)
    label = re.sub(r"[\s\-]+$", "", label)
    return normalize_label(label)


def choose_plain_text_value(spans: list[tuple[int, int, float]], target_pos: int | None, form: str | None, field: str | None = None) -> float | None:
    if not spans:
        return None
    if target_pos is not None:
        return min(spans, key=lambda x: abs(((x[0] + x[1]) // 2) - target_pos))[2]
    if form == "10-Q" and PREFER_QUARTERLY_FOR_10Q and len(spans) >= 4:
        return spans[1][2]
    return spans[-1][2]


def extract_fields_from_plain_text(path: Path, report_date: str, form: str | None) -> dict:
    try:
        text = read_text(path)
    except Exception:
        return {}
    block = extract_income_statement_text_block(text)
    if not block:
        return {}
    target_pos = find_plain_text_target_position(block, report_date, form)
    extracted: dict[str, float] = {}
    eps_quality: dict[str, int] = {}
    current_section = None
    current_share_multiplier = infer_plain_text_share_multiplier(block)
    current_eps_field = None
    current_eps_priority = 1
    pending_label = ""

    for raw_line in block.splitlines():
        line = clean_plain_text_line(raw_line)
        if not line.strip():
            continue
        if re.match(r"^[\-=]{5,}$", line.strip()):
            continue
        if line.strip().startswith(("<", "</")):
            continue

        spans = extract_number_spans_from_plain_line(line)
        if not spans:
            label_piece = normalize_label(line)
            if label_piece:
                previous_section = current_section
                current_section = infer_section(label_piece, current_section)
                if current_section == "shares" and previous_section != "shares":
                    label_share_multiplier = share_multiplier_from_label(label_piece)
                    if label_share_multiplier is not None:
                        current_share_multiplier = label_share_multiplier
                    current_eps_field = None
                if current_section == "eps":
                    current_eps_priority = eps_context_priority(label_piece, current_eps_priority)
                if re.search(r"basic\s+earnings.*per\s+share|basic\s+loss.*per\s+share", label_piece):
                    current_eps_field = "eps"
                elif re.search(r"diluted\s+earnings.*per\s+share|diluted\s+loss.*per\s+share", label_piece):
                    current_eps_field = "epsDiluted"
                if len(label_piece) > 8:
                    pending_label = label_piece
            continue

        label = row_label_from_plain_line(line, spans)
        if not label and pending_label:
            label = pending_label
        elif pending_label and len(label) < 8:
            # Do not prepend EPS/share section headings to ambiguous rows like
            # "Basic" / "Diluted". Section context already disambiguates them,
            # and prepending the heading prevents section_field() from matching.
            if current_section in {"eps", "shares"} and re.search(r"^(basic|diluted)$", label):
                pass
            else:
                label = normalize_label(pending_label + " " + label)
        pending_label = ""

        if not label and current_section == "eps" and current_eps_field:
            value = choose_plain_text_value(spans, target_pos, form, current_eps_field)
            if value is not None:
                maybe_store_eps(
                    extracted,
                    eps_quality,
                    current_eps_field,
                    scale_extracted_value(current_eps_field, value, current_share_multiplier),
                    current_eps_priority,
                )
            continue
        if not label:
            continue

        previous_section = current_section
        current_section = infer_section(label, current_section)
        if current_section == "shares" and previous_section != "shares":
            label_share_multiplier = share_multiplier_from_label(label)
            if label_share_multiplier is not None:
                current_share_multiplier = label_share_multiplier
            current_eps_field = None
        if current_section == "eps":
            current_eps_priority = eps_context_priority(label, current_eps_priority)

        # Older fixed-width/HTML text tables sometimes put EPS values on
        # "Before/After accounting change" rows under a prior Basic/Diluted EPS heading.
        if current_section == "eps" and current_eps_field:
            if re.search(r"^(before|after)\s+accounting\s+change$|^income\s+(before|after)\s+accounting\s+change$", label):
                row_priority = eps_context_priority(label, current_eps_priority)
                value = choose_plain_text_value(spans, target_pos, form, current_eps_field)
                if value is not None:
                    maybe_store_eps(
                        extracted,
                        eps_quality,
                        current_eps_field,
                        scale_extracted_value(current_eps_field, value, current_share_multiplier),
                        row_priority,
                    )
                continue

        # Some loss-period filings report one line for both basic and diluted EPS.
        if is_basic_and_diluted_eps_label(label):
            value = choose_plain_text_value(spans, target_pos, form, "eps")
            if value is not None:
                maybe_store_eps(extracted, eps_quality, "eps", value, current_eps_priority)
                maybe_store_eps(extracted, eps_quality, "epsDiluted", value, current_eps_priority)
            continue

        # Section-aware fields first.
        sfield = section_field(label, current_section)
        if sfield:
            value = choose_plain_text_value(spans, target_pos, form, sfield)
            if value is not None:
                scaled = scale_extracted_value(sfield, value, current_share_multiplier)
                if sfield in EPS_FIELDS:
                    maybe_store_eps(extracted, eps_quality, sfield, scaled, current_eps_priority)
                elif sfield not in extracted:
                    extracted[sfield] = scaled
            continue

        for field, patterns in FIELD_PATTERNS.items():
            if field in extracted or field not in OUTPUT_COLUMNS:
                continue
            if label_matches(label, patterns):
                value = choose_plain_text_value(spans, target_pos, form, field)
                if value is not None:
                    extracted[field] = scale_extracted_value(field, value, current_share_multiplier)
                break
    return extracted

# ============================================================
# EXTRACTION
# ============================================================

def empty_result(report_date: str | None) -> dict:
    return {col: pd.NA for col in OUTPUT_COLUMNS} | {"date": report_date}


def apply_derived_fields(result: dict) -> dict:
    """
    Conservative post-processing.

    Rule for this project: extract directly reported filing attributes only.
    Do NOT derive analytical fields such as EBIT, EBITDA, costAndExpenses, etc.

    We only mirror exact direct aliases when the same reported line item has two
    output names.
    """
    if pd.isna(result.get("bottomLineNetIncome")) and pd.notna(result.get("netIncome")):
        result["bottomLineNetIncome"] = result["netIncome"]
    return result


def extract_income_statement_from_filing(path: Path) -> dict:
    report_date = parse_report_date_from_filename(path)
    form = parse_form_from_filename(path)
    result = empty_result(report_date)
    if report_date is None:
        return result

    # XBRL first.
    extracted = extract_fields_from_xbrl(path, report_date, form)
    for key, value in extracted.items():
        if key in result:
            result[key] = value

    # HTML table fallback for missing fields.
    tables = read_tables(path)
    candidate_tables = choose_income_statement_tables(tables)
    if candidate_tables:
        for table in candidate_tables:
            fallback = extract_fields_from_table(table, report_date, form)
            for key, value in fallback.items():
                if key in result and pd.isna(result[key]):
                    result[key] = value

        # Plain-text SEC fallback can still recover fields missed by pandas table parsing
        # in old filings, especially EPS before/after accounting-change layouts.
        remaining_missing = any(pd.isna(result.get(col)) for col in OUTPUT_COLUMNS if col != "date")
        if remaining_missing:
            text_fallback = extract_fields_from_plain_text(path, report_date, form)
            if text_fallback:
                for key, value in text_fallback.items():
                    if key in result and pd.isna(result[key]):
                        result[key] = value
                    # Allow text fallback to correct EPS if it found the after-accounting-change value.
                    elif key in EPS_FIELDS and key in result and pd.notna(value):
                        result[key] = value
    else:
        # Plain-text SEC fallback for old .txt filings where read_html returns no useful tables.
        fallback = extract_fields_from_plain_text(path, report_date, form)
        if fallback:
            print(f"[plain-text income-statement fallback used] {path.name}")
            for key, value in fallback.items():
                if key in result and pd.isna(result[key]):
                    result[key] = value
        else:
            print(f"[no income-statement table found] {path.name}")

    result = apply_derived_fields(result)
    return result


# ============================================================
# FOLDER LEVEL
# ============================================================

def list_filing_files(filings_dir: Path) -> list[Path]:
    files = []
    for suffix in ["*.htm", "*.html", "*.txt"]:
        files.extend(filings_dir.glob(suffix))
    return sorted(set(files))


def extract_income_statements_from_folder(filings_dir: Path) -> pd.DataFrame:
    files = list_filing_files(filings_dir)
    print(f"Found {len(files)} filing files in {filings_dir}")
    rows = []
    for path in files:
        print(f"\nParsing: {path.name}")
        row = extract_income_statement_from_filing(path)
        row["source_file"] = path.name
        extracted_fields = [col for col in OUTPUT_COLUMNS if col != "date" and pd.notna(row.get(col))]
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
    df = extract_income_statements_from_folder(FILINGS_DIR)
    df.to_csv(OUTPUT_CSV, index=False)
    print("\nDone.")
    print(f"Saved to: {OUTPUT_CSV}")
    print("\nMissing counts:")
    print(df[OUTPUT_COLUMNS].isna().sum())
    print("\nPreview:")
    print(df.head(20).to_string(index=False))
