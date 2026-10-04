"""Standard templates every site should have, created where missing (never overwritten).

    bench --site demo execute mm_core.setup_templates.apply              # every company on the site
    bench --site demo execute mm_core.setup_templates.apply --kwargs "{'company': 'MicroMax Spinning Demo'}"

Site-wide (shared by all companies):
  - Payment Terms + Payment Terms Templates: Advance, Cash on Delivery, Net 7..90, split advance terms, LC terms.
  - Terms and Conditions: sales, purchase, export sales and quotation terms.
Per company:
  - Journal Entry Templates for routine vouchers, on that company's own default accounts ("Bank Charges - MEPL").
    A template whose account the company hasn't set is skipped (and reported), never created half-empty.
  - A Holiday List for the current fiscal year — Sundays and Pakistan's fixed public holidays — set as the
    company default, only when the company has none. Eid / Muharram / Eid Milad-un-Nabi depend on the moon:
    add them to the list once announced.
Existing records with the same name are left exactly as they are.
"""

import frappe
from frappe.utils import add_days, getdate

AFTER_INVOICE = "Day(s) after invoice date"

# name -> (portion %, credit days, description)
PAYMENT_TERMS = {
    "Advance 100%": (100, 0, "Full payment in advance, before delivery."),
    "Cash on Delivery": (100, 0, "Full payment on delivery."),
    "Net 7": (100, 7, "Full payment within 7 days of the invoice."),
    "Net 15": (100, 15, "Full payment within 15 days of the invoice."),
    "Net 30": (100, 30, "Full payment within 30 days of the invoice."),
    "Net 45": (100, 45, "Full payment within 45 days of the invoice."),
    "Net 60": (100, 60, "Full payment within 60 days of the invoice."),
    "Net 90": (100, 90, "Full payment within 90 days of the invoice."),
    "Advance 30%": (30, 0, "30% of the order value in advance."),
    "Balance 70% on Delivery": (70, 0, "Remaining 70% on delivery."),
    "Advance 50%": (50, 0, "50% of the order value in advance."),
    "Balance 50% after 30 Days": (50, 30, "Remaining 50% within 30 days of the invoice."),
    "LC at Sight": (100, 0, "Irrevocable letter of credit, payable at sight."),
    "LC 90 Days": (100, 90, "Usance letter of credit, payable 90 days after the invoice / bill of lading."),
    "LC 120 Days": (100, 120, "Usance letter of credit, payable 120 days after the invoice / bill of lading."),
}

# template -> payment terms (portions must total 100)
PAYMENT_TERMS_TEMPLATES = {
    "Advance": ["Advance 100%"],
    "Cash on Delivery": ["Cash on Delivery"],
    "Net 7": ["Net 7"],
    "Net 15": ["Net 15"],
    "Net 30": ["Net 30"],
    "Net 45": ["Net 45"],
    "Net 60": ["Net 60"],
    "Net 90": ["Net 90"],
    "30% Advance, 70% on Delivery": ["Advance 30%", "Balance 70% on Delivery"],
    "50% Advance, 50% after 30 Days": ["Advance 50%", "Balance 50% after 30 Days"],
    "LC at Sight": ["LC at Sight"],
    "LC 90 Days": ["LC 90 Days"],
    "LC 120 Days": ["LC 120 Days"],
}

# title -> (selling, buying, terms html)
TERMS_AND_CONDITIONS = {
    "Standard Sales Terms": (1, 0, """<ol>
<li><b>Prices</b> are in the quoted currency, exclusive of sales tax and other government levies, which are charged at the rates in force on the invoice date.</li>
<li><b>Payment</b> is due as per the payment terms on the order or invoice. Overdue amounts may stop further deliveries.</li>
<li><b>Delivery</b> dates are estimates; risk passes to the buyer on dispatch from our premises unless agreed otherwise in writing.</li>
<li><b>Claims</b> for shortage or visible damage must be made in writing within 7 days of receipt, before the goods are used or processed.</li>
<li><b>Returns</b> are accepted only with our prior written approval.</li>
<li>Any dispute is subject to the jurisdiction of the courts of Pakistan.</li>
</ol>"""),
    "Standard Purchase Terms": (0, 1, """<ol>
<li>Goods must match the specification, quantity and quality on this order; non-conforming goods may be rejected at the supplier's cost.</li>
<li>Deliveries must reach the address on this order by the required date; delays must be notified in advance.</li>
<li>Each delivery must carry a delivery challan quoting this order number; invoices must show the applicable taxes and registration numbers.</li>
<li>Payment will be made as per the payment terms on this order, after receipt and acceptance of the goods and a correct invoice.</li>
<li>Applicable withholding taxes will be deducted at source as required by law.</li>
<li>Any dispute is subject to the jurisdiction of the courts of Pakistan.</li>
</ol>"""),
    "Export Sales Terms": (1, 0, """<ol>
<li><b>Incoterms</b>: as stated on the proforma / contract (Incoterms 2020).</li>
<li><b>Payment</b>: as stated on the proforma — by irrevocable letter of credit or as agreed; all bank charges outside Pakistan are for the buyer's account.</li>
<li><b>Shipment</b>: within the period stated on the proforma; partial shipments and transhipment allowed unless stated otherwise.</li>
<li><b>Quantity / weight</b>: a tolerance of ±5% is allowed.</li>
<li><b>Claims</b> on quality must be raised within 30 days of arrival at destination, supported by an independent inspection report, before the goods are processed.</li>
<li>Any dispute is subject to the jurisdiction of the courts of Pakistan.</li>
</ol>"""),
    "Quotation Terms": (1, 0, """<ol>
<li>This quotation is valid for 15 days from its date.</li>
<li>Prices are exclusive of sales tax unless stated otherwise.</li>
<li>Delivery time is counted from receipt of a confirmed order and any agreed advance.</li>
<li>Payment as per the payment terms stated on this quotation.</li>
</ol>"""),
}

# Pakistan's fixed public holidays (month, day, name). Moon-dependent ones are added by hand once announced.
FIXED_HOLIDAYS = [
    (2, 5, "Kashmir Solidarity Day"),
    (3, 23, "Pakistan Day"),
    (5, 1, "Labour Day"),
    (5, 28, "Youm-e-Takbeer"),
    (8, 14, "Independence Day"),
    (11, 9, "Iqbal Day"),
    (12, 25, "Quaid-e-Azam Day"),
]


def _report(out, kind, name, state):
    out.setdefault(kind, {}).setdefault(state, []).append(name)


def _payment_terms(out):
    for name, (portion, days, desc) in PAYMENT_TERMS.items():
        if frappe.db.exists("Payment Term", name):
            _report(out, "Payment Term", name, "exists")
            continue
        frappe.get_doc({"doctype": "Payment Term", "payment_term_name": name, "invoice_portion": portion,
                        "due_date_based_on": AFTER_INVOICE, "credit_days": days, "description": desc}).insert(ignore_permissions=True)
        _report(out, "Payment Term", name, "created")
    for name, terms in PAYMENT_TERMS_TEMPLATES.items():
        if frappe.db.exists("Payment Terms Template", name):
            _report(out, "Payment Terms Template", name, "exists")
            continue
        rows = []
        for t in terms:
            portion, days, desc = PAYMENT_TERMS[t]
            rows.append({"payment_term": t, "invoice_portion": portion, "due_date_based_on": AFTER_INVOICE,
                         "credit_days": days, "description": desc})
        frappe.get_doc({"doctype": "Payment Terms Template", "template_name": name, "terms": rows}).insert(ignore_permissions=True)
        _report(out, "Payment Terms Template", name, "created")


def _terms_and_conditions(out):
    has_hr = frappe.get_meta("Terms and Conditions").has_field("hr")
    for title, (selling, buying, terms) in TERMS_AND_CONDITIONS.items():
        if frappe.db.exists("Terms and Conditions", title):
            _report(out, "Terms and Conditions", title, "exists")
            continue
        doc = {"doctype": "Terms and Conditions", "title": title, "selling": selling, "buying": buying, "terms": terms}
        if has_hr:
            doc["hr"] = 0
        frappe.get_doc(doc).insert(ignore_permissions=True)
        _report(out, "Terms and Conditions", title, "created")


def _account(company, *, field=None, account_type=None, name_like=None, root_type=None):
    """A leaf account of `company`: the company default `field`, else by type, else by name."""
    if field:
        value = frappe.get_cached_value("Company", company, field)
        if value:
            return value
    filters = {"company": company, "is_group": 0, "disabled": 0}
    if root_type:
        filters["root_type"] = root_type
    if account_type:
        hit = frappe.get_all("Account", filters={**filters, "account_type": account_type}, pluck="name", order_by="lft", limit=1)
        if hit:
            return hit[0]
    if name_like:
        # Prefer the plain name ("Miscellaneous Expenses") over variants ("Miscellaneous Expenses-Mill").
        hits = frappe.get_all("Account", filters={**filters, "account_name": ["like", f"%{name_like}%"]},
                              fields=["name", "account_name"], order_by="lft")
        exact = [h.name for h in hits if (h.account_name or "").strip().lower() == name_like.lower()]
        if exact or hits:
            return (exact or [h.name for h in hits])[0]
    return None


def _journal_templates(company, out):
    abbr = frappe.get_cached_value("Company", company, "abbr")
    bank = _account(company, field="default_bank_account", account_type="Bank")
    cash = _account(company, field="default_cash_account", account_type="Cash")
    acc = {
        "bank": bank,
        "cash": cash,
        "bank_charges": _account(company, name_like="Bank Charges", root_type="Expense"),
        "misc_expense": _account(company, name_like="Miscellaneous Expenses", root_type="Expense"),
        "payroll_payable": _account(company, field="default_payroll_payable_account"),
        "depreciation": _account(company, field="depreciation_expense_account", account_type="Depreciation"),
        "accumulated_depreciation": _account(company, field="accumulated_depreciation_account", account_type="Accumulated Depreciation"),
        "write_off": _account(company, field="write_off_account"),
        "receivable": _account(company, field="default_receivable_account", account_type="Receivable"),
        "temporary_opening": _account(company, account_type="Temporary"),
    }
    # title -> (voucher type, is_opening, [account keys])
    templates = {
        "Bank Charges": ("Bank Entry", "No", ["bank_charges", "bank"]),
        "Cash Deposit to Bank": ("Contra Entry", "No", ["bank", "cash"]),
        "Cash Withdrawal from Bank": ("Contra Entry", "No", ["cash", "bank"]),
        "Petty Cash Expense": ("Cash Entry", "No", ["misc_expense", "cash"]),
        "Salary Payment": ("Bank Entry", "No", ["payroll_payable", "bank"]),
        "Depreciation": ("Depreciation Entry", "No", ["depreciation", "accumulated_depreciation"]),
        "Write Off": ("Write Off Entry", "No", ["write_off", "receivable"]),
        "Opening Balance": ("Opening Entry", "Yes", ["temporary_opening"]),
    }
    series = (frappe.get_meta("Journal Entry").get_field("naming_series").options or "").split("\n")[0]
    for title, (vtype, opening, keys) in templates.items():
        name = f"{title} - {abbr}"
        if frappe.db.exists("Journal Entry Template", name):
            _report(out, "Journal Entry Template", name, "exists")
            continue
        missing = [k for k in keys if not acc[k]]
        if missing:
            _report(out, "Journal Entry Template", f"{name} (no {', '.join(missing)} account)", "skipped")
            continue
        frappe.get_doc({"doctype": "Journal Entry Template", "template_title": name, "company": company,
                        "voucher_type": vtype, "is_opening": opening, "naming_series": series,
                        "accounts": [{"account": acc[k]} for k in keys]}).insert(ignore_permissions=True)
        _report(out, "Journal Entry Template", name, "created")


def _holiday_list(company, out):
    if frappe.get_cached_value("Company", company, "default_holiday_list"):
        _report(out, "Holiday List", f"{company} (has a default)", "exists")
        return
    from erpnext.accounts.utils import get_fiscal_year

    fy, start, end = get_fiscal_year(frappe.utils.today(), company=company)[:3]
    start, end = getdate(start), getdate(end)
    abbr = frappe.get_cached_value("Company", company, "abbr")
    name = f"Holidays {fy} - {abbr}"
    if not frappe.db.exists("Holiday List", name):
        days = {}
        d = start
        while d <= end:
            if d.weekday() == 6:
                days[d] = ("Sunday", 1)
            d = add_days(d, 1)
        for year in range(start.year, end.year + 1):
            for month, day, label in FIXED_HOLIDAYS:
                hd = getdate(f"{year}-{month:02d}-{day:02d}")
                if start <= hd <= end:
                    days[hd] = (label, 0)
        frappe.get_doc({
            "doctype": "Holiday List", "holiday_list_name": name, "from_date": start, "to_date": end,
            "weekly_off": "Sunday", "country": "Pakistan" if frappe.get_meta("Holiday List").has_field("country") else None,
            "holidays": [{"holiday_date": hd, "description": label, "weekly_off": wo} for hd, (label, wo) in sorted(days.items())],
        }).insert(ignore_permissions=True)
        _report(out, "Holiday List", name, "created")
    frappe.db.set_value("Company", company, "default_holiday_list", name)
    _report(out, "Holiday List", f"{name} -> default for {company}", "set")


def apply(company: str | None = None) -> dict:
    """Create the standard templates that are missing on this site (see module docstring)."""
    out = {}
    _payment_terms(out)
    _terms_and_conditions(out)
    for c in ([company] if company else frappe.get_all("Company", pluck="name")):
        _journal_templates(c, out)
        _holiday_list(c, out)
    frappe.db.commit()
    for kind, states in out.items():
        for state, names in states.items():
            print(f"{kind:24} {state:8} {len(names):3}  " + ", ".join(names))
    return out
