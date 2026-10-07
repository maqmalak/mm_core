"""Cheque tracking and bank clearance for the React Banking pages.

- Mode of payment Cheque on a Payment Entry (Pay) or a Journal Entry crediting a bank account: the next in-hand leaf
  of that bank account's cheque book is filled in as the cheque number (unless one from the book is typed), and on
  submit the leaf becomes Issued with the voucher (type + no), party and amount; cancelling the voucher cancels it.
- get_clearance_entries / update_clearance wrap ERPNext's Bank Clearance (an unsaved single) for the React page, and
  copy clearance dates onto the matching cheque leaves.
"""

import json

import frappe
from frappe import _
from frappe.utils import flt, getdate, nowdate


def _is_cheque(doc):
    """Mode of payment is a cheque (Mode of Payment named Cheque / Check)."""
    mop = (doc.get("mode_of_payment") or "").lower()
    return "cheque" in mop or "check" in mop


def _bank_account_rows(doc):
    """(GL account, amount paid out) of the bank side of a voucher."""
    if doc.doctype == "Payment Entry":
        return [(doc.paid_from, doc.paid_amount)] if doc.payment_type == "Pay" else []
    return [(r.account, r.credit_in_account_currency) for r in doc.get("accounts") or []
            if r.credit_in_account_currency and frappe.get_cached_value("Account", r.account, "account_type") == "Bank"]


def _books(doc, account):
    filters = {"company": doc.company, "status": ["in", ["Unused", "In Use"]]}
    if doc.get("bank_account"):
        filters["bank_account"] = doc.bank_account
    else:
        filters["account"] = account
    return frappe.get_all("Cheque Book", filters, pluck="name", order_by="posting_date asc, name asc")


def _ref_field(doc):
    return "reference_no" if doc.doctype == "Payment Entry" else "cheque_no"


def _find_leaf(doc, cheque_no):
    """Leaf with this number in a cheque book of the voucher's bank account (any status)."""
    if not cheque_no:
        return None
    for account, _amt in _bank_account_rows(doc):
        for leaf in frappe.get_all("Cheque Book Leaf", {"cheque_no": cheque_no.strip(), "parenttype": "Cheque Book"},
                                   ["name", "parent", "status", "voucher_type", "voucher_no"]):
            book = frappe.db.get_value("Cheque Book", leaf.parent, ["bank_account", "account", "company"], as_dict=True)
            if book and book.company == doc.company and (
                (doc.get("bank_account") and book.bank_account == doc.bank_account) or book.account == account
            ):
                return leaf
    return None


def _next_free_leaf(doc):
    """Next in-hand leaf of the account's oldest open book, skipping numbers other draft vouchers already hold."""
    for account, _amt in _bank_account_rows(doc):
        held = set(frappe.get_all("Payment Entry", {"docstatus": 0, "paid_from": account, "name": ["!=", doc.name or ""],
                                                    "reference_no": ["is", "set"]}, pluck="reference_no"))
        held |= set(frappe.get_all("Journal Entry", {"docstatus": 0, "name": ["!=", doc.name or ""], "cheque_no": ["is", "set"]},
                                   pluck="cheque_no"))
        for book in _books(doc, account):
            for leaf in frappe.get_all("Cheque Book Leaf", {"parent": book, "status": "Unused"}, ["name", "cheque_no", "parent"],
                                       order_by="idx asc"):
                if leaf.cheque_no not in held:
                    return leaf
    return None


def assign_cheque_no(doc, method=None):
    """before_validate (Payment Entry / Journal Entry): mode Cheque and no cheque number yet (or one that isn't in the
    books) → take the next in-hand leaf of the bank account's cheque book. Payment Entry needs the number on every save
    (ERPNext checks it in validate); a Journal Entry only at submit."""
    if not frappe.db.exists("DocType", "Cheque Book") or not _is_cheque(doc) or not _bank_account_rows(doc):
        return
    if doc.doctype == "Journal Entry" and doc._action != "submit":
        return
    field = _ref_field(doc)
    current = (doc.get(field) or "").strip()
    leaf = _find_leaf(doc, current) if current else None
    if leaf and (leaf.status == "Unused" or leaf.voucher_no == doc.name):
        return
    nxt = _next_free_leaf(doc)
    if not nxt:
        if not current:
            frappe.msgprint(_("No unused cheque leaf for this bank account — add a cheque book or enter the cheque number."),
                            indicator="orange", alert=True)
        return
    doc.set(field, nxt.cheque_no)
    date_field = "reference_date" if doc.doctype == "Payment Entry" else "cheque_date"
    if not doc.get(date_field):
        doc.set(date_field, doc.posting_date)
    frappe.msgprint(_("Cheque {0} from cheque book {1}").format(nxt.cheque_no, nxt.parent), indicator="blue", alert=True)


def _resave(book_name):
    book = frappe.get_doc("Cheque Book", book_name)
    book.flags.ignore_permissions = True
    book.save()


def _party(doc):
    if doc.doctype == "Payment Entry":
        return doc.party_type, doc.party, doc.party_name
    for r in doc.get("accounts") or []:
        if r.get("party"):
            return r.party_type, r.party, frappe.db.get_value(r.party_type, r.party, {"Supplier": "supplier_name", "Customer": "customer_name",
                                                                                        "Employee": "employee_name"}.get(r.party_type, "name"))
    return None, None, (doc.get("pay_to_recd_from") or doc.get("title"))


def on_voucher_submit(doc, method=None):
    """Mark the cheque leaf Issued with this voucher (Payment Entry Pay / Journal Entry crediting a bank account)."""
    if not frappe.db.exists("DocType", "Cheque Book") or not _bank_account_rows(doc):
        return
    leaf = _find_leaf(doc, doc.get(_ref_field(doc)))
    if not leaf:
        return
    if leaf.status != "Unused" and leaf.voucher_no != doc.name:
        frappe.msgprint(_("Cheque {0} is already {1} in cheque book {2}.").format(doc.get(_ref_field(doc)), leaf.status.lower(), leaf.parent),
                        indicator="orange", alert=True)
        return
    party_type, party, party_name = _party(doc)
    amount = sum(a for _acc, a in _bank_account_rows(doc))
    frappe.db.set_value("Cheque Book Leaf", leaf.name, {
        "status": "Issued", "issue_date": doc.get("reference_date") or doc.get("cheque_date") or doc.posting_date,
        "party_type": party_type if party_type in ("Supplier", "Customer", "Employee", "Shareholder") else None, "party": party,
        "party_name": party_name, "amount": amount, "voucher_type": doc.doctype, "voucher_no": doc.name, "cancel_reason": None,
    })
    _resave(leaf.parent)


def on_voucher_cancel(doc, method=None):
    if not frappe.db.exists("DocType", "Cheque Book"):
        return
    for leaf in frappe.get_all("Cheque Book Leaf", {"voucher_type": doc.doctype, "voucher_no": doc.name}, ["name", "parent"]):
        frappe.db.set_value("Cheque Book Leaf", leaf.name, {"status": "Void", "cancel_reason": f"{doc.doctype} {doc.name} cancelled"})
        _resave(leaf.parent)


# kept for hooks written before the voucher fields
on_payment_submit = on_voucher_submit
on_payment_cancel = on_voucher_cancel


# ------------------------------------------------------------------ bank clearance (React page)
def _clearance_doc(account=None, bank_account=None, from_date=None, to_date=None, include_reconciled=0):
    frappe.has_permission("Bank Clearance", "write", throw=True)
    if bank_account and not account:
        account = frappe.db.get_value("Bank Account", bank_account, "account")
    return frappe.get_doc({"doctype": "Bank Clearance", "account": account, "bank_account": bank_account,
                           "from_date": from_date, "to_date": to_date, "include_reconciled_entries": int(include_reconciled or 0)})


@frappe.whitelist()
def get_clearance_entries(bank_account: str, from_date: str, to_date: str, include_reconciled: int = 0) -> list:
    doc = _clearance_doc(bank_account=bank_account, from_date=from_date, to_date=to_date, include_reconciled=include_reconciled)
    if not doc.account:
        frappe.throw(_("Bank Account {0} has no GL account set.").format(bank_account))
    doc.get_payment_entries()
    return [{k: r.get(k) for k in ("payment_document", "payment_entry", "against_account", "amount", "posting_date",
                                   "cheque_number", "cheque_date", "clearance_date")} for r in doc.payment_entries]


@frappe.whitelist(methods=["POST"])
def update_clearance(bank_account: str, from_date: str, to_date: str, rows: str) -> dict:
    """rows: [{payment_document, payment_entry, clearance_date}] — saves clearance dates (ERPNext's own checks) and copies
    them onto cheque leaves issued by those payments."""
    rows = json.loads(rows) if isinstance(rows, str) else rows
    doc = _clearance_doc(bank_account=bank_account, from_date=from_date, to_date=to_date, include_reconciled=1)
    doc.get_payment_entries()
    wanted = {(r["payment_document"], r["payment_entry"]): r.get("clearance_date") or None for r in rows}
    changed = 0
    for r in doc.payment_entries:
        key = (r.payment_document, r.payment_entry)
        if key in wanted and str(r.clearance_date or "") != str(wanted[key] or ""):
            r.clearance_date = wanted[key]
            changed += 1
    if changed:
        doc.update_clearance_date()
        for (dt, name), date in wanted.items():
            if frappe.db.exists("DocType", "Cheque Book Leaf"):
                for leaf in frappe.get_all("Cheque Book Leaf", {"voucher_type": dt, "voucher_no": name, "status": ["in", ["Issued", "Cleared"]]},
                                           ["name", "parent"]):
                    # paid by the bank → Cleared; clearance date removed → back to Issued
                    frappe.db.set_value("Cheque Book Leaf", leaf.name, {"clearance_date": date, "status": "Cleared" if date else "Issued"})
                    _resave(leaf.parent)
    return {"updated": changed}


# ------------------------------------------------------------------ cheque tracking (React list of every leaf)
@frappe.whitelist()
def search_cheques(company: str | None = None, q: str | None = None, status: str | None = None, bank_account: str | None = None,
                   from_date: str | None = None, to_date: str | None = None, start: int = 0, limit: int = 50) -> dict:
    """Every cheque leaf across all cheque books, searchable by cheque no, payee, voucher no or amount."""
    if not frappe.has_permission("Cheque Book", "read"):
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    cond, args = ["1 = 1"], {}
    if company:
        cond.append("b.company = %(company)s")
        args["company"] = company
    if bank_account:
        cond.append("b.bank_account = %(bank_account)s")
        args["bank_account"] = bank_account
    if from_date:
        cond.append("coalesce(l.issue_date, b.posting_date) >= %(from_date)s")
        args["from_date"] = from_date
    if to_date:
        cond.append("coalesce(l.issue_date, b.posting_date) <= %(to_date)s")
        args["to_date"] = to_date
    base = " and ".join(cond)
    where = base
    q = (q or "").strip()
    if q:
        args["q"] = f"%{q}%"
        amount_match = ""
        try:
            args["amt"] = flt(q.replace(",", ""))
            amount_match = " or l.amount = %(amt)s" if args["amt"] else ""
        except Exception:
            pass
        where += f""" and (l.cheque_no like %(q)s or l.party like %(q)s or l.party_name like %(q)s or l.voucher_no like %(q)s
            or b.name like %(q)s{amount_match})"""
    counts = dict(frappe.db.sql(f"""select l.status, count(*) from `tabCheque Book Leaf` l join `tabCheque Book` b on b.name = l.parent
        where {where} group by l.status""", args))
    if status:
        where += " and l.status = %(status)s"
        args["status"] = status
    args.update({"start": int(start or 0), "limit": min(int(limit or 50), 500)})
    rows = frappe.db.sql(f"""select l.cheque_no, l.sno, l.status, l.issue_date, l.party_type, l.party, l.party_name, l.amount,
            l.voucher_type, l.voucher_no, l.clearance_date, l.cancel_reason, b.name as book, b.bank_account, b.bank, b.posting_date
        from `tabCheque Book Leaf` l join `tabCheque Book` b on b.name = l.parent
        where {where}
        order by (l.status = 'Unused'), coalesce(l.issue_date, b.posting_date) desc, l.cheque_no desc
        limit %(start)s, %(limit)s""", args, as_dict=True)
    total = frappe.db.sql(f"""select count(*) from `tabCheque Book Leaf` l join `tabCheque Book` b on b.name = l.parent where {where}""", args)[0][0]
    amount = frappe.db.sql(f"""select sum(l.amount) from `tabCheque Book Leaf` l join `tabCheque Book` b on b.name = l.parent
        where {where} and l.status in ('Issued', 'Cleared')""", args)[0][0]
    return {"rows": rows, "total": total, "counts": counts, "issued_amount": flt(amount)}


@frappe.whitelist()
def cheque_insights(company: str | None = None, bank_account: str | None = None) -> dict:
    """Cheque register insights: status-wise mix, bank-wise usage, top payees and the monthly issue trend."""
    if not frappe.has_permission("Cheque Book", "read"):
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    cond, args = ["1 = 1"], {}
    if company:
        cond.append("b.company = %(company)s")
        args["company"] = company
    if bank_account:
        cond.append("b.bank_account = %(bank_account)s")
        args["bank_account"] = bank_account
    where = " and ".join(cond)
    join = "from `tabCheque Book Leaf` l join `tabCheque Book` b on b.name = l.parent"
    by_status = frappe.db.sql(f"select l.status, count(*) as count, sum(l.amount) as amount {join} where {where} group by l.status",
                              args, as_dict=True)
    by_bank = frappe.db.sql(f"""select b.bank_account, max(b.bank) as bank, count(distinct b.name) as books, count(*) as total,
            sum(l.status = 'Unused') as unused, sum(l.status = 'Issued') as issued, sum(l.status = 'Cleared') as cleared,
            sum(l.status = 'Void') as void, sum(l.status = 'Stopped') as stopped, sum(l.status = 'Dishonoured') as dishonoured,
            sum(case when l.status in ('Issued', 'Cleared') then l.amount else 0 end) as issued_amount,
            sum(case when l.status = 'Issued' then l.amount else 0 end) as outstanding_amount
        {join} where {where} group by b.bank_account order by issued_amount desc""", args, as_dict=True)
    top = frappe.db.sql(f"""select l.party_type, l.party, max(l.party_name) as party_name, count(*) as count, sum(l.amount) as amount
        {join} where {where} and l.status in ('Issued', 'Cleared') and ifnull(l.party, '') != ''
        group by l.party_type, l.party order by amount desc limit 6""", args, as_dict=True)
    monthly = frappe.db.sql(f"""select date_format(l.issue_date, '%%Y-%%m') as month, count(*) as count, sum(l.amount) as amount
        {join} where {where} and l.status in ('Issued', 'Cleared') and l.issue_date >= date_sub(curdate(), interval 6 month)
        group by month order by month""", args, as_dict=True)
    for r in by_bank:
        for k in ("total", "unused", "issued", "cleared", "void", "stopped", "dishonoured", "books"):
            r[k] = int(r[k] or 0)
        for k in ("issued_amount", "outstanding_amount"):
            r[k] = flt(r[k])
    total = sum(int(s.count) for s in by_status)
    unused = sum(int(s.count) for s in by_status if s.status == "Unused")
    amt = {s.status: flt(s.amount) for s in by_status}
    return {
        "summary": {"total": total, "used": total - unused, "unused": unused,
                    "outstanding": amt.get("Issued", 0.0), "cleared": amt.get("Cleared", 0.0),
                    "dishonoured": amt.get("Dishonoured", 0.0), "books": sum(r["books"] for r in by_bank)},
        "by_status": [{"status": s.status, "count": int(s.count), "amount": flt(s.amount)} for s in by_status],
        "by_bank": by_bank,
        "top_payees": [{"party_type": t.party_type, "party": t.party, "party_name": t.party_name or t.party, "count": int(t.count),
                        "amount": flt(t.amount)} for t in top],
        "monthly": [{"month": m.month, "count": int(m.count), "amount": flt(m.amount)} for m in monthly],
    }


def sync_cleared_status():
    """Leaf status follows its voucher's clearance date, wherever it was set (React Bank Clearance, the desk's Bank
    Clearance form, the Bank Reconciliation Tool): Issued + cleared voucher → Cleared; Cleared + date removed → Issued.
    Runs every 10 minutes (hooks) and after reconciliation."""
    if not frappe.db.exists("DocType", "Cheque Book Leaf"):
        return 0
    changed = set()
    for vt in ("Payment Entry", "Journal Entry"):
        rows = frappe.db.sql(f"""select l.name, l.parent, l.status, v.clearance_date from `tabCheque Book Leaf` l
            join `tab{vt}` v on v.name = l.voucher_no
            where l.voucher_type = %s and l.status in ('Issued', 'Cleared')
              and ((l.status = 'Issued' and v.clearance_date is not null) or (l.status = 'Cleared' and v.clearance_date is null)
                   or (l.status = 'Cleared' and ifnull(l.clearance_date, '1900-01-01') != v.clearance_date))""", vt, as_dict=True)
        for r in rows:
            frappe.db.set_value("Cheque Book Leaf", r.name, {"status": "Cleared" if r.clearance_date else "Issued",
                                                            "clearance_date": r.clearance_date})
            changed.add(r.parent)
    for book in changed:
        _resave(book)
    frappe.db.commit()
    return len(changed)
