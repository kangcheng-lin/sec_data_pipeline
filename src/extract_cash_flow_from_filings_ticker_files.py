from pathlib import Path
import argparse
import re
import html
import pandas as pd


# ============================================================
# CONFIG
# ============================================================

FILINGS_DIR = Path("sec_filings/AAPL")
OUTPUT_CSV = Path("AAPL_cash_flow_direct_from_filings.csv")

# Rendered SEC statement tables usually show values in millions.
# Inline XBRL facts are handled separately through scale/decimals.
VALUE_MULTIPLIER = 1_000_000

# For 10-Q cash-flow statements, SEC filings often report YTD cash flow only.
# This script first tries to extract a quarterly duration context. If unavailable,
# it uses the YTD context directly and marks extraction as best effort.
PREFER_QUARTERLY_FOR_10Q = True

# Dividend policy: if the cash-flow statement / financing section is successfully
# parsed and no common dividend line is reported, treat dividends paid as zero.
# This is useful for non-dividend periods and prevents dropping valid rows in
# Value-strategy TTM dividend-yield construction.
ZERO_FILL_MISSING_COMMON_DIVIDENDS_WHEN_FINANCING_CF_PARSED = True


OUTPUT_COLUMNS = [
    "date",
    # Operating section
    "netIncome",
    "deferredIncomeTax",
    "stockBasedCompensation",
    "accountsReceivables",
    "inventory",
    "accountsPayables",
    "otherNonCashItems",
    "netCashProvidedByOperatingActivities",
    "operatingCashFlow",
    # Investing section
    "capitalExpenditure",
    "investmentsInPropertyPlantAndEquipment",
    "purchasesOfInvestments",
    "salesMaturitiesOfInvestments",
    "otherInvestingActivities",
    "netCashProvidedByInvestingActivities",
    # Financing section
    "commonStockIssuance",
    "commonStockRepurchased",
    "commonDividendsPaid",
    "netDividendsPaid",
    "netCashProvidedByFinancingActivities",
    # Cash reconciliation / supplemental cash flow
    "effectOfForexChangesOnCash",
    "netChangeInCash",
    "cashAtBeginningOfPeriod",
    "cashAtEndOfPeriod",
]


# ============================================================
# XBRL TAG MAPS
# ============================================================

# Sign convention: provider-style cash outflows are usually negative.
# For tags that represent cash paid / purchases / repayments / dividends,
# we multiply by -1 when SEC reports the fact as a positive payment amount.
XBRL_FIELD_TAGS = {
    "netIncome": [
        ("NetIncomeLoss", 1),
        ("ProfitLoss", 1),
    ],
    "deferredIncomeTax": [
        ("DeferredIncomeTaxExpenseBenefit", 1),
        ("DeferredIncomeTaxesAndTaxCredits", 1),
    ],
    "stockBasedCompensation": [
        ("ShareBasedCompensation", 1),
        ("AllocatedShareBasedCompensationExpense", 1),
    ],
    "accountsReceivables": [
        ("IncreaseDecreaseInAccountsReceivable", 1),
        ("IncreaseDecreaseInReceivables", 1),
    ],
    "inventory": [
        ("IncreaseDecreaseInInventories", 1),
    ],
    "accountsPayables": [
        ("IncreaseDecreaseInAccountsPayable", 1),
        ("IncreaseDecreaseInAccountsPayableAndAccruedLiabilities", 1),
    ],
    "changeInWorkingCapital": [
        ("IncreaseDecreaseInOperatingCapital", 1),
        ("IncreaseDecreaseInOperatingAssetsAndLiabilities", 1),
    ],
    "otherNonCashItems": [
        ("OtherNoncashIncomeExpense", 1),
        ("OtherOperatingActivitiesCashFlowStatement", 1),
    ],
    "netCashProvidedByOperatingActivities": [
        ("NetCashProvidedByUsedInOperatingActivities", 1),
        ("NetCashProvidedByUsedInOperatingActivitiesContinuingOperations", 1),
    ],
    # Direct alias for the same reported operating cash flow line.
    "operatingCashFlow": [
        ("NetCashProvidedByUsedInOperatingActivities", 1),
        ("NetCashProvidedByUsedInOperatingActivitiesContinuingOperations", 1),
    ],
    "investmentsInPropertyPlantAndEquipment": [
        ("PaymentsToAcquirePropertyPlantAndEquipment", -1),
        ("PaymentsToAcquireProductiveAssets", -1),
    ],
    "capitalExpenditure": [
        ("PaymentsToAcquirePropertyPlantAndEquipment", -1),
        ("PaymentsToAcquireProductiveAssets", -1),
    ],
    "purchasesOfInvestments": [
        ("PaymentsToAcquireAvailableForSaleSecurities", -1),
        ("PaymentsToAcquireMarketableSecurities", -1),
        ("PaymentsToAcquireInvestments", -1),
    ],
    "salesMaturitiesOfInvestments": [
        ("ProceedsFromSaleAndMaturityOfMarketableSecurities", 1),
        ("ProceedsFromMaturitiesPrepaymentsAndCallsOfAvailableForSaleSecurities", 1),
        ("ProceedsFromSaleOfAvailableForSaleSecurities", 1),
        ("ProceedsFromSaleOfMarketableSecurities", 1),
        ("ProceedsFromMaturitiesOfMarketableSecurities", 1),
    ],
    "otherInvestingActivities": [
        ("PaymentsForProceedsFromOtherInvestingActivities", 1),
        ("OtherCashProvidedByUsedInInvestingActivities", 1),
    ],
    "netCashProvidedByInvestingActivities": [
        ("NetCashProvidedByUsedInInvestingActivities", 1),
        ("NetCashProvidedByUsedInInvestingActivitiesContinuingOperations", 1),
    ],
    "commonStockIssuance": [
        ("ProceedsFromIssuanceOfCommonStock", 1),
        ("ProceedsFromStockOptionsExercised", 1),
    ],
    "commonStockRepurchased": [
        ("PaymentsForRepurchaseOfCommonStock", -1),
        ("PaymentsForRepurchaseOfEquity", -1),
    ],
    "commonDividendsPaid": [
        ("PaymentsOfDividends", -1),
        ("PaymentsOfDividendsCommonStock", -1),
        ("PaymentsOfCommonStockDividends", -1),
        ("PaymentsOfDividendsAndDividendEquivalentsOnCommonStockAndRestrictedStockUnits", -1),
    ],
    # Direct alias only; do not net against other payout fields.
    "netDividendsPaid": [
        ("PaymentsOfDividends", -1),
        ("PaymentsOfDividendsCommonStock", -1),
        ("PaymentsOfCommonStockDividends", -1),
        ("PaymentsOfDividendsAndDividendEquivalentsOnCommonStockAndRestrictedStockUnits", -1),
    ],
    "netCashProvidedByFinancingActivities": [
        ("NetCashProvidedByUsedInFinancingActivities", 1),
        ("NetCashProvidedByUsedInFinancingActivitiesContinuingOperations", 1),
    ],
    "effectOfForexChangesOnCash": [
        ("EffectOfExchangeRateOnCashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", 1),
        ("EffectOfExchangeRateOnCashAndCashEquivalents", 1),
    ],
    "netChangeInCash": [
        ("CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect", 1),
        ("CashAndCashEquivalentsPeriodIncreaseDecrease", 1),
    ],
    "cashAtBeginningOfPeriod": [
        ("CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseExcludingExchangeRateEffect", 0),
    ],
}

# Beginning/end cash are usually instant facts. We read them separately.
XBRL_INSTANT_CASH_TAGS = {
    "cashAtEndOfPeriod": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ]
}


# ============================================================
# TABLE FALLBACK LABEL PATTERNS
# ============================================================

FIELD_PATTERNS = {
    # Operating section
    "netIncome": [
        r"^net\s+income\b",
        r"^net\s+loss\b",
    ],
    "deferredIncomeTax": [
        r"deferred\s+income\s+tax",
        r"deferred\s+tax",
    ],
    "stockBasedCompensation": [
        r"share\s+based\s+compensation",
        r"stock\s+based\s+compensation",
    ],
    "accountsReceivables": [
        r"accounts\s+receivable",
        r"accounts\s+receivables",
        r"\breceivables\b",
    ],
    "inventory": [
        r"\binventor(y|ies)\b",
    ],
    "accountsPayables": [
        r"accounts\s+payable",
        r"accounts\s+payables",
        r"\bpayables\b",
    ],
    "changeInWorkingCapital": [
        r"changes?\s+in\s+working\s+capital",
        r"changes?\s+in\s+operating\s+assets\s+and\s+liabilities",
        r"operating\s+assets\s+and\s+liabilities",
    ],
    "otherWorkingCapital": [
        r"other\s+current\s+and\s+non\s+current\s+assets\s+and\s+liabilities",
        r"other\s+assets\s+and\s+liabilities",
        r"other\s+working\s+capital",
    ],
    "otherNonCashItems": [
        r"other\s+non\s+cash",
        r"other\s+noncash",
        r"other\s+operating\s+activities",
        r"other\b",
    ],
    "netCashProvidedByOperatingActivities": [
        r"net\s+cash\s+provided\s+by\s+operating",
        r"net\s+cash\s+provided\s+by\s+used\s+in\s+operating",
        r"net\s+cash\s+from\s+operating",
        r"net\s+cash\s+from\s+operations",
        r"cash\s+generated\s+by\s+operating\s+activities",
        r"cash\s+generated\s+by\s+used\s+for\s+operating\s+activities",
        r"cash\s+provided\s+by\s+operating\s+activities",
        r"cash\s+from\s+operations",
        r"cash\s+flows?\s+from\s+operations",
    ],
    # Direct alias for the same reported operating cash flow line.
    "operatingCashFlow": [
        r"net\s+cash\s+provided\s+by\s+operating",
        r"net\s+cash\s+provided\s+by\s+used\s+in\s+operating",
        r"net\s+cash\s+from\s+operating",
        r"net\s+cash\s+from\s+operations",
        r"cash\s+generated\s+by\s+operating\s+activities",
        r"cash\s+generated\s+by\s+used\s+for\s+operating\s+activities",
        r"cash\s+provided\s+by\s+operating\s+activities",
        r"cash\s+from\s+operations",
        r"cash\s+flows?\s+from\s+operations",
    ],

    # Investing section
    # Apple often says "Payments for acquisition of property, plant and equipment".
    # This is CapEx, NOT acquisitionsNet.
    "capitalExpenditure": [
        r"payments\s+for\s+acquisition\s+of\s+property\s+plant\s+and\s+equipment",
        r"payments\s+to\s+acquire\s+property\s+plant\s+and\s+equipment",
        r"payments\s+for\s+property\s+plant\s+and\s+equipment",
        r"purchases\s+of\s+property\s+plant\s+and\s+equipment",
        r"purchase\s+of\s+property\s+plant\s+and\s+equipment",
        r"purchases\s+of\s+property\s+and\s+equipment",
        r"purchase\s+of\s+property\s+and\s+equipment",
        r"additions\s+to\s+property\s+and\s+equipment",
        r"capital\s+expenditure",
        r"capital\s+expenditures",
    ],
    "investmentsInPropertyPlantAndEquipment": [
        r"payments\s+for\s+acquisition\s+of\s+property\s+plant\s+and\s+equipment",
        r"payments\s+to\s+acquire\s+property\s+plant\s+and\s+equipment",
        r"payments\s+for\s+property\s+plant\s+and\s+equipment",
        r"purchases\s+of\s+property\s+plant\s+and\s+equipment",
        r"purchase\s+of\s+property\s+plant\s+and\s+equipment",
        r"purchases\s+of\s+property\s+and\s+equipment",
        r"purchase\s+of\s+property\s+and\s+equipment",
        r"additions\s+to\s+property\s+and\s+equipment",
        r"capital\s+expenditure",
        r"capital\s+expenditures",
    ],
    "purchasesOfInvestments": [
        r"purchases\s+of\s+investments",
        r"purchase\s+of\s+investments",
        r"purchases\s+of\s+marketable\s+securities",
        r"purchase\s+of\s+marketable\s+securities",
        r"purchases\s+of\s+available\s+for\s+sale\s+securities",
        r"payments\s+to\s+acquire\s+investments",
    ],
    "salesMaturitiesOfInvestments": [
        r"sales\s+and\s+maturities\s+of\s+investments",
        r"proceeds\s+from\s+maturities",
        r"proceeds\s+from\s+sales\s+of\s+investments",
        r"proceeds\s+from\s+sales\s+and\s+maturities",
        r"sales\s+of\s+investments",
        r"maturities\s+of\s+investments",
    ],
    "otherInvestingActivities": [
        r"other\s+investing\s+activities",
    ],
    "netCashProvidedByInvestingActivities": [
        r"net\s+cash\s+provided\s+by\s+investing",
        r"net\s+cash\s+used\s+in\s+investing",
        r"net\s+cash\s+used\s+for\s+investing",
        r"net\s+cash\s+provided\s+by\s+used\s+in\s+investing",
        r"net\s+cash\s+from\s+used\s+in\s+investing",
        r"net\s+cash\s+from\s+used\s+for\s+investing",
        r"cash\s+generated\s+by\s+used\s+in\s+investing\s+activities",
        r"cash\s+provided\s+by\s+used\s+in\s+investing\s+activities",
        r"cash\s+used\s+in\s+investing\s+activities",
        r"cash\s+generated\s+by\s+used\s+for\s+investing\s+activities",
        r"cash\s+generated\s+by\s+investing\s+activities",
        r"cash\s+provided\s+by\s+used\s+for\s+investing\s+activities",
    ],

    # Financing section
    "commonStockIssuance": [
        r"issuance\s+of\s+common\s+stock",
        r"proceeds\s+from\s+issuance\s+of\s+common",
        r"proceeds\s+from\s+stock\s+plans",
        r"common\s+stock\s+issued",
    ],
    "commonStockRepurchased": [
        r"repurchase\s+of\s+common\s+stock",
        r"repurchases\s+of\s+common\s+stock",
        r"payments\s+for\s+repurchase\s+of\s+common\s+stock",
        r"common\s+stock\s+repurchased",
    ],
    "commonDividendsPaid": [
        r"payments\s+for\s+dividends\s+and\s+dividend\s+equivalents",
        r"dividends\s+and\s+dividend\s+equivalents",
        r"dividends\s+paid",
        r"cash\s+dividends",
        r"common\s+stock\s+cash\s+dividends",
        r"common\s+stock\s+dividends\b",
        r"common\s+stock\s+dividend\b",
        r"common\s+dividends\b",
        r"dividends\s+on\s+common\s+stock",
    ],
    # Direct alias only; do not derive from multiple payout fields.
    "netDividendsPaid": [
        r"payments\s+for\s+dividends\s+and\s+dividend\s+equivalents",
        r"dividends\s+and\s+dividend\s+equivalents",
        r"dividends\s+paid",
        r"cash\s+dividends",
        r"common\s+stock\s+cash\s+dividends",
        r"common\s+stock\s+dividends\b",
        r"common\s+stock\s+dividend\b",
        r"common\s+dividends\b",
        r"dividends\s+on\s+common\s+stock",
    ],
    "netCashProvidedByFinancingActivities": [
        r"net\s+cash\s+provided\s+by\s+financing",
        r"net\s+cash\s+used\s+in\s+financing",
        r"net\s+cash\s+used\s+for\s+financing",
        r"net\s+cash\s+provided\s+by\s+used\s+in\s+financing",
        r"net\s+cash\s+from\s+used\s+in\s+financing",
        r"net\s+cash\s+from\s+used\s+for\s+financing",
        r"cash\s+used\s+in\s+financing\s+activities",
        r"cash\s+provided\s+by\s+used\s+in\s+financing\s+activities",
        r"cash\s+generated\s+by\s+financing\s+activities",
        r"cash\s+generated\s+by\s+used\s+for\s+financing\s+activities",
        r"cash\s+provided\s+by\s+financing\s+activities",
    ],

    # Cash reconciliation
    "effectOfForexChangesOnCash": [
        r"effect\s+of\s+exchange\s+rate",
        r"effect\s+of\s+foreign\s+exchange",
    ],
    "netChangeInCash": [
        r"increase\s+decrease\s+in\s+cash",
        r"net\s+increase\s+decrease\s+in\s+cash",
        r"increase\s+in\s+cash\s+and\s+cash\s+equivalents",
        r"decrease\s+in\s+cash\s+and\s+cash\s+equivalents",
        r"net\s+change\s+in\s+cash",
    ],
    "cashAtBeginningOfPeriod": [
        r"cash\s+and\s+cash\s+equivalents.*beginning",
        r"cash\s+and\s+equivalents.*beginning",
        r"cash.*beginning\s+of\s+period",
        r"cash.*beginning\s+of\s+year",
        r"cash.*beginning\s+of\s+the\s+period",
    ],
    "cashAtEndOfPeriod": [
        r"cash\s+and\s+cash\s+equivalents.*end\s+of\s+period",
        r"cash\s+and\s+equivalents.*end\s+of\s+period",
        r"cash\s+and\s+equivalents.*end\s+of\s+year",
        r"cash\s+and\s+cash\s+equivalents.*end\s+of\s+the\s+period",
        r"cash\s+and\s+cash\s+equivalents\s+end\s+of\s+the\s+period",
        r"cash.*end\s+of\s+period",
        r"cash.*end\s+of\s+year",
        r"cash.*end\s+of\s+the\s+period",
    ],
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
    label = normalize_label(label)
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
    s = s.replace("$", "").replace(",", "").replace("(", "").replace(")", "")
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



def parse_table_number_from_neighbors(cells: list[str], idx: int) -> float | None:
    """
    Parse a table value while handling SEC HTML where parentheses and
    dollar signs are split across neighboring cells, e.g. "$", "(34,235", ")".
    """
    s = normalize_text(cells[idx])
    if not re.search(r"\d", s):
        return None

    left = normalize_text(cells[idx - 1]) if idx > 0 else ""
    right = normalize_text(cells[idx + 1]) if idx + 1 < len(cells) else ""

    # SEC tables often split closing ')' into the next cell.
    is_negative = (
        "(" in s
        or ")" in s
        or left.strip() == "("
        or right.strip() == ")"
        or right.strip().startswith(")")
    )

    # Ignore obvious per-share/percent values in fallback table parsing.
    # Cash flow statement values are normally whole-dollar table values.
    cleaned = s.replace("$", "").replace(",", "").replace("(", "").replace(")", "")
    cleaned = re.sub(r"[^0-9.\-]", "", cleaned)

    if cleaned in {"", "-", ".", "-."}:
        return None

    try:
        value = float(cleaned)
    except ValueError:
        return None

    if is_negative:
        value = -abs(value)

    return value


def cell_has_letters(x: str) -> bool:
    return bool(re.search(r"[A-Za-z]", normalize_text(x)))


def cell_has_digits(x: str) -> bool:
    return bool(re.search(r"\d", normalize_text(x)))


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


def parse_instant_contexts(filing_text: str, report_date: str) -> set[str]:
    ids = set()
    context_re = re.compile(r"<xbrli:context\b(?P<attrs>[^>]*)>(?P<body>.*?)</xbrli:context>", re.I | re.S)
    for m in context_re.finditer(filing_text):
        ctx_id = get_attr(m.group("attrs"), "id")
        body = m.group("body")
        if not ctx_id:
            continue
        if re.search(r"<xbrldi:explicitMember\b", body, re.I):
            continue
        instant_m = re.search(r"<xbrli:instant>\s*([^<]+)\s*</xbrli:instant>", body, re.I)
        if instant_m and instant_m.group(1).strip() == report_date:
            ids.add(ctx_id)
    return ids


def target_duration_days(form: str | None) -> int:
    if form == "10-K":
        return 365
    return 90 if PREFER_QUARTERLY_FOR_10Q else 270


def context_duration_score(days: int, form: str | None) -> int:
    target = target_duration_days(form)
    # Prefer near-quarter contexts for 10-Q. If unavailable, YTD contexts can still win by being the only candidate.
    return -abs(days - target)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


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


def extract_xbrl_instant_facts(path: Path, report_date: str) -> dict[str, list[dict]]:
    try:
        text = read_text(path)
    except Exception:
        return {}
    contexts = parse_instant_contexts(text, report_date)
    if not contexts:
        return {}
    facts = {}
    fact_re = re.compile(r"<ix:nonFraction\b(?P<attrs>[^>]*)>(?P<body>.*?)</ix:nonFraction>", re.I | re.S)
    for m in fact_re.finditer(text):
        attrs = m.group("attrs")
        if get_attr(attrs, "contextRef") not in contexts:
            continue
        tag = local_name(get_attr(attrs, "name"))
        value = parse_inline_xbrl_number(attrs, m.group("body"))
        if tag and value is not None:
            facts.setdefault(tag, []).append({"value": value, "precision": decimals_precision_score(attrs)})
    return facts


def first_available_duration_fact(facts: dict[str, list[dict]], tags_with_sign: list[tuple[str, int]]) -> float | None:
    for tag, sign in tags_with_sign:
        vals = facts.get(tag)
        if not vals:
            continue
        best = sorted(vals, key=lambda d: (d["duration_score"], d["precision"], abs(d["value"])), reverse=True)[0]
        if sign == 0:
            return None
        # For payment/outflow tags, SEC inline XBRL may already carry a negative
        # sign. Normalize provider-style outflow fields to negative rather than
        # multiplying and accidentally flipping them positive.
        if sign < 0:
            return -abs(best["value"])
        return best["value"]
    return None


def first_available_instant_fact(facts: dict[str, list[dict]], tags: list[str]) -> float | None:
    for tag in tags:
        vals = facts.get(tag)
        if vals:
            best = sorted(vals, key=lambda d: (d["precision"], abs(d["value"])), reverse=True)[0]
            return best["value"]
    return None


def extract_fields_from_xbrl(path: Path, report_date: str, form: str | None) -> dict:
    duration_facts = extract_xbrl_duration_facts(path, report_date, form)
    instant_facts = extract_xbrl_instant_facts(path, report_date)
    extracted = {}
    for field, tags in XBRL_FIELD_TAGS.items():
        value = first_available_duration_fact(duration_facts, tags)
        if value is not None:
            extracted[field] = value
    for field, tags in XBRL_INSTANT_CASH_TAGS.items():
        value = first_available_instant_fact(instant_facts, tags)
        if value is not None:
            extracted[field] = value
    return extracted


# ============================================================
# TABLE FALLBACK
# ============================================================

def read_tables(path: Path) -> list[pd.DataFrame]:
    try:
        return pd.read_html(path, flavor="lxml")
    except Exception as e:
        print(f"[read_html failed] {path.name}: {e}")
        return []


def table_score(table: pd.DataFrame) -> int:
    """
    Score likely cash-flow statement tables.

    v3 is stricter than v2: it penalizes tiny narrative/footnote tables that
    merely mention "cash flows from operations" and rewards tables with many
    cash-flow statement line items.
    """
    n_rows, n_cols = table.shape
    text = " ".join(normalize_text(x).lower() for x in table.astype(str).fillna("").values.flatten())
    label_text = normalize_label(text)

    score = 0

    # Avoid footnotes / commitment tables that mention cash flows once.
    if n_rows < 8:
        score -= 10
    if "we expect to fund" in text or "commitments" in text or "obligations" in text:
        score -= 6

    # Strong cash-flow identifiers.
    for keyword in [
        "statements of cash flows",
        "statement of cash flows",
        "cash flows statements",
        "cash flow statements",
        "cash flows",
        "cash flow",
    ]:
        if keyword in text:
            score += 5

    # Core cash-flow labels. We count how many distinct statement lines appear.
    core_patterns = [
        r"net cash from operations",
        r"net cash provided by operating",
        r"cash generated by operating activities",
        r"cash flows? from operations",
        r"net cash used in investing",
        r"net cash from used in investing",
        r"cash generated by used for investing activities",
        r"cash generated by investing activities",
        r"net cash used in financing",
        r"net cash from used in financing",
        r"cash generated by financing activities",
        r"cash used in financing activities",
        r"cash and cash equivalents.*end",
        r"cash and cash equivalents.*beginning",
        r"purchases of property plant and equipment",
        r"purchases of property and equipment",
        r"additions to property and equipment",
        r"proceeds from maturities",
        r"repurchases of common stock",
        r"common stock cash dividends",
        r"common stock dividends",
    ]
    hits = 0
    for pattern in core_patterns:
        if re.search(pattern, label_text):
            hits += 1
            score += 3

    # Older MSFT tables often use short section labels. Reward only if the
    # table is large enough to be a statement table.
    if n_rows >= 15:
        for section in [r"\boperations\b", r"\binvesting\b", r"\bfinancing\b"]:
            if re.search(section, label_text):
                score += 2

    # Tables with several statement hits are likely the real statement even if
    # the title text is missing from read_html output.
    if hits >= 4:
        score += 8

    # Penalize obvious non-cash-flow statement tables.
    for bad in ["income statements", "balance sheets", "stockholders equity", "comprehensive income"]:
        if bad in text:
            score -= 5

    return score


def choose_cash_flow_tables(tables: list[pd.DataFrame]) -> list[pd.DataFrame]:
    """
    Return candidate cash-flow tables, best first. Some filings split the
    cash-flow statement or have supplementary cash-flow rows in nearby tables,
    so we allow multiple candidates and fill missing fields from them.
    """
    if not tables:
        return []

    scored = sorted(
        [(table_score(t), i, t) for i, t in enumerate(tables)],
        key=lambda x: x[0],
        reverse=True,
    )

    candidates = [(score, i, t) for score, i, t in scored if score >= 5 and t.shape[0] >= 8]

    if not candidates:
        return []

    # Keep only tables that are close to the best score to avoid pulling
    # unrelated tables with generic words like "operations".
    best = candidates[0][0]
    candidates = [(s, i, t) for s, i, t in candidates if s >= max(5, best - 3)]

    return [t for _, _, t in candidates]


def choose_cash_flow_table(tables: list[pd.DataFrame]) -> pd.DataFrame | None:
    candidates = choose_cash_flow_tables(tables)
    return candidates[0] if candidates else None


def report_date_tokens(report_date: str) -> list[str]:
    dt = pd.to_datetime(report_date)
    return [
        str(dt.year), dt.strftime("%B").lower(), dt.strftime("%b").lower(),
        f"{dt.strftime('%b').lower()}.", str(dt.day), f"{dt.month}/{dt.day}/{dt.year}",
        f"{dt.month:02d}/{dt.day:02d}/{dt.year}", dt.strftime("%Y-%m-%d").lower(),
    ]


def score_columns_for_report_date(table: pd.DataFrame, report_date: str, form: str | None) -> dict[int, int]:
    """
    Score numeric columns for the report date / period.

    v2 gave neighboring columns too much weight, so duplicate SEC columns such
    as "$ | 1,447 | blank | $ | 900" could make prior-year columns tie with
    current-year columns. v3 weights the column itself strongly and neighbors
    lightly.
    """
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

        score = 0

        # Strong direct date/year evidence.
        if report_year in own_text:
            score += 10
        if report_month_full in own_text or report_month_abbr in own_text:
            score += 5
        for token in tokens:
            if token in own_text:
                score += 3

        # Weak neighbor evidence, useful when dates are split across columns.
        if report_year in neighbor_text:
            score += 2
        if report_month_full in neighbor_text or report_month_abbr in neighbor_text:
            score += 1
        for token in tokens:
            if token in neighbor_text:
                score += 1

        # Period preference.
        combined = own_text + " " + neighbor_text
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
    """
    Robust row parser for old SEC HTML tables.

    Key fixes versus the earlier version:
    - Do not treat numbers embedded in the label cell as financial values.
    - Handle split parentheses, e.g. "(34,235" in one cell and ")" in the next.
    - Keep 2000 as a valid financial number; do not discard it as a year.
    """
    cells = [normalize_text(x) for x in row.tolist()]

    # Find the first cell that looks like a row label.
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

    # Some filings put continuation label text in immediately adjacent text cells.
    # Append non-numeric text cells until values start.
    j = label_idx + 1
    while j < len(cells):
        cell = cells[j]
        if not cell or cell.lower() in {"nan", "none"}:
            j += 1
            continue
        if cell_has_digits(cell):
            break
        if cell_has_letters(cell):
            label = normalize_label(label + " " + cell)
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
        # Use the leftmost numeric cell among the best-scored columns. In SEC
        # statement tables, current-period columns generally appear before
        # prior-period columns once date scoring identifies the right group.
        best = [item for item in scored if item[0] == max_score]
        best = sorted(best, key=lambda x: x[1])
        return best[0][2]

    # Fallback: use rightmost numeric value, consistent with many old filings.
    return numeric_cells[-1][1]


def field_value_from_label(field: str, raw_value: float) -> float:
    """
    Normalize sign for selected cash-flow outflow fields when the HTML table
    shows a positive number without parentheses. If the table already parsed a
    negative number, keep it negative.
    """
    outflow_fields = {
        "capitalExpenditure",
        "investmentsInPropertyPlantAndEquipment",
        "purchasesOfInvestments",
        "commonStockRepurchased",
        "commonDividendsPaid",
        "netDividendsPaid",
    }
    if field in outflow_fields:
        return -abs(raw_value)
    return raw_value


def extract_fields_from_table(table: pd.DataFrame, report_date: str, form: str | None) -> dict:
    extracted = {}
    col_scores = score_columns_for_report_date(table, report_date, form)

    for _, row in table.iterrows():
        label, numeric_cells = get_row_label_and_numeric_cells(row)
        if not label or not numeric_cells:
            continue

        # Avoid generic section headings.
        if label in {"operations", "investing", "financing", "operating activities", "investing activities", "financing activities"}:
            continue

        for field, patterns in FIELD_PATTERNS.items():
            if field in extracted:
                continue
            if label_matches(label, patterns):
                value = choose_value_from_numeric_cells(numeric_cells, col_scores)
                if value is not None:
                    value = field_value_from_label(field, value)
                    extracted[field] = value * VALUE_MULTIPLIER

    return extracted



# ============================================================
# PLAIN-TEXT SEC TABLE FALLBACK
# ============================================================

def is_likely_plain_text_filing(path: Path) -> bool:
    return path.suffix.lower() == ".txt"


def clean_plain_text_line(line: str) -> str:
    line = html.unescape(line)
    line = line.replace("\xa0", " ")
    line = line.replace("&#151;", "-").replace("&mdash;", "-")
    return line.rstrip("\n")


def extract_cash_flow_text_block(text: str) -> str | None:
    """
    Extract the actual statement-of-cash-flows block from old SEC .txt filings.

    The index/table-of-contents often contains the phrase "Cash Flows
    Statements" before the real statement. We therefore collect all title
    candidates and prefer the one followed by statement rows such as
    Operations, Net income, Investing, Financing, and Cash equivalents.
    """
    lines = [clean_plain_text_line(x) for x in text.splitlines()]
    title_re = re.compile(r"cash\s+flows?\s+statements?|statements?\s+of\s+cash\s+flows", re.I)

    candidates = []
    for i, line in enumerate(lines):
        window_title = " ".join(lines[i:i + 4])
        if not title_re.search(window_title):
            continue
        lookahead = "\n".join(lines[i:i + 80]).lower()
        score = 0
        for keyword in [
            "operations", "net income", "depreciation", "investing", "financing",
            "cash and equivalents", "cash and cash equivalents", "net cash from operations",
        ]:
            if keyword in lookahead:
                score += 1
        # Penalize table of contents / index occurrences.
        if "index" in "\n".join(lines[max(0, i - 20):i + 5]).lower():
            score -= 4
        if "page" in lookahead[:300].lower() and score < 3:
            score -= 2
        candidates.append((score, i))

    if not candidates:
        return None

    # Choose the highest-score occurrence; if tied, choose the later one to avoid TOC.
    candidates = sorted(candidates, key=lambda x: (x[0], x[1]), reverse=True)
    start_idx = candidates[0][1]

    end_idx = len(lines)
    end_re = re.compile(
        r"stockholders'?\s+equity|shareholders'?\s+equity|notes\s+to\s+financial|"
        r"management'?s\s+discussion",
        re.I,
    )
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
    """Find approximate horizontal position of the current-period column."""
    lines = block.splitlines()[:35]
    variants = plain_text_date_variants(report_date)
    report_year = str(pd.to_datetime(report_date).year)

    candidates: list[tuple[int, int]] = []
    for line in lines:
        low = line.lower()
        for variant in variants:
            if not variant or variant == report_year:
                continue
            pos = low.find(variant)
            if pos >= 0:
                candidates.append((20, pos + len(variant) // 2))

        # Fallback: use the year in header lines. If the report year appears
        # multiple times, keep all positions and later use period logic.
        for m in re.finditer(rf"\b{re.escape(report_year)}\b", low):
            candidates.append((5, m.start() + 2))

    if candidates:
        candidates = sorted(candidates, key=lambda x: (x[0], x[1]), reverse=True)
        # For equal-quality exact year candidates, the rightmost is usually
        # current in MSFT-style tables; exact date variants above override this.
        return candidates[0][1]

    # No usable header position. Use a broad fallback.
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
    """Return (start, end, value) for statement numbers on one fixed-width row."""
    # Match accounting tokens: $ 1,234, (1,234), --, -. Avoid normal words.
    token_re = re.compile(r"(?<![A-Za-z])(?:\$\s*)?(?:\([0-9][0-9,]*(?:\.[0-9]+)?\)|[0-9][0-9,]*(?:\.[0-9]+)?|--|-)(?![A-Za-z])")
    spans = []
    for m in token_re.finditer(line):
        raw = m.group(0).strip()
        # Avoid picking page numbers / row separator fragments in mostly empty lines.
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


def choose_plain_text_value(spans: list[tuple[int, int, float]], target_pos: int | None, form: str | None) -> float | None:
    if not spans:
        return None
    if target_pos is not None:
        return min(spans, key=lambda x: abs(((x[0] + x[1]) // 2) - target_pos))[2]
    # 10-K tables are usually chronological left-to-right with current year last.
    # Old 10-Q text tables usually put current year last, except where exact
    # date matching above should already identify current position.
    return spans[-1][2]


def extract_fields_from_plain_text(path: Path, report_date: str, form: str | None) -> dict:
    try:
        text = read_text(path)
    except Exception:
        return {}

    block = extract_cash_flow_text_block(text)
    if not block:
        return {}

    target_pos = find_plain_text_target_position(block, report_date, form)
    extracted: dict[str, float] = {}

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
            # Accumulate continuation labels such as "Adjustments to reconcile..."
            # but ignore pure section headings.
            if label_piece and label_piece not in {"operations", "operating", "financing", "investing"}:
                pending_label = label_piece if len(label_piece) > 8 else pending_label
            continue

        label = row_label_from_plain_line(line, spans)
        if not label and pending_label:
            label = pending_label
        elif pending_label and len(label) < 8:
            label = normalize_label(pending_label + " " + label)
        pending_label = ""

        if not label:
            continue
        if label in {"operations", "operating", "financing", "investing"}:
            continue

        for field, patterns in FIELD_PATTERNS.items():
            if field in extracted or field not in OUTPUT_COLUMNS:
                continue
            if label_matches(label, patterns):
                value = choose_plain_text_value(spans, target_pos, form)
                if value is not None:
                    value = field_value_from_label(field, value)
                    extracted[field] = value * VALUE_MULTIPLIER
                break

    return extracted




def filing_explicitly_says_no_common_dividends(path: Path) -> bool:
    """
    Conservative zero-fill rule for dividends.

    We only set commonDividendsPaid/netDividendsPaid to 0 when the filing text
    explicitly states that the company has not paid cash dividends on common
    stock. This avoids incorrectly zero-filling rows where a dividend line was
    merely missed by the parser.
    """
    try:
        text = read_text(path)
    except Exception:
        return False

    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = normalize_label(text)

    no_dividend_patterns = [
        r"has not paid cash dividends on (?:its )?common stock",
        r"have not paid cash dividends on (?:its |our )?common stock",
        r"has never paid cash dividends on (?:its )?common stock",
        r"have never paid cash dividends on (?:our )?common stock",
        r"no cash dividends (?:have been|were) paid on (?:its |our )?common stock",
        r"we have not declared or paid any cash dividends",
    ]
    return any(re.search(pattern, text) for pattern in no_dividend_patterns)


def apply_explicit_no_dividend_zero(result: dict, path: Path) -> dict:
    """
    Fill dividend fields with zero only when the filing itself clearly says no
    common dividends were paid. This is source-based, not a blanket imputation.
    """
    if filing_explicitly_says_no_common_dividends(path):
        if pd.isna(result.get("commonDividendsPaid")):
            result["commonDividendsPaid"] = 0.0
        if pd.isna(result.get("netDividendsPaid")):
            result["netDividendsPaid"] = 0.0
    return result


def apply_financing_section_no_dividend_zero(result: dict) -> dict:
    """
    Zero-fill missing common dividends only after the filing's financing cash-flow
    section was successfully parsed. In a complete cash-flow statement, a company
    that paid common dividends normally reports a dividend/cash-dividend line in
    financing activities. If the financing section is present but no dividend
    line is reported, treating commonDividendsPaid as 0 is usually safer than
    leaving a false missing value for TTM dividend-yield construction.
    """
    if not ZERO_FILL_MISSING_COMMON_DIVIDENDS_WHEN_FINANCING_CF_PARSED:
        return result

    financing_section_parsed = (
        pd.notna(result.get("netCashProvidedByFinancingActivities"))
        or pd.notna(result.get("commonStockIssuance"))
        or pd.notna(result.get("commonStockRepurchased"))
    )

    if financing_section_parsed and pd.isna(result.get("commonDividendsPaid")):
        result["commonDividendsPaid"] = 0.0
    if financing_section_parsed and pd.isna(result.get("netDividendsPaid")):
        result["netDividendsPaid"] = result.get("commonDividendsPaid")

    return result

# ============================================================
# EXTRACTION
# ============================================================

def empty_result(report_date: str | None) -> dict:
    return {col: pd.NA for col in OUTPUT_COLUMNS} | {"date": report_date}


def apply_derived_fields(result: dict) -> dict:
    """
    Conservative post-processing.

    Rule for this project: extract directly reported filing attributes only.
    Do NOT derive analytical fields such as freeCashFlow, netDebtIssuance,
    or netCommonStockIssuance here. Those should be computed downstream in
    the factor-building pipeline when needed.

    We only mirror exact direct aliases when the same reported line item has
    two output names.
    """
    if pd.isna(result.get("operatingCashFlow")) and pd.notna(result.get("netCashProvidedByOperatingActivities")):
        result["operatingCashFlow"] = result["netCashProvidedByOperatingActivities"]

    if pd.isna(result.get("investmentsInPropertyPlantAndEquipment")) and pd.notna(result.get("capitalExpenditure")):
        result["investmentsInPropertyPlantAndEquipment"] = result["capitalExpenditure"]

    if pd.isna(result.get("netDividendsPaid")) and pd.notna(result.get("commonDividendsPaid")):
        result["netDividendsPaid"] = result["commonDividendsPaid"]

    return result


def extract_cash_flow_from_filing(path: Path) -> dict:
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
    candidate_tables = choose_cash_flow_tables(tables)
    if candidate_tables:
        for table in candidate_tables:
            fallback = extract_fields_from_table(table, report_date, form)
            for key, value in fallback.items():
                if key in result and pd.isna(result[key]):
                    result[key] = value
    else:
        # Plain-text SEC fallback for old .txt filings where read_html returns zero tables.
        fallback = extract_fields_from_plain_text(path, report_date, form)
        if fallback:
            print(f"[plain-text cash-flow fallback used] {path.name}")
            for key, value in fallback.items():
                if key in result and pd.isna(result[key]):
                    result[key] = value
        else:
            print(f"[no cash-flow table found] {path.name}")

    result = apply_explicit_no_dividend_zero(result, path)
    result = apply_financing_section_no_dividend_zero(result)
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


def extract_cash_flows_from_folder(filings_dir: Path) -> pd.DataFrame:
    files = list_filing_files(filings_dir)
    print(f"Found {len(files)} filing files in {filings_dir}")
    rows = []
    for path in files:
        print(f"\nParsing: {path.name}")
        row = extract_cash_flow_from_filing(path)
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
# BATCH-COMPATIBLE EXTRACTION HELPERS
# ============================================================

def parse_accession_from_filename(path: Path) -> str | None:
    """
    Extract dashed SEC accession number from filenames created by the rolling
    downloader. Example: ..._000032019324000123_... -> 0000320193-24-000123.
    """
    m = re.search(r"_(\d{18})(?:_|\.)", path.name)
    if not m:
        return None

    s = m.group(1)
    return f"{s[:10]}-{s[10:12]}-{s[12:]}"


def extract_cash_flows_from_ticker_folder(
    ticker_dir: Path,
    ticker: str | None = None,
    batch_id: str | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Extract cash flows fields from one ticker folder.
    This folder should contain the downloaded 10-K / 10-Q files for one ticker.
    """
    ticker_dir = Path(ticker_dir)
    ticker = ticker or ticker_dir.name

    files = list_filing_files(ticker_dir)
    print(f"Found {len(files)} filing files for {ticker} in {ticker_dir}")

    rows = []
    status_rows = []

    for path in files:
        print(f"\n[{ticker}] Parsing: {path.name}")

        accession_number = parse_accession_from_filename(path)
        form = parse_form_from_filename(path)
        report_date = parse_report_date_from_filename(path)

        status = {
            "batch_id": batch_id or "",
            "ticker": ticker,
            "accession_number": accession_number or "",
            "form": form or "",
            "report_date": report_date or "",
            "source_file": path.name,
            "source_path": str(path),
            "status": "",
            "num_extracted_fields": 0,
            "error": "",
        }

        try:
            row = extract_cash_flow_from_filing(path)
            row["ticker"] = ticker
            row["accession_number"] = accession_number
            row["form"] = form
            row["batch_id"] = batch_id or ""
            row["source_file"] = path.name
            row["source_path"] = str(path)

            extracted_fields = [
                col for col in OUTPUT_COLUMNS
                if col != "date" and pd.notna(row.get(col))
            ]

            print(f"Extracted fields: {extracted_fields}")

            status["status"] = "success"
            status["num_extracted_fields"] = len(extracted_fields)
            rows.append(row)

        except Exception as e:
            print(f"[cash-flows extraction failed] {path.name}: {e}")
            status["status"] = "failed"
            status["error"] = str(e)

        status_rows.append(status)

    extra_cols = ["ticker", "accession_number", "form", "batch_id", "source_file", "source_path"]
    out_cols = extra_cols[:1] + OUTPUT_COLUMNS + extra_cols[1:]

    if rows:
        df = pd.DataFrame(rows)
        df = df[df["date"].notna()].copy()
        for col in out_cols:
            if col not in df.columns:
                df[col] = pd.NA
        df = df[out_cols]
        df = df.sort_values(["ticker", "date", "accession_number"], ascending=[True, False, False]).reset_index(drop=True)
    else:
        df = pd.DataFrame(columns=out_cols)

    status_df = pd.DataFrame(status_rows)
    return df, status_df


def safe_ticker_filename(ticker: str) -> str:
    """
    Convert ticker to a safe filename stem while preserving common SEC ticker notation.
    Examples:
        AAPL -> AAPL
        BRK-B -> BRK-B
        BRK.B -> BRK-B
    """
    ticker = str(ticker or "").strip().upper().replace(".", "-")
    ticker = re.sub(r"[^A-Z0-9_-]", "_", ticker)
    ticker = re.sub(r"_+", "_", ticker).strip("_")
    return ticker or "UNKNOWN"


def merge_deduplicate_extractions(old_df: pd.DataFrame, new_df: pd.DataFrame) -> pd.DataFrame:
    """
    Append new extraction rows to old rows and deduplicate.

    Priority:
    1. Use ticker + accession_number when accession_number exists.
    2. Fall back to ticker + date + source_file for legacy rows.
    """
    if old_df is None or old_df.empty:
        combined = new_df.copy()
    elif new_df is None or new_df.empty:
        combined = old_df.copy()
    else:
        combined = pd.concat([old_df, new_df], ignore_index=True)

    if combined.empty:
        return combined

    for col in ["ticker", "accession_number", "date", "source_file"]:
        if col not in combined.columns:
            combined[col] = ""

    has_acc = combined["accession_number"].astype(str).str.len() > 0

    with_acc = combined[has_acc].drop_duplicates(
        ["ticker", "accession_number"],
        keep="last",
    )

    without_acc = combined[~has_acc].drop_duplicates(
        ["ticker", "date", "source_file"],
        keep="last",
    )

    combined = pd.concat([with_acc, without_acc], ignore_index=True)

    if "ticker" in combined.columns and "date" in combined.columns:
        combined = combined.sort_values(
            ["ticker", "date", "accession_number"],
            ascending=[True, False, False],
        )

    return combined.reset_index(drop=True)


def append_ticker_output(
    ticker_df: pd.DataFrame,
    output_dir: Path,
    ticker: str,
    statement_name: str = "cash_flow",
) -> Path:
    """
    Save/update one independent CSV per ticker.

    Example output:
        data/processed/sec_cash_flow/AAPL_cash_flow.csv

    If the ticker file already exists, new rows are appended and deduplicated
    by ticker + accession_number.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    ticker_safe = safe_ticker_filename(ticker)
    ticker_output = output_dir / f"{ticker_safe}.csv"

    if ticker_df is None or ticker_df.empty:
        # Do not create empty ticker files.
        print(f"[{ticker}] No extracted rows; ticker file not updated.")
        return ticker_output

    if ticker_output.exists():
        old = pd.read_csv(ticker_output, dtype=str, keep_default_na=False, na_filter=False)
        combined = merge_deduplicate_extractions(old, ticker_df)
    else:
        combined = merge_deduplicate_extractions(pd.DataFrame(), ticker_df)

    combined.to_csv(ticker_output, index=False)
    print(f"[{ticker}] Updated ticker output: {ticker_output} ({len(combined)} rows)")
    return ticker_output


def extract_cash_flows_from_batch_folder(
    input_dir: Path,
    output_dir: Path,
    batch_id: str | None = None,
    update_master: bool = True,
    update_ticker_files: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Extract cash flow fields from all ticker subfolders
    in one rolling batch folder.

    Writes:
        cash_flow_batch_<batch_id>.csv
        cash_flow_extraction_status_<batch_id>.csv
        <TICKER>_cash_flow.csv for each ticker (append + deduplicate)
        cash_flow_all.csv  (optional deduplicated cross-ticker master)
    """
    input_dir = Path(input_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not input_dir.exists():
        raise FileNotFoundError(f"Input directory not found: {input_dir}")

    ticker_dirs = sorted([p for p in input_dir.iterdir() if p.is_dir()])

    print(f"Input batch folder: {input_dir}")
    print(f"Output folder: {output_dir}")
    print(f"Found {len(ticker_dirs)} ticker folders.")

    all_rows = []
    all_status = []

    for ticker_dir in ticker_dirs:
        ticker = ticker_dir.name

        df_ticker, status_ticker = extract_cash_flows_from_ticker_folder(
            ticker_dir=ticker_dir,
            ticker=ticker,
            batch_id=batch_id,
        )

        if update_ticker_files:
            append_ticker_output(
                ticker_df=df_ticker,
                output_dir=output_dir,
                ticker=ticker,
                statement_name="cash_flow",
            )

        all_rows.append(df_ticker)
        all_status.append(status_ticker)

    if all_rows:
        batch_df = pd.concat(all_rows, ignore_index=True)
    else:
        batch_df = pd.DataFrame()

    if all_status:
        status_df = pd.concat(all_status, ignore_index=True)
    else:
        status_df = pd.DataFrame()

    batch_label = batch_id or input_dir.name
    batch_output = output_dir / f"cash_flow_batch_{batch_label}.csv"
    status_output = output_dir / f"cash_flow_extraction_status_{batch_label}.csv"

    batch_df.to_csv(batch_output, index=False)
    status_df.to_csv(status_output, index=False)

    print("\nBatch extraction done.")
    print(f"Saved batch output to: {batch_output}")
    print(f"Saved status output to: {status_output}")

    if update_master:
        master_output = output_dir / "cash_flow_all.csv"

        if master_output.exists():
            old = pd.read_csv(master_output, dtype=str, keep_default_na=False, na_filter=False)
            combined = merge_deduplicate_extractions(old, batch_df)
        else:
            combined = merge_deduplicate_extractions(pd.DataFrame(), batch_df)

        combined.to_csv(master_output, index=False)
        print(f"Updated master output: {master_output}")

    return batch_df, status_df


# ============================================================
# CLI
# ============================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract cash flows fields from SEC filings.")

    parser.add_argument(
        "--input-dir",
        type=str,
        default=str(FILINGS_DIR),
        help="Ticker folder or rolling batch folder containing ticker subfolders.",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="data/processed/sec_cash_flow",
        help="Output directory for extracted cash flows CSV files.",
    )
    parser.add_argument(
        "--output-csv",
        type=str,
        default=str(OUTPUT_CSV),
        help="Output CSV for single-ticker mode.",
    )
    parser.add_argument(
        "--batch-id",
        type=str,
        default=None,
        help="Batch ID label, e.g. 00001. If omitted, input folder name is used.",
    )
    parser.add_argument(
        "--mode",
        choices=["single", "batch"],
        default="batch",
        help="single = input-dir is one ticker folder; batch = input-dir contains ticker subfolders.",
    )
    parser.add_argument(
        "--no-master",
        action="store_true",
        help="Do not update cash_flow_all.csv in batch mode.",
    )

    parser.add_argument(
        "--no-ticker-files",
        action="store_true",
        help="Do not update per-ticker CSV files in batch mode.",
    )

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    input_dir = Path(args.input_dir)

    if args.mode == "single":
        df = extract_cash_flows_from_folder(input_dir)
        output_csv = Path(args.output_csv)
        output_csv.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(output_csv, index=False)

        print("\nDone.")
        print(f"Saved to: {output_csv}")

        print("\nMissing counts:")
        print(df[OUTPUT_COLUMNS].isna().sum())

        print("\nPreview:")
        print(df.head(20).to_string(index=False))

    else:
        batch_label = args.batch_id or input_dir.name
        extract_cash_flows_from_batch_folder(
            input_dir=input_dir,
            output_dir=Path(args.output_dir),
            batch_id=batch_label,
            update_master=not args.no_master,
            update_ticker_files=not args.no_ticker_files,
        )
