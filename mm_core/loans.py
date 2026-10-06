"""Salary loans: eligibility, guarantor rules, loan history and instalments.

A loan is an Employee Advance with "Salary loan" ticked (custom fields from mm_core.custom_fields): paid out like
any advance, then recovered in equal monthly instalments by a recurring Additional Salary linked to it, so each
salary slip reduces the advance's balance (HRMS's own advance-return flow).

The policy (HR Settings → Salary Loans tab) decides who may borrow and how much:
- employment type and minimum service;
- maximum loan = a multiple of average gross pay (last 3 salary slips);
- maximum instalment = a share of average net pay, maximum tenure in months;
- how many loans may be outstanding at once, and no defaulted (old, unrecovered) advance;
- a guarantor: eligible type, minimum service, no outstanding loan of their own, a cap on loans they back.

check_eligibility() returns every check with its result; validate() / before_submit() (doc_events) store the
result on the loan and block submission when it fails, unless an HR Manager gives an override reason.


The HR Settings → Salary Loans tab now has the default policy filled in, on both local sites (micromaxerp and wise). I checked the stored values in the database; I haven't opened the tab in the browser.

Field	Value
Eligible Employment Types =	Full-time
Minimum Service (months) =	12
Maximum Loan (x average gross salary) =	3
Maximum Instalment (% of average net pay) =	30
Maximum Tenure (months) =	12
Other Loans Allowed While One Is Outstanding =	0
Guarantor Required =	Always
Guarantor Minimum Service (months) =	24
Loans One Guarantor May Back =	2
Recovery Salary Component =	Loan Recovery (a Deduction component, created if missing)
How it behaves:

Only empty fields are filled. A value you change, including a deliberate 0, is never overwritten.
It runs automatically after every migrate and on a new install. When you deploy, production (demo) and the school site get the same defaults without a manual step.
The settings form shows exactly what the eligibility check enforces, and editing a value changes the rule straight away.
Changes are in loans.py (new ensure_policy_defaults) and hooks.py. Backend only, in mm_core; it applies on the next full deploy:

"""

import frappe
from frappe import _
from frappe.utils import add_days, add_months, cint, date_diff, flt, fmt_money, get_first_day, get_last_day, getdate, month_diff, nowdate

DEFAULTS = {
    "employment_types": "Full-time", "min_service_months": 12, "salary_multiple": 3.0, "max_installment_pct": 30.0,
    "max_months": 12, "max_active": 0, "guarantor_required": "Always", "guarantor_min_service_months": 24,
    "guarantor_max_guarantees": 2, "component": None,
}
DEFAULT_COMPONENT = "Loan Recovery"


def policy():
    p = dict(DEFAULTS)
    # Raw Singles rows: a setting never saved has no row (get_single_value would return 0 for numbers, which is a
    # real value for "Other loans allowed"), so only saved values override the defaults.
    saved = dict(frappe.db.sql("""select field, value from `tabSingles` where doctype = 'HR Settings' and field like 'mm\\_loan\\_%%'"""))
    for key in DEFAULTS:
        v = saved.get(f"mm_loan_{key}")
        if v not in (None, ""):
            p[key] = v
    p["employment_types"] = [t.strip() for t in str(p["employment_types"] or "").splitlines() if t.strip()]
    for k in ("min_service_months", "max_months", "max_active", "guarantor_min_service_months", "guarantor_max_guarantees"):
        p[k] = cint(p[k])
    for k in ("salary_multiple", "max_installment_pct"):
        p[k] = flt(p[k])
    return p


def ensure_policy_defaults():
    """Fill HR Settings → Salary Loans with DEFAULTS where a value was never saved (after install / migrate), so the
    tab shows the policy in force. Saved values — including a deliberate 0 — are never overwritten."""
    if not frappe.db.exists("DocType", "HR Settings") or not frappe.get_meta("HR Settings").has_field("mm_loan_min_service_months"):
        return
    if not frappe.db.exists("Salary Component", DEFAULT_COMPONENT) and frappe.db.exists("DocType", "Salary Component"):
        frappe.get_doc({"doctype": "Salary Component", "salary_component": DEFAULT_COMPONENT, "salary_component_abbr": "LR",
                        "type": "Deduction", "depends_on_payment_days": 0,
                        "description": "Salary loan instalment (mm_core.loans)"}).insert(ignore_permissions=True)
    values = {**DEFAULTS, "component": DEFAULT_COMPONENT}
    saved = {r[0] for r in frappe.db.sql("""select field from `tabSingles` where doctype = 'HR Settings' and field like 'mm\\_loan\\_%%'""")}
    for key, value in values.items():
        field = f"mm_loan_{key}"
        if field not in saved and value is not None:
            frappe.db.set_single_value("HR Settings", field, value, update_modified=False)
    frappe.db.commit()


def _service_months(doj, on):
    return max(0, month_diff(on, doj) - 1) if doj else 0


def _pay(employee):
    """Average gross and net of the last 3 submitted salary slips (fallback: the salary structure assignment base)."""
    slips = frappe.get_all("Salary Slip", {"employee": employee, "docstatus": 1}, ["gross_pay", "net_pay", "end_date"],
                           order_by="end_date desc", limit=3)
    if slips:
        return {"gross": flt(sum(s.gross_pay for s in slips) / len(slips), 2), "net": flt(sum(s.net_pay for s in slips) / len(slips), 2),
                "basis": f"average of last {len(slips)} salary slip{'s' if len(slips) > 1 else ''}"}
    base = flt(frappe.db.get_value("Salary Structure Assignment", {"employee": employee, "docstatus": 1}, "base", order_by="from_date desc"))
    return {"gross": base, "net": base, "basis": "salary structure base (no salary slips yet)" if base else "no salary data"}


def _outstanding(a):
    return max(0.0, flt(a.paid_amount) - flt(a.claimed_amount) - flt(a.return_amount))


def _loans(employee, exclude=None):
    """Every submitted advance of the employee, with outstanding balance and its scheduled monthly recovery."""
    rows = frappe.get_all("Employee Advance", {"employee": employee, "docstatus": 1, "name": ["!=", exclude or ""]},
                          ["name", "posting_date", "advance_amount", "paid_amount", "claimed_amount", "return_amount", "status",
                           "purpose", "mm_is_loan", "mm_installment_months", "mm_monthly_installment", "mm_guarantor_name"],
                          order_by="posting_date desc")
    today = getdate(nowdate())
    for r in rows:
        r.outstanding = _outstanding(r)
        r.scheduled = flt(frappe.db.sql("""select sum(amount) from `tabAdditional Salary`
            where ref_doctype = 'Employee Advance' and ref_docname = %s and docstatus = 1""", r.name)[0][0])
        months = cint(r.mm_installment_months) or 6
        # A default: still owed well past its term with nothing scheduled to recover it.
        r.defaulted = bool(r.outstanding > 0 and not r.scheduled and date_diff(today, r.posting_date) > (months + 3) * 30)
    return rows


def _guarantees(guarantor, exclude=None):
    rows = frappe.get_all("Employee Advance", {"mm_guarantor": guarantor, "docstatus": 1, "name": ["!=", exclude or ""]},
                          ["name", "employee_name", "paid_amount", "claimed_amount", "return_amount"])
    return [r for r in rows if _outstanding(r) > 0]


@frappe.whitelist()
def check_eligibility(employee: str, amount: float | None = None, months: int | None = None, guarantor: str | None = None,
                      exclude: str | None = None) -> dict:
    """Every policy check for a proposed loan, the amount the employee is eligible for, and their loan history."""
    if not frappe.has_permission("Employee Advance", "read"):
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    p, today = policy(), getdate(nowdate())
    amount, months = flt(amount), cint(months)
    emp = frappe.db.get_value("Employee", employee, ["name", "employee_name", "status", "employment_type", "date_of_joining",
                                                     "relieving_date", "designation", "department", "company"], as_dict=True)
    if not emp:
        frappe.throw(_("Employee {0} not found").format(employee))
    currency = frappe.get_cached_value("Company", emp.company, "default_currency") or "PKR"
    money = lambda v: fmt_money(v, currency=currency)  # noqa: E731
    service = _service_months(emp.date_of_joining, today)
    pay = _pay(employee)
    loans = _loans(employee, exclude)
    active = [l for l in loans if l.outstanding > 0]
    outstanding = sum(l.outstanding for l in active)

    max_by_salary = flt(pay["gross"] * p["salary_multiple"], 2)
    max_installment = flt(pay["net"] * p["max_installment_pct"] / 100, 2)
    max_by_installment = flt(max_installment * p["max_months"], 2)
    eligible_amount = max(0.0, min(max_by_salary, max_by_installment) - (outstanding if p["max_active"] else 0))
    installment = flt(amount / months, 2) if amount and months else 0

    checks = []

    def check(key, label, ok, detail, blocking=True):
        checks.append({"key": key, "label": label, "ok": bool(ok), "detail": detail, "blocking": blocking})

    check("active", "Active employee", emp.status == "Active" and not emp.relieving_date, f"Status: {emp.status}")
    check("type", "Employment type", not p["employment_types"] or emp.employment_type in p["employment_types"],
          f"{emp.employment_type or 'Not set'} · eligible: {', '.join(p['employment_types']) or 'any'}")
    check("service", "Length of service", service >= p["min_service_months"],
          f"{service} months since {emp.date_of_joining} · minimum {p['min_service_months']}")
    check("salary", "Salary on record", pay["gross"] > 0, f"Gross {money(pay['gross'])}, net {money(pay['net'])} ({pay['basis']})")
    check("open_loans", "Outstanding loans", len(active) <= p["max_active"],
          f"{len(active)} outstanding ({money(outstanding)}) · allowed {p['max_active']}" if active else "None outstanding")
    defaults = [l for l in loans if l.defaulted]
    check("history", "Repayment history", not defaults,
          f"{len(defaults)} old advance(s) unrecovered: {', '.join(l.name for l in defaults)}" if defaults
          else f"{len(loans)} previous advance(s), none in default")
    if amount:
        check("amount", "Amount within limit", amount <= eligible_amount + 0.5,
              f"{money(amount)} requested · eligible up to {money(eligible_amount)}")
    if months:
        check("tenure", "Tenure within limit", 0 < months <= p["max_months"], f"{months} months · maximum {p['max_months']}")
    if installment:
        check("installment", "Instalment affordable", installment <= max_installment + 0.5,
              f"{money(installment)} / month · maximum {money(max_installment)} ({p['max_installment_pct']:g}% of net)")

    need_guarantor = p["guarantor_required"] == "Always" or (p["guarantor_required"].startswith("Above") and amount > pay["gross"])
    g_info = None
    if guarantor:
        g = frappe.db.get_value("Employee", guarantor, ["name", "employee_name", "status", "employment_type", "date_of_joining"], as_dict=True)
        if g:
            g_service = _service_months(g.date_of_joining, today)
            g_loans = [l for l in _loans(guarantor) if l.outstanding > 0]
            backing = _guarantees(guarantor, exclude)
            g_info = {"name": g.name, "employee_name": g.employee_name, "service_months": g_service,
                      "employment_type": g.employment_type, "outstanding": sum(l.outstanding for l in g_loans), "backing": len(backing)}
            check("g_self", "Guarantor is another employee", g.name != employee, "Guarantor must not be the borrower")
            check("g_active", "Guarantor active", g.status == "Active", f"{g.employee_name}: {g.status}")
            check("g_type", "Guarantor employment type", not p["employment_types"] or g.employment_type in p["employment_types"],
                  g.employment_type or "Not set")
            check("g_service", "Guarantor service", g_service >= p["guarantor_min_service_months"],
                  f"{g_service} months · minimum {p['guarantor_min_service_months']}")
            check("g_loans", "Guarantor has no outstanding loan", not g_loans,
                  f"{money(g_info['outstanding'])} outstanding" if g_loans else "None outstanding")
            check("g_backing", "Guarantor's other guarantees", len(backing) < p["guarantor_max_guarantees"],
                  f"Already backs {len(backing)} loan(s) · maximum {p['guarantor_max_guarantees']}")
        else:
            check("guarantor", "Guarantor", False, f"Employee {guarantor} not found")
    elif need_guarantor:
        check("guarantor", "Guarantor", False, f"A guarantor is required ({p['guarantor_required'].lower()})")

    blocking_failed = [c for c in checks if c["blocking"] and not c["ok"]]
    return {
        "employee": {**emp, "service_months": service, "currency": currency},
        "pay": pay, "policy": p,
        "limits": {"max_by_salary": max_by_salary, "max_installment": max_installment, "max_by_installment": max_by_installment,
                   "max_months": p["max_months"], "eligible_amount": flt(eligible_amount, 2),
                   "suggested_months": min(p["max_months"], max(1, -(-int(amount or eligible_amount) // int(max_installment or 1)))) if max_installment else p["max_months"]},
        "requested": {"amount": amount, "months": months, "installment": installment},
        "checks": checks, "eligible": not blocking_failed, "failed": [c["label"] for c in blocking_failed],
        "history": [{k: l.get(k) for k in ("name", "posting_date", "advance_amount", "paid_amount", "return_amount", "outstanding",
                                            "status", "purpose", "mm_installment_months", "mm_monthly_installment", "mm_guarantor_name",
                                            "defaulted", "scheduled")} for l in loans],
        "outstanding": outstanding, "guarantor": g_info, "guarantor_required": need_guarantor,
    }


# ------------------------------------------------------------------ doc events (Employee Advance)
def validate(doc, method=None):
    if not doc.get("mm_is_loan"):
        return
    months = cint(doc.mm_installment_months)
    if months <= 0:
        frappe.throw(_("Enter the number of monthly instalments for this salary loan."))
    doc.mm_monthly_installment = flt(flt(doc.advance_amount) / months, 2)
    if not doc.mm_first_deduction:
        doc.mm_first_deduction = get_first_day(add_months(doc.posting_date or nowdate(), 1))
    doc.repay_unclaimed_amount_from_salary = 1
    r = check_eligibility(doc.employee, doc.advance_amount, months, doc.mm_guarantor, exclude=doc.name if not doc.is_new() else None)
    lines = [f"{'✓' if c['ok'] else '✗'} {c['label']}: {c['detail']}" for c in r["checks"]]
    doc.mm_eligibility = ("ELIGIBLE" if r["eligible"] else "NOT ELIGIBLE — " + ", ".join(r["failed"])) + \
        f" · eligible up to {fmt_money(r['limits']['eligible_amount'], currency=doc.currency)}\n" + "\n".join(lines)
    doc.flags.mm_loan_eligible = r["eligible"]
    doc.flags.mm_loan_failed = r["failed"]


def before_submit(doc, method=None):
    if not doc.get("mm_is_loan") or doc.flags.get("mm_loan_eligible", True):
        return
    if "HR Manager" in frappe.get_roles() and (doc.mm_override_reason or "").strip():
        doc.add_comment("Comment", f"Salary loan approved outside policy by {frappe.session.user}: {doc.mm_override_reason}")
        return
    frappe.throw(_("This loan doesn't meet the loan policy: {0}. An HR Manager can approve it by entering an Override Reason.")
                 .format(", ".join(doc.flags.get("mm_loan_failed") or [])), title=_("Not eligible"))


# ------------------------------------------------------------------ instalments
def _component(company, advance_account):
    name = policy().get("component") or DEFAULT_COMPONENT
    if not frappe.db.exists("Salary Component", name):
        frappe.get_doc({"doctype": "Salary Component", "salary_component": name, "salary_component_abbr": "LR", "type": "Deduction",
                        "description": "Salary loan instalment (mm_core.loans)", "depends_on_payment_days": 0,
                        "accounts": [{"company": company, "account": advance_account}]}).insert(ignore_permissions=True)
    elif advance_account and not frappe.db.exists("Salary Component Account", {"parent": name, "company": company}):
        comp = frappe.get_doc("Salary Component", name)
        comp.append("accounts", {"company": company, "account": advance_account})
        comp.save(ignore_permissions=True)
    return name


@frappe.whitelist()
def create_installments(advance: str) -> str | None:
    """Recurring Additional Salary that recovers a paid salary loan: monthly instalment from the first deduction month."""
    doc = frappe.get_doc("Employee Advance", advance)
    doc.check_permission("submit")
    if not doc.get("mm_is_loan") or doc.docstatus != 1 or flt(doc.paid_amount) <= 0:
        frappe.throw(_("Instalments are set up for a submitted, paid salary loan."))
    existing = frappe.db.get_value("Additional Salary", {"ref_doctype": "Employee Advance", "ref_docname": doc.name, "docstatus": 1}, "name")
    if existing:
        return existing
    months = cint(doc.mm_installment_months) or 1
    balance = _outstanding(doc)
    start = get_first_day(doc.mm_first_deduction or add_months(nowdate(), 1))
    end = add_days(add_months(start, months), -1)
    add = frappe.get_doc({
        "doctype": "Additional Salary", "employee": doc.employee, "company": doc.company, "currency": doc.currency,
        "salary_component": _component(doc.company, doc.advance_account), "amount": flt(balance / months, 2),
        "is_recurring": 1, "from_date": start, "to_date": end, "overwrite_salary_structure_amount": 0,
        "ref_doctype": "Employee Advance", "ref_docname": doc.name,
    })
    add.insert(ignore_permissions=True)
    add.submit()
    doc.add_comment("Comment", f"Instalments set up: {months} × {fmt_money(add.amount, currency=doc.currency)} from {start} ({add.name})")
    return add.name


def on_payment_submit(doc, method=None):
    """Payment Entry paying out a salary loan: set up its instalments right away."""
    for ref in doc.get("references") or []:
        if ref.reference_doctype == "Employee Advance" and frappe.db.get_value("Employee Advance", ref.reference_name, "mm_is_loan"):
            try:
                create_installments(ref.reference_name)
            except Exception:
                frappe.log_error(title=f"Salary loan instalments: {ref.reference_name}")


@frappe.whitelist()
def get_loans(company: str | None = None) -> list:
    """Salary loans for the Loans page: balance, instalment, guarantor, recovery progress."""
    filters = {"mm_is_loan": 1, "docstatus": ["<", 2]}
    if company:
        filters["company"] = company
    rows = frappe.get_all("Employee Advance", filters, ["name", "employee", "employee_name", "department", "posting_date", "advance_amount",
                                                        "paid_amount", "claimed_amount", "return_amount", "status", "docstatus",
                                                        "mm_installment_months", "mm_monthly_installment", "mm_first_deduction",
                                                        "mm_guarantor", "mm_guarantor_name", "currency"], order_by="posting_date desc", limit=500)
    for r in rows:
        r.outstanding = _outstanding(r)
        r.schedule = frappe.db.get_value("Additional Salary", {"ref_doctype": "Employee Advance", "ref_docname": r.name, "docstatus": 1},
                                         ["name", "amount", "from_date", "to_date"], as_dict=True)
    return rows


def fix_advance_account(company: str) -> str:
    """HRMS needs the company's Employee Advances account to be of type Receivable. Sets it, only while the account
    has no ledger entries (never touches posted accounts).

        bench --site <site> execute mm_core.loans.fix_advance_account --kwargs "{'company': '...'}"
    """
    acc = frappe.db.get_value("Company", company, "default_employee_advance_account")
    if not acc:
        return f"{company}: no default Employee Advance account set"
    if frappe.db.get_value("Account", acc, "account_type") == "Receivable":
        return f"{acc}: already Receivable"
    if frappe.db.exists("GL Entry", {"account": acc}):
        return f"{acc}: has ledger entries — change its type by hand after review"
    frappe.db.set_value("Account", acc, "account_type", "Receivable")
    frappe.db.commit()
    return f"{acc}: set to Receivable"


# ------------------------------------------------------------------ loan ledger + dashboard (React Salary Loans page)
def _entry_type(voucher_type, debit, credit):
    if flt(debit) > 0:
        return "Disbursed"
    return {"Journal Entry": "Salary deduction", "Payment Entry": "Repaid", "Expense Claim": "Adjusted (expense claim)"}.get(
        voucher_type, "Recovered")


@frappe.whitelist()
def get_loan_ledger(company: str, scope: str = "loans", from_date: str | None = None, to_date: str | None = None,
                    employee: str | None = None, loan: str | None = None) -> dict:
    """Loan ledger from the general ledger (entries posted against each loan / advance): disbursements, salary deductions,
    cash repayments, with opening and running balance — plus this month vs last month figures for the summary cards.

    scope: "loans" = salary loans only (Salary loan ticked); "all" = every employee advance."""
    if not frappe.has_permission("Employee Advance", "read"):
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    today = getdate(nowdate())
    this_start = get_first_day(today)
    last_start = get_first_day(add_months(today, -1))
    from_date = getdate(from_date or add_months(this_start, -5))
    to_date = getdate(to_date or today)

    f = {"company": company, "docstatus": 1}
    if scope != "all" and frappe.get_meta("Employee Advance").has_field("mm_is_loan"):
        f["mm_is_loan"] = 1
    fields = ["name", "employee", "employee_name", "department", "posting_date", "advance_amount", "paid_amount", "claimed_amount",
              "return_amount", "status", "purpose", "currency"]
    if frappe.get_meta("Employee Advance").has_field("mm_is_loan"):
        fields += ["mm_is_loan", "mm_installment_months", "mm_monthly_installment", "mm_guarantor_name", "mm_first_deduction"]
    loans = frappe.get_all("Employee Advance", f, fields, order_by="posting_date desc", limit=5000)
    for l in loans:
        l.outstanding = _outstanding(l)
    names = [l.name for l in loans]
    by_name = {l.name: l for l in loans}
    pending = frappe.db.count("Employee Advance", {"company": company, "docstatus": 0, **({"mm_is_loan": 1} if "mm_is_loan" in f else {})})

    gl = frappe.db.sql("""select posting_date, voucher_type, voucher_no, against_voucher as loan, sum(debit) as debit,
            sum(credit) as credit, max(remarks) as remarks
        from `tabGL Entry`
        where is_cancelled = 0 and company = %(company)s and against_voucher_type = 'Employee Advance'
          and against_voucher in %(names)s and (debit > 0 or credit > 0)
        group by posting_date, voucher_type, voucher_no, against_voucher
        order by posting_date, voucher_no""", {"company": company, "names": names or [""]}, as_dict=True)
    # Newer ERPNext links payments / payroll deductions to the advance in the Advance Payment Ledger instead of the
    # GL entry's against_voucher (+ = paid out, − = recovered). Read both; a voucher already in the GL list wins.
    if frappe.db.exists("DocType", "Advance Payment Ledger Entry"):
        seen = {(g.voucher_no, g.loan) for g in gl}
        dates = {}
        for a in frappe.db.sql("""select voucher_type, voucher_no, against_voucher_no as loan, sum(amount) as amount
                from `tabAdvance Payment Ledger Entry`
                where company = %(company)s and against_voucher_type = 'Employee Advance' and against_voucher_no in %(names)s
                  and delinked = 0
                group by voucher_type, voucher_no, against_voucher_no having sum(amount) <> 0""",
                               {"company": company, "names": names or [""]}, as_dict=True):
            if (a.voucher_no, a.loan) in seen:
                continue
            key = (a.voucher_type, a.voucher_no)
            if key not in dates:
                dates[key] = frappe.db.get_value(a.voucher_type, a.voucher_no, "posting_date") if frappe.db.exists("DocType", a.voucher_type) else None
            if not dates[key]:
                continue
            gl.append(frappe._dict(posting_date=dates[key], voucher_type=a.voucher_type, voucher_no=a.voucher_no, loan=a.loan,
                                   debit=max(0.0, flt(a.amount)), credit=max(0.0, -flt(a.amount))))
        gl.sort(key=lambda g: (getdate(g.posting_date), g.voucher_no))

    def month_sum(start, kind):
        end = get_last_day(start)
        return flt(sum(g.debit if kind == "Disbursed" else g.credit for g in gl
                       if start <= getdate(g.posting_date) <= end and _entry_type(g.voucher_type, g.debit, g.credit) == kind), 2)

    def pct(a, b):
        return None if not b else round((a - b) / b * 100, 1)

    ded_this, ded_last = month_sum(this_start, "Salary deduction"), month_sum(last_start, "Salary deduction")
    dis_this, dis_last = month_sum(this_start, "Disbursed"), month_sum(last_start, "Disbursed")
    scheduled = flt(frappe.db.sql("""select sum(amount) from `tabAdditional Salary`
        where docstatus = 1 and company = %(company)s and ref_doctype = 'Employee Advance' and ref_docname in %(names)s
          and ((is_recurring = 1 and from_date <= %(end)s and to_date >= %(start)s) or (is_recurring = 0 and payroll_date between %(start)s and %(end)s))""",
        {"company": company, "names": names or [""], "start": this_start, "end": get_last_day(this_start)})[0][0])
    active = [l for l in loans if l.outstanding > 0]
    # Before this month's payroll has run, the month's deduction is what's scheduled (shown as such, not as a 100% drop).
    payroll_run = bool(frappe.db.exists("Payroll Entry", {"company": company, "docstatus": 1, "start_date": [">=", this_start],
                                                          "end_date": ["<=", get_last_day(this_start)]}))
    projected = not payroll_run and ded_this == 0
    ded_shown = scheduled if projected else ded_this

    # ledger rows for the selection
    sel = [g for g in gl if (not loan or g.loan == loan) and (not employee or by_name[g.loan].employee == employee)]
    opening = flt(sum(g.debit - g.credit for g in sel if getdate(g.posting_date) < from_date), 2)
    rows, bal = [], opening
    for g in sel:
        if not (from_date <= getdate(g.posting_date) <= to_date):
            continue
        bal = flt(bal + g.debit - g.credit, 2)
        l = by_name[g.loan]
        rows.append({"date": str(g.posting_date), "employee": l.employee, "employee_name": l.employee_name, "loan": g.loan,
                     "type": _entry_type(g.voucher_type, g.debit, g.credit), "voucher_type": g.voucher_type, "voucher_no": g.voucher_no,
                     "debit": flt(g.debit, 2), "credit": flt(g.credit, 2), "balance": bal})
    return {
        "currency": frappe.get_cached_value("Company", company, "default_currency") or "PKR",
        "summary": {
            "outstanding": flt(sum(l.outstanding for l in active), 2), "active": len(active), "loans": len(loans), "pending": pending,
            "deducted_this": ded_shown, "deducted_last": ded_last, "deducted_pct": pct(ded_shown, ded_last),
            "deducted_projected": projected,
            "disbursed_this": dis_this, "disbursed_last": dis_last, "disbursed_pct": pct(dis_this, dis_last),
            "scheduled_this": scheduled, "this_month": str(this_start), "last_month": str(last_start),
        },
        "ledger": {"from_date": str(from_date), "to_date": str(to_date), "opening": opening, "closing": bal, "rows": rows[-2000:],
                   "debit": flt(sum(r["debit"] for r in rows), 2), "credit": flt(sum(r["credit"] for r in rows), 2)},
        "loans": loans,
    }
