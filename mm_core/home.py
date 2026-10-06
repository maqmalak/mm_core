"""Data for the React home page (Desktop): KPIs, things needing attention, approvals, recent activity and a live
figure per app tile — one call, every block filtered to what the current user may read on this site.

    mm_core.home.get_home(company)            -> {...}
    mm_core.home.approve(doctype, name)       -> applies the "Approve" workflow transition the user may take

Each block is computed independently: a doctype that isn't installed (e.g. no HRMS on a site) or a query that
fails just leaves that block out — the page never breaks because one module is missing.
"""

import frappe
from frappe.utils import add_days, add_months, flt, get_first_day, get_last_day, getdate, now_datetime, nowdate

CACHE_SECONDS = 120

# Transactions shown in the activity feed: doctype -> (category, amount field, party/title field)
ACTIVITY = {
    "Sales Invoice": ("finance", "base_grand_total", "customer_name"),
    "Purchase Invoice": ("finance", "base_grand_total", "supplier_name"),
    "Payment Entry": ("finance", "base_paid_amount", "party_name"),
    "Journal Entry": ("finance", "total_debit", "title"),
    "Sales Order": ("ops", "base_grand_total", "customer_name"),
    "Purchase Order": ("ops", "base_grand_total", "supplier_name"),
    "Delivery Note": ("ops", "base_grand_total", "customer_name"),
    "Purchase Receipt": ("ops", "base_grand_total", "supplier_name"),
    "Stock Entry": ("ops", "total_outgoing_value", "stock_entry_type"),
    "Material Request": ("ops", None, "material_request_type"),
    "Work Order": ("ops", None, "item_name"),
    "Quotation": ("ops", "base_grand_total", "customer_name"),
    "CRM Lead": ("crm", None, "lead_name"),
    "CRM Deal": ("crm", "deal_value", "organization"),
    "Leave Application": ("hr", None, "employee_name"),
    "Expense Claim": ("hr", "total_claimed_amount", "employee_name"),
}


def _can(doctype):
    return frappe.db.exists("DocType", doctype) and frappe.has_permission(doctype, "read")


def _has(doctype, field):
    return frappe.get_meta(doctype).has_field(field)


def _block(fn, *args):
    try:
        return fn(*args)
    except Exception:
        frappe.log_error(title=f"Home block failed: {fn.__name__}")
        return None


def _months(n=12):
    start = get_first_day(add_months(nowdate(), -(n - 1)))
    return [getdate(add_months(start, i)) for i in range(n)]


def _monthly(doctype, field, company, date_field="posting_date"):
    months = _months()
    rows = frappe.db.sql(
        f"""select date_format(`{date_field}`, '%%Y-%%m') m, sum(`{field}`) v from `tab{doctype}`
            where docstatus = 1 and company = %s and `{date_field}` >= %s group by m""",
        (company, months[0]), as_dict=True)
    by = {r.m: flt(r.v) for r in rows}
    return [by.get(m.strftime("%Y-%m"), 0) for m in months]


def _kpis(company):
    out = []
    today, month_start = getdate(nowdate()), get_first_day(nowdate())
    if _can("Sales Invoice"):
        spark = _monthly("Sales Invoice", "base_net_total", company)
        day = today.day
        last_start = get_first_day(add_months(today, -1))
        cur = flt(frappe.db.sql("""select sum(base_net_total) from `tabSales Invoice` where docstatus=1 and company=%s
            and posting_date between %s and %s""", (company, month_start, today))[0][0])
        prev = flt(frappe.db.sql("""select sum(base_net_total) from `tabSales Invoice` where docstatus=1 and company=%s
            and posting_date between %s and %s""", (company, last_start, add_days(last_start, day - 1)))[0][0])
        out.append({"key": "sales", "label": "Sales, month to date", "value": cur, "format": "money",
                    "delta": ((cur - prev) / prev * 100) if prev else None, "spark": spark,
                    "note": f"vs {prev:,.0f} same days last month", "tone": "green"})
        rec = frappe.db.sql("""select sum(outstanding_amount), sum(case when due_date < %s then outstanding_amount else 0 end)
            from `tabSales Invoice` where docstatus=1 and company=%s and outstanding_amount > 0""",
                            (add_days(today, -60), company))[0]
        out.append({"key": "receivables", "label": "Receivables", "value": flt(rec[0]), "format": "money",
                    "note": f"{flt(rec[1]):,.0f} overdue over 60 days", "tone": "amber", "invert": True})
    if _can("Purchase Invoice"):
        pay = frappe.db.sql("""select sum(outstanding_amount), sum(case when due_date <= %s then outstanding_amount else 0 end)
            from `tabPurchase Invoice` where docstatus=1 and company=%s and outstanding_amount > 0""",
                            (add_days(today, 7), company))[0]
        out.append({"key": "payables", "label": "Payables", "value": flt(pay[0]), "format": "money",
                    "note": f"{flt(pay[1]):,.0f} due within 7 days", "tone": "red", "invert": True})
    if _can("GL Entry"):
        accounts = frappe.get_all("Account", filters={"company": company, "account_type": ["in", ["Bank", "Cash"]], "is_group": 0}, pluck="name")
        if accounts:
            months = _months()
            opening = flt(frappe.db.sql("""select sum(debit - credit) from `tabGL Entry` where is_cancelled=0 and account in %s
                and posting_date < %s""", (accounts, months[0]))[0][0])
            rows = frappe.db.sql("""select date_format(posting_date, '%%Y-%%m') m, sum(debit - credit) v from `tabGL Entry`
                where is_cancelled=0 and account in %s and posting_date >= %s group by m""", (accounts, months[0]), as_dict=True)
            by, run, spark = {r.m: flt(r.v) for r in rows}, opening, []
            for m in months:
                run += by.get(m.strftime("%Y-%m"), 0)
                spark.append(run)
            banks = frappe.db.count("Account", {"company": company, "account_type": "Bank", "is_group": 0})
            out.append({"key": "cash", "label": "Cash & bank", "value": run, "format": "money", "spark": spark,
                        "note": f"Across {banks} bank account{'s' if banks != 1 else ''}", "tone": "lime"})
    if _can("Bin"):
        row = frappe.db.sql("""select sum(b.stock_value), count(distinct case when b.actual_qty > 0 then b.item_code end),
            count(distinct case when b.actual_qty > 0 then b.warehouse end)
            from `tabBin` b join `tabWarehouse` w on w.name = b.warehouse where w.company = %s""", (company,))[0]
        if flt(row[0]) or row[1]:
            out.append({"key": "stock", "label": "Stock value", "value": flt(row[0]), "format": "money",
                        "note": f"{row[1] or 0:,} items in {row[2] or 0} warehouses", "tone": "teal"})
    if _can("Stock Entry"):
        made = frappe.db.sql("""select sum(fg_completed_qty), count(*) from `tabStock Entry` where docstatus=1 and company=%s
            and purpose='Manufacture' and posting_date=%s""", (company, today))[0]
        if made[1] or frappe.db.exists("Stock Entry", {"company": company, "purpose": "Manufacture", "docstatus": 1,
                                                        "posting_date": [">=", add_days(today, -90)]}):
            out.append({"key": "production", "label": "Production today", "value": flt(made[0]), "format": "number",
                        "note": f"{made[1] or 0} manufacture entries", "tone": "orange"})
    return out


def _approvals(limit=30, company=None):
    """What waits for the current user (workflow documents + HR requests), from the approvals inbox."""
    from mm_core.approvals import get_inbox

    items = []
    for p in get_inbox(company, days=1)["pending"][:limit]:
        items.append({"doctype": p["doctype"], "name": p["name"], "state": p["state"], "title": p.get("title") or "",
                      "amount": p.get("amount"), "since": p["since"], "category": p["category"],
                      "can_approve": "Approve" in (p.get("actions") or [])})
    return items


def _todos():
    """The current user's open to-dos (and how many are past their date) — shown in the activity panel header."""
    user, today = frappe.session.user, getdate(nowdate())
    open_, overdue = frappe.db.sql("""select count(*), sum(date < %s) from `tabToDo`
        where status = 'Open' and allocated_to = %s""", (today, user))[0]
    return {"open": int(open_ or 0), "overdue": int(overdue or 0)}


def _alerts(company, approvals_count):
    today, user, out = getdate(nowdate()), frappe.session.user, []

    def add(key, n, label, meta, to, tone, amount=None):
        if n:
            out.append({"key": key, "n": n, "label": label, "meta": meta, "to": to, "tone": tone, "amount": amount})

    if _can("Sales Invoice"):
        n, amt = frappe.db.sql("""select count(*), sum(outstanding_amount) from `tabSales Invoice` where docstatus=1
            and company=%s and outstanding_amount > 0 and due_date < %s""", (company, today))[0]
        add("overdue_sales", n, "Overdue sales invoices", "Receivable past due date", "/selling/sales-invoices?overdue=1", "rose", flt(amt))
    if _can("Purchase Invoice"):
        n, amt = frappe.db.sql("""select count(*), sum(outstanding_amount) from `tabPurchase Invoice` where docstatus=1
            and company=%s and outstanding_amount > 0 and due_date < %s""", (company, today))[0]
        add("overdue_purchase", n, "Overdue supplier bills", "Payable past due date", "/purchase/invoices?overdue=1", "red", flt(amt))
    if _can("Sales Order"):
        n = frappe.db.sql("""select count(*) from `tabSales Order` where docstatus=1 and company=%s
            and status in ('To Deliver and Bill','To Deliver') and delivery_date < %s""", (company, today))[0][0]
        add("late_delivery", n, "Sales orders past delivery date", "Not yet delivered", "/selling/sales-orders?late=1", "amber")
    if _can("Purchase Order"):
        n = frappe.db.sql("""select count(*) from `tabPurchase Order` where docstatus=1 and company=%s
            and status in ('To Receive and Bill','To Receive') and schedule_date < %s""", (company, today))[0][0]
        add("late_receipt", n, "Purchase orders past due", "Not yet received", "/import/purchase-orders?late=1", "yellow")
    if _can("Quotation"):
        n, amt = frappe.db.sql("""select count(*), sum(base_grand_total) from `tabQuotation` where docstatus=1 and company=%s
            and status in ('Open','Replied') and valid_till between %s and %s""", (company, today, add_days(today, 7)))[0]
        add("quotations_expiring", n, "Quotations expiring this week", "Open quotes to follow up", "/selling/quotations?expiring=1", "orange", flt(amt))
    if _can("Material Request"):
        n = frappe.db.sql("""select count(*) from `tabMaterial Request` where docstatus=1 and company=%s
            and status in ('Pending','Partially Ordered') and transaction_date < %s""", (company, add_days(today, -7)))[0][0]
        add("material_requests", n, "Material requests waiting", "Pending over 7 days, not fully ordered", "/import/material-requests?waiting=1", "violet")
    if _can("Delivery Note"):
        n, amt = frappe.db.sql("""select count(*), sum(base_grand_total) from `tabDelivery Note` where docstatus=1
            and company=%s and status='To Bill' and posting_date < %s""", (company, add_days(today, -7)))[0]
        add("dn_to_bill", n, "Deliveries not invoiced", "Delivered over 7 days ago, still to bill", "/selling/delivery-notes?tobill=1", "green", flt(amt))
    if _can("Purchase Receipt"):
        n, amt = frappe.db.sql("""select count(*), sum(base_grand_total) from `tabPurchase Receipt` where docstatus=1
            and company=%s and status='To Bill' and posting_date < %s""", (company, add_days(today, -7)))[0]
        add("pr_to_bill", n, "Receipts not billed", "Received over 7 days ago, no supplier bill", "/purchase/receipts?tobill=1", "yellow", flt(amt))
    if _can("Work Order"):
        n = frappe.db.count("Work Order", {"docstatus": 1, "company": company, "status": "In Process"})
        add("wo_running", n, "Work orders running", "In process on the floor", "/production/work-orders?running=1", "orange")
    if _can("Purchase Order"):
        n, amt = frappe.db.sql("""select count(*), sum(base_grand_total) from `tabPurchase Order` where docstatus=1 and company=%s
            and status in ('To Receive and Bill','To Receive','To Bill')""", (company,))[0]
        add("po_running", n, "Purchase orders running", "Open — to receive or bill", "/import/purchase-orders?running=1", "sky", flt(amt))
    if _can("Bin") and frappe.db.exists("DocType", "Item Reorder"):
        n = frappe.db.sql("""select count(*) from `tabItem Reorder` r join `tabBin` b on b.item_code = r.parent
            and b.warehouse = r.warehouse join `tabWarehouse` w on w.name = b.warehouse
            where w.company = %s and r.warehouse_reorder_level > 0 and b.projected_qty < r.warehouse_reorder_level""",
                          (company,))[0][0]
        add("reorder", n, "Items below reorder level", "Projected qty under reorder level", "/reports/run/stock-projected-qty", "amber")
    if _can("Bank Transaction"):
        n = frappe.db.sql("""select count(*) from `tabBank Transaction` where docstatus=1 and company=%s
            and status in ('Unreconciled','Pending') and unallocated_amount > 0""", (company,))[0][0]
        add("bank", n, "Bank lines unreconciled", "Bank transactions to match", "/desk/bank-transaction?status=Unreconciled", "sky")
    add("approvals", approvals_count, "Approvals waiting", "Workflow and HR requests waiting for you", "/approvals/inbox", "yellow")
    if _can("CRM Task"):
        n = frappe.db.sql("""select count(*) from `tabCRM Task` where status in ('Backlog','Todo','In Progress')
            and assigned_to = %s and due_date < %s""", (user, now_datetime()))[0][0]
        add("followups", n, "Overdue follow-ups", "Assigned to you", "/crm/follow-ups?overdue=1", "rose")
    return out


def _activity(company, days=7, per_doctype=12):
    since = add_days(nowdate(), -(days - 1))
    feed = []
    for dt, (cat, amt, title) in ACTIVITY.items():
        if not _can(dt):
            continue
        fields = ["name", "docstatus", "owner", "modified", "creation"]
        if amt and _has(dt, amt):
            fields.append(f"`{amt}` as amount")
        if title and _has(dt, title):
            fields.append(f"`{title}` as title")
        if _has(dt, "status"):
            fields.append("status")
        filters = {"modified": [">=", since]}
        if _has(dt, "company"):
            filters["company"] = company
        for r in frappe.get_all(dt, filters=filters, fields=fields, order_by="modified desc", limit=per_doctype):
            status = r.get("status") or {0: "Draft", 1: "Submitted", 2: "Cancelled"}.get(r.docstatus, "")
            feed.append({"doctype": dt, "name": r.name, "category": cat, "title": r.get("title") or "",
                         "amount": flt(r.get("amount")) or None, "status": status, "owner": r.owner,
                         "owner_name": frappe.utils.get_fullname(r.owner), "at": str(r.modified), "created": str(r.creation)})
    feed.sort(key=lambda x: x["at"], reverse=True)
    today = nowdate()
    hours = [0] * 24
    mix = {}
    posted = 0.0
    for f in feed:
        if f["created"][:10] == today:
            hours[int(f["created"][11:13])] += 1
            mix[f["category"]] = mix.get(f["category"], 0) + 1
            if f["status"] not in ("Draft", "Cancelled") and f["category"] == "finance":
                posted += f["amount"] or 0
    return feed[:40], hours, mix, posted


def _week_days(company):
    """Documents created per day over the last 7 days, today included (same doctypes as the activity feed)."""
    today = getdate(nowdate())
    start = add_days(today, -6)
    counts = {}
    for dt in ACTIVITY:
        if not _can(dt):
            continue
        cond, args = "creation >= %s", [start]
        if _has(dt, "company"):
            cond += " and company = %s"
            args.append(company)
        for d, n in frappe.db.sql(f"select date(creation), count(*) from `tab{dt}` where {cond} group by date(creation)", args):
            counts[str(d)] = counts.get(str(d), 0) + n
    days = []
    for i in range(7):
        d = str(add_days(start, i))
        days.append({"date": d, "count": counts.get(d, 0)})
    return days


def _tiles(company):
    t, today = {}, getdate(nowdate())

    def put(key, value, label):
        if value is not None:
            t[key] = {"value": value, "label": label}

    if _can("Sales Order"):
        put("selling", frappe.db.count("Sales Order", {"docstatus": 1, "company": company,
                                                       "status": ["not in", ["Completed", "Closed", "Cancelled"]]}), "open orders")
    if _can("Purchase Order"):
        put("purchase", frappe.db.count("Purchase Order", {"docstatus": 1, "company": company,
                                                           "status": ["not in", ["Completed", "Closed", "Cancelled", "Delivered"]]}), "open POs")
    if _can("CRM Lead"):
        put("crm", frappe.db.count("CRM Lead", {"converted": 0}), "active leads")
    if _can("Journal Entry") or _can("Payment Entry"):
        n = (frappe.db.count("Journal Entry", {"docstatus": 1, "company": company, "posting_date": today}) if _can("Journal Entry") else 0) + \
            (frappe.db.count("Payment Entry", {"docstatus": 1, "company": company, "posting_date": today}) if _can("Payment Entry") else 0)
        put("accounting", n, "vouchers today")
    if _can("Bin"):
        n = frappe.db.sql("""select count(distinct b.item_code) from `tabBin` b join `tabWarehouse` w on w.name=b.warehouse
            where w.company=%s and b.actual_qty > 0""", (company,))[0][0]
        put("stock", n, "items in stock")
    if _can("Asset"):
        put("assets", frappe.db.count("Asset", {"docstatus": 1, "company": company}), "assets")
    if _can("Work Order"):
        put("production", frappe.db.count("Work Order", {"docstatus": 1, "company": company, "status": "In Process"}), "work orders running")
    if _can("Quality Inspection"):
        put("quality", frappe.db.count("Quality Inspection", {"docstatus": 0}), "inspections pending")
    if _can("Task"):
        put("projects", frappe.db.count("Task", {"status": ["in", ["Open", "Working", "Overdue"]], "exp_end_date": ["<=", today]}), "tasks due")
    if _can("Employee"):
        staff = frappe.db.count("Employee", {"status": "Active", "company": company})
        present = frappe.db.count("Attendance", {"attendance_date": today, "status": "Present", "docstatus": 1, "company": company}) if _can("Attendance") else 0
        put("hr", staff, f"staff · {present} present today")
    if _can("Salary Slip"):
        put("payroll", frappe.db.count("Salary Slip", {"company": company, "start_date": ["between", [get_first_day(today), get_last_day(today)]]}), "slips this month")
    if _can("Issue"):
        put("support", frappe.db.count("Issue", {"status": ["in", ["Open", "Replied"]]}), "open issues")
    if _can("Student"):
        put("education", frappe.db.count("Student", {"enabled": 1}), "active students")
    if frappe.has_permission("User", "read"):
        put("admin", frappe.db.count("User", {"enabled": 1, "user_type": "System User"}), "active users")
    return t


@frappe.whitelist()
def get_home(company: str | None = None, refresh: int = 0) -> dict:
    company = company or frappe.defaults.get_user_default("Company") or frappe.db.get_single_value("Global Defaults", "default_company")
    # Per user + company, briefly: the page polls on focus and the large-site queries take a few seconds.
    key = f"mm_core-home:{frappe.session.user}:{company}"
    if not frappe.utils.cint(refresh):
        cached = frappe.cache.get_value(key)
        if cached:
            return cached
    out = _compute(company)
    frappe.cache.set_value(key, out, expires_in_sec=CACHE_SECONDS)
    return out


def _compute(company):
    approvals = _block(_approvals, 30, company) or []
    activity = _block(_activity, company) or ([], [0] * 24, {}, 0.0)
    feed, hours, mix, posted = activity
    days = _block(_week_days, company) or []
    return {
        "company": company,
        "kpis": [],  # the home page no longer shows KPI tiles (_kpis kept for reuse)
        "approvals": approvals,
        "alerts": _block(_alerts, company, len(approvals)) or [],
        "activity": feed,
        "hours": hours,
        "days": days,
        "mix": mix,
        "stats": {"entries_today": next((d["count"] for d in days if d["date"] == nowdate()), sum(hours)),
                  "pending": len(approvals), "posted_today": posted},
        "tiles": _block(_tiles, company) or {},
        "todos": _block(_todos) or {"open": 0, "overdue": 0},
    }


@frappe.whitelist(methods=["POST"])
def approve(doctype: str, name: str) -> dict:
    """Apply the workflow transition whose action starts with "Approve" that the current user may take."""
    from frappe.model.workflow import apply_workflow, get_transitions

    if not frappe.db.exists("Workflow", {"document_type": doctype, "is_active": 1}):
        from mm_core.approvals import act

        return act(doctype, name, "Approve")              # HR requests without a workflow (expense claims…)
    doc = frappe.get_doc(doctype, name)
    actions = [t.action for t in get_transitions(doc) if (t.action or "").lower().startswith("approv")]
    if not actions:
        frappe.throw(frappe._("No approve action is available to you for {0} {1}").format(doctype, name))
    doc = apply_workflow(doc, actions[0])
    return {"name": doc.name, "workflow_state": doc.get("workflow_state")}
