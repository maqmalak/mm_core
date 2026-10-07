"""Bank insights for the React Banks page: balances per books and per bank, uncleared items, money in / out by
month, cheque usage and recent transactions — per company bank account and in total."""

import frappe
from frappe import _
from frappe.utils import add_months, flt, get_first_day, get_last_day, getdate, nowdate


@frappe.whitelist()
def bank_insights(company: str) -> dict:
    if not frappe.has_permission("Bank Account", "read"):
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    today = getdate(nowdate())
    this_start, last_start = get_first_day(today), get_first_day(add_months(today, -1))
    six_start = get_first_day(add_months(today, -5))
    accounts = frappe.get_all("Bank Account", {"company": company, "is_company_account": 1, "disabled": 0, "account": ["is", "set"]},
                              ["name", "account_name", "bank", "account", "bank_account_no", "is_default"], order_by="is_default desc, account_name")
    has_cheques = frappe.db.exists("DocType", "Cheque Book")
    out = []
    for ba in accounts:
        acc = ba.account
        book = flt(frappe.db.sql("""select sum(debit - credit) from `tabGL Entry` where account = %s and company = %s and is_cancelled = 0
            and posting_date <= %s""", (acc, company, today))[0][0])
        # uncleared: cheques / transfers out not yet paid by the bank, deposits not yet credited
        unc_out = flt(frappe.db.sql("""select sum(paid_amount) from `tabPayment Entry` where docstatus = 1 and company = %s
            and paid_from = %s and clearance_date is null and posting_date <= %s""", (company, acc, today))[0][0])
        unc_in = flt(frappe.db.sql("""select sum(received_amount) from `tabPayment Entry` where docstatus = 1 and company = %s
            and paid_to = %s and clearance_date is null and posting_date <= %s""", (company, acc, today))[0][0])
        je = frappe.db.sql("""select sum(a.credit_in_account_currency), sum(a.debit_in_account_currency) from `tabJournal Entry Account` a
            join `tabJournal Entry` j on j.name = a.parent where j.docstatus = 1 and j.company = %s and a.account = %s
            and j.clearance_date is null and j.posting_date <= %s and j.voucher_type in ('Bank Entry', 'Journal Entry', 'Contra Entry')""",
                           (company, acc, today))[0]
        unc_out += flt(je[0])
        unc_in += flt(je[1])
        monthly = {r[0]: (flt(r[1]), flt(r[2])) for r in frappe.db.sql("""select date_format(posting_date, '%%Y-%%m'), sum(debit), sum(credit)
            from `tabGL Entry` where account = %s and company = %s and is_cancelled = 0 and posting_date between %s and %s
            group by 1""", (acc, company, six_start, get_last_day(today)))}
        months = []
        for i in range(6):
            m = add_months(six_start, i).strftime("%Y-%m")
            inflow, outflow = monthly.get(m, (0.0, 0.0))
            months.append({"month": m, "in": inflow, "out": outflow})
        this_m, last_m = today.strftime("%Y-%m"), last_start.strftime("%Y-%m")
        recent = frappe.db.sql("""select posting_date, voucher_type, voucher_no, party, against, debit, credit from `tabGL Entry`
            where account = %s and company = %s and is_cancelled = 0 order by posting_date desc, creation desc limit 5""",
                               (acc, company), as_dict=True)
        cheques = {}
        if has_cheques:
            cheques = dict(frappe.db.sql("""select l.status, count(*) from `tabCheque Book Leaf` l join `tabCheque Book` b on b.name = l.parent
                where b.bank_account = %s group by l.status""", ba.name))
        out.append({
            "name": ba.name, "account_name": ba.account_name, "bank": ba.bank, "account": acc, "account_no": ba.bank_account_no,
            "is_default": ba.is_default, "book_balance": book, "uncleared_out": unc_out, "uncleared_in": unc_in,
            "bank_balance": flt(book + unc_out - unc_in, 2),
            "in_this": monthly.get(this_m, (0, 0))[0], "out_this": monthly.get(this_m, (0, 0))[1],
            "in_last": monthly.get(last_m, (0, 0))[0], "out_last": monthly.get(last_m, (0, 0))[1],
            "months": months, "recent": recent, "cheques": cheques,
        })
    tot = lambda k: flt(sum(a[k] for a in out), 2)  # noqa: E731
    return {
        "currency": frappe.get_cached_value("Company", company, "default_currency") or "PKR",
        "totals": {k: tot(k) for k in ("book_balance", "bank_balance", "uncleared_out", "uncleared_in", "in_this", "out_this", "in_last", "out_last")},
        "accounts": out,
    }
