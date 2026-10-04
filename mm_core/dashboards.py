"""Module dashboards for the React SPA (Accounts, Sales, Purchase, Stock, HR, Payroll, Production, Assets).

One whitelisted call per module returns everything its page draws, aggregated in SQL for the chosen
period (the SPA defaults to the fiscal year, Jul–Jun):

    {
      "period": {"from": ..., "to": ..., "months": ["Jul 26", ...]},
      "kpis":   [{"key", "label", "value", "format", "avg", "delta", "spark", "invert", "hint"}],
      "widgets":[{"id", "title", "subtitle", "type", "data", "series", "xKey", "money", "span", ...}],
    }

`format` is money | number | percent. `delta` is the month-on-month change of the last month in the
period that has data (in %); `invert` marks KPIs where a rise is bad (expenses, absence…). Widget
`type` is one of combo | bar | line | area | donut | pie | barlist — the SPA maps each to a chart.

Aggregating on the server keeps the page correct on large tables (GL Entry has 550k+ rows here) —
pulling rows into the browser and summing there only ever sees the first page.

Lives in mm_core so every site on the bench gets the generic dashboards (accounts, sales, stock, HR...);
the modules in MICROMAX_MODULES read MicroMax's spinning / export-import doctypes and fields, so they only
run where the micromax app is installed. micromax.dashboards re-exports this module for old callers.
"""

import re
from collections import Counter, defaultdict
from datetime import date

import frappe
from frappe import _
from frappe.utils import add_months, flt, getdate

MODULES = {
    "accounts": ("GL Entry", "_accounts"),
    "sales": ("Sales Invoice", "_sales"),
    "purchase": ("Purchase Invoice", "_purchase"),
    "stock": ("Stock Ledger Entry", "_stock"),
    "hr": ("Employee", "_hr"),
    "payroll": ("Salary Slip", "_payroll"),
    "production": ("Work Order", "_production"),
    "assets": ("Asset", "_assets"),
    "financials": ("GL Entry", "_financials"),
    "procurement": ("Purchase Order", "_procurement"),
    "so_analysis": ("Sales Order", "_so_analysis"),
    "do_analysis": ("Delivery Note", "_do_analysis"),
    "export_analysis": ("Sales Order", "_export_analysis"),
    "import_analysis": ("Purchase Order", "_import_analysis"),
    "quality": ("Quality Inspection", "_quality"),
    "wo_analysis": ("Work Order", "_wo_analysis"),
    "jc_analysis": ("Job Card", "_jc_analysis"),
}


CACHE_SECONDS = 15 * 60

# Dashboards built on MicroMax-only doctypes/fields (Work Order spinning fields, Export / Import Shipment, LC Proforma).
MICROMAX_MODULES = {"production", "wo_analysis", "export_analysis", "import_analysis"}


def _check_module(module: str) -> None:
    if module not in MODULES:
        frappe.throw(_("Unknown dashboard {0}").format(module))
    if module in MICROMAX_MODULES and "micromax" not in frappe.get_installed_apps():
        frappe.throw(_("The {0} dashboard needs the MicroMax app, which isn't installed on this site.").format(module))
    if not frappe.has_permission(MODULES[module][0], "read"):
        frappe.throw(_("Not permitted to view the {0} dashboard").format(module), frappe.PermissionError)

# Which dashboard filters each module honours (the UI shows only these; others are ignored).
MODULE_FILTERS = {
    "accounts": ("cost_center", "account", "account_group", "account_type", "customer", "supplier"),
    "financials": ("cost_center", "account", "account_group", "account_type", "customer", "supplier"),
    "sales": ("cost_center", "customer", "item_group", "item"), "so_analysis": ("cost_center", "customer", "item_group", "item"),
    "do_analysis": ("cost_center", "customer", "item_group", "item"), "export_analysis": ("customer", "item_group", "item"),
    "purchase": ("cost_center", "supplier", "item_group", "item"), "procurement": ("cost_center", "supplier", "item_group", "item"),
    "import_analysis": ("supplier", "item_group", "item"), "stock": ("item_group", "item"),
    "production": ("stream", "wo_status"), "wo_analysis": ("stream", "wo_status"), "jc_analysis": ("stream", "wo_status"),
    "quality": ("item",), "hr": ("department",), "payroll": ("department",), "assets": ("asset_category",),
}


@frappe.whitelist()
def get_permitted_modules() -> list[str]:
    """Dashboards the current user can open on this site: read access to the module's main doctype, and
    micromax installed for the MicroMax-only ones. The SPA loads only these (a CRM-only user sees SO / DO
    analysis, not GL-based accounts or purchase dashboards)."""
    has_micromax = "micromax" in frappe.get_installed_apps()
    return [m for m, (doctype, _fn) in MODULES.items()
            if (has_micromax or m not in MICROMAX_MODULES)
            and frappe.db.exists("DocType", doctype) and frappe.has_permission(doctype, "read")]


@frappe.whitelist()
def get_dashboard_filters() -> dict:
    return {k: list(v) for k, v in MODULE_FILTERS.items()}


@frappe.whitelist()
def get_dashboard(module: str, from_date: str, to_date: str, company: str | None = None, refresh: int | None = None,
                  tolerance: float | None = None, cost_center: str | None = None, customer: str | None = None,
                  supplier: str | None = None, item: str | None = None, item_group: str | None = None,
                  department: str | None = None, asset_category: str | None = None, account: str | None = None,
                  account_group: str | None = None, account_type: str | None = None, stream: str | None = None,
                  wo_status: str | None = None) -> dict:
    _check_module(module)
    fn = MODULES[module][1]
    f, t = getdate(from_date), getdate(to_date)
    if f > t:
        frappe.throw(_("From date must be before To date"))
    # GL / attendance aggregates take seconds on this data volume, so results are cached briefly per period
    tol = flt(tolerance) if tolerance not in (None, "") else 5.0
    dims = _dims(cost_center=cost_center, customer=customer, supplier=supplier, item=item, item_group=item_group,
                 department=department, asset_category=asset_category, account=account, account_group=account_group,
                 account_type=account_type, stream=stream, wo_status=wo_status)
    dims = {k: v for k, v in dims.items() if k in MODULE_FILTERS.get(module, ())}
    key = f"micromax-dashboard:{module}:{f}:{t}:{company or ''}:{tol}:{_dims_key(dims)}"
    if not frappe.utils.cint(refresh):
        cached = frappe.cache.get_value(key)
        if cached:
            return cached
    ctx = _Ctx(f, t, company or None, dims)
    ctx.tolerance = tol
    kpis, widgets = globals()[fn](ctx)
    try:
        insights = _insights(module, {k["key"]: k for k in kpis}, {w["id"]: w for w in widgets})
    except Exception:
        frappe.log_error(title=f"Dashboard insights failed: {module}")
        insights = []
    out = {"period": {"from": str(f), "to": str(t), "months": [lbl for _k, lbl in ctx.months]}, "kpis": kpis, "widgets": widgets,
           "insights": insights, "generated_at": str(frappe.utils.now_datetime()),
           "filters": {"applied": dims, "used": sorted(ctx.dims_used)}}
    frappe.cache.set_value(key, out, expires_in_sec=CACHE_SECONDS)
    return out


# ----------------------------------------------------------------------------- helpers
def _dims(**kw) -> dict:
    """Dashboard filters that are set (cost centre, customer, supplier, item)."""
    return {k: v for k, v in kw.items() if v not in (None, "")}


def _dims_key(dims: dict) -> str:
    return "|".join(f"{k}={dims[k]}" for k in sorted(dims))


# Item rows per document type, for "documents that include the item".
_ITEM_CHILD = {"Sales Invoice": "Sales Invoice Item", "Sales Order": "Sales Order Item", "Delivery Note": "Delivery Note Item",
               "Purchase Invoice": "Purchase Invoice Item", "Purchase Order": "Purchase Order Item",
               "Purchase Receipt": "Purchase Receipt Item", "Material Request": "Material Request Item",
               "Stock Entry": "Stock Entry Detail", "Quotation": "Quotation Item", "Import Cost Sheet": "Import Cost Sheet Item",
               "LC Proforma": "LC Proforma Item"}
_ITEM_COL = {"Import Cost Sheet Item": "item", "LC Proforma Item": "item"}
_TABLE_RE = re.compile(r"`tab([^`]+)`(?:\s+(?:as\s+)?(?!where\b|join\b|left\b|inner\b|on\b|group\b|order\b|limit\b)(\w+))?", re.I)


class _Ctx:
    def __init__(self, f: date, t: date, company: str | None, dims: dict | None = None):
        self.f, self.t, self.company = f, t, company
        self.months = []
        d = date(f.year, f.month, 1)
        while d <= t:
            self.months.append((d.strftime("%Y-%m"), d.strftime("%b %y")))
            d = getdate(add_months(d, 1))
        self.dims = dims or {}
        self.dims_used = set()
        self.p = {"f": f, "t": t, "co": company, **{f"dim_{k}": v for k, v in self.dims.items()}}
        # tree filters include their sub-nodes (nested set)
        for dim, dt, pre in (("cost_center", "Cost Center", "cc"), ("item_group", "Item Group", "ig"), ("department", "Department", "dep"),
                             ("account", "Account", "acc"), ("account_group", "Account", "accg")):
            if self.dims.get(dim):
                lft, rgt = frappe.db.get_value(dt, self.dims[dim], ["lft", "rgt"]) or (0, 0)
                self.p.update({f"dim_{pre}_lft": lft, f"dim_{pre}_rgt": rgt})

    def co(self, alias: str = "") -> str:
        """`and <alias>.company = %(co)s` when a company is selected — plus a marker that `sql()` turns into the
        dashboard filters (cost centre / customer / supplier / item) the aliased table supports."""
        cond = f" and {alias + '.' if alias else ''}company = %(co)s" if self.company else ""
        return cond + (f" /*mmdim:{alias}*/" if self.dims else "")

    def sql(self, q: str, extra: dict | None = None):
        if self.dims and "/*mmdim:" in q:
            q = self._apply_dims(q)
        return frappe.db.sql(q, {**self.p, **(extra or {})}, as_dict=True)

    def _apply_dims(self, q: str) -> str:
        tables = {}
        for m in _TABLE_RE.finditer(q):
            if m.group(2):
                tables.setdefault(m.group(2), m.group(1))
        from_re = re.compile(r"\bfrom\s+`tab([^`]+)`(?:\s+(?:as\s+)?(?!where\b|join\b|left\b|inner\b|on\b|group\b|order\b|limit\b)(\w+))?", re.I)

        def repl(m):
            alias = m.group(1)
            if alias:
                return self._dim_cond(alias, tables.get(alias), tables)
            # un-aliased co(): the table of the nearest FROM before it (its alias, if it has one)
            last = None
            for fm in from_re.finditer(q, 0, m.start()):
                last = fm
            if not last:
                return ""
            return self._dim_cond(last.group(2) or f"`tab{last.group(1)}`", last.group(1), tables)

        return re.sub(r"/\*mmdim:(\w*)\*/", repl, q)

    def _dim_cond(self, alias: str, doctype: str | None, tables: dict | None = None) -> str:
        if not doctype:
            return ""
        a = f"{alias}."
        cols = set(frappe.db.get_table_columns(doctype))
        child = _ITEM_CHILD.get(doctype)
        child_cols = set(frappe.db.get_table_columns(child)) if child else set()
        # the query's own alias for this document's item rows, when it joins them (e.g. "Top products")
        child_alias = next((al for al, dt in (tables or {}).items() if dt == child), None)
        out = []
        for dim in ("customer", "supplier"):
            if not self.dims.get(dim):
                continue
            label = dim.capitalize()
            if dim in cols:
                out.append(f"{a}{dim} = %(dim_{dim})s")
            elif doctype == "GL Entry":
                # the ledger for a party = every voucher posted against that party (its invoices' revenue, tax, stock...)
                # ... plus its delivery notes / receipts, which carry the cost of goods but no party line
                stock_doc = "Delivery Note" if dim == "customer" else "Purchase Receipt"
                out.append(f"""({a}voucher_no in (select pg.voucher_no from `tabGL Entry` pg where pg.party_type = '{label}'
                    and pg.party = %(dim_{dim})s and pg.is_cancelled = 0)
                    or {a}voucher_no in (select sd.name from `tab{stock_doc}` sd where sd.{dim} = %(dim_{dim})s and sd.docstatus = 1))""")
            elif {"party_type", "party"} <= cols:
                out.append(f"({a}party_type = '{label}' and {a}party = %(dim_{dim})s)")
            else:
                continue
            self.dims_used.add(dim)
        if self.dims.get("cost_center"):
            sub = "select name from `tabCost Center` where lft >= %(dim_cc_lft)s and rgt <= %(dim_cc_rgt)s"
            conds = []
            if "cost_center" in cols:
                conds.append(f"{a}cost_center in ({sub})")
            if child_alias and "cost_center" in child_cols:
                conds.append(f"{child_alias}.cost_center in ({sub})")
            elif child and "cost_center" in child_cols:
                conds.append(f"exists (select 1 from `tab{child}` dc where dc.parent = {a}name and dc.cost_center in ({sub}))")
            if not conds and "payroll_cost_centers" in {df.fieldname for df in frappe.get_meta(doctype).get_table_fields()}:
                conds.append(f"exists (select 1 from `tabEmployee Cost Center` dc where dc.parent = {a}name and dc.cost_center in ({sub}))")
            if not conds and doctype in ("Salary Slip", "Employee", "Attendance", "Leave Application"):
                # people are costed through their salary structure assignment's payroll cost centres
                conds.append(f"""{a}employee in (select ssa.employee from `tabSalary Structure Assignment` ssa
                    join `tabEmployee Cost Center` dc on dc.parent = ssa.name where ssa.docstatus = 1 and dc.cost_center in ({sub}))"""
                             if doctype != "Employee" else
                             f"""{a}name in (select ssa.employee from `tabSalary Structure Assignment` ssa
                    join `tabEmployee Cost Center` dc on dc.parent = ssa.name where ssa.docstatus = 1 and dc.cost_center in ({sub}))""")
            if conds:
                out.append("(" + " or ".join(conds) + ")")
                self.dims_used.add("cost_center")
        if self.dims.get("item"):
            col = _ITEM_COL.get(child, "item_code") if child else "item_code"
            if "item_code" in cols:
                out.append(f"{a}item_code = %(dim_item)s")
            elif "production_item" in cols:
                out.append(f"{a}production_item = %(dim_item)s")
            elif child_alias:
                out.append(f"{child_alias}.{col} = %(dim_item)s")
            elif child:
                out.append(f"exists (select 1 from `tab{child}` di where di.parent = {a}name and di.{col} = %(dim_item)s)")
            if any("dim_item" in c for c in out):
                self.dims_used.add("item")
        if self.dims.get("item_group"):
            groups = "select name from `tabItem Group` where lft >= %(dim_ig_lft)s and rgt <= %(dim_ig_rgt)s"
            items = f"select name from `tabItem` where item_group in ({groups})"
            col = _ITEM_COL.get(child, "item_code") if child else "item_code"
            cond = None
            if "item_group" in cols:
                cond = f"{a}item_group in ({groups})"
            elif "item_code" in cols:
                cond = f"{a}item_code in ({items})"
            elif "production_item" in cols:
                cond = f"{a}production_item in ({items})"
            elif child_alias:
                cond = f"{child_alias}.{col} in ({items})"
            elif child:
                cond = f"exists (select 1 from `tab{child}` dg where dg.parent = {a}name and dg.{col} in ({items}))"
            if cond:
                out.append(cond)
                self.dims_used.add("item_group")
        if self.dims.get("department"):
            deps = "select name from `tabDepartment` where lft >= %(dim_dep_lft)s and rgt <= %(dim_dep_rgt)s"
            if "department" in cols:
                out.append(f"{a}department in ({deps})")
                self.dims_used.add("department")
            elif "employee" in cols:
                out.append(f"{a}employee in (select name from `tabEmployee` where department in ({deps}))")
                self.dims_used.add("department")
        # ledger: an account (with everything under it), a group of accounts, or an account type
        for dim, pre in (("account", "acc"), ("account_group", "accg")):
            if self.dims.get(dim) and "account" in cols and doctype in ("GL Entry", "Payment Ledger Entry", "Journal Entry Account"):
                out.append(f"{a}account in (select name from `tabAccount` where lft >= %(dim_{pre}_lft)s and rgt <= %(dim_{pre}_rgt)s)")
                self.dims_used.add(dim)
        if self.dims.get("account_type") and "account" in cols and doctype in ("GL Entry", "Payment Ledger Entry", "Journal Entry Account"):
            out.append(f"{a}account in (select name from `tabAccount` where account_type = %(dim_account_type)s)")
            self.dims_used.add("account_type")
        # production: conversion (finished goods into a third-party warehouse) vs own production, and work-order status
        wo_conds = []
        if self.dims.get("stream"):
            conv = "lower(ifnull({0}fg_warehouse, '')) like '%%%%third party%%%%'"
            wo_conds.append(conv if self.dims["stream"] == "Conversion" else f"not ({conv})")
        if self.dims.get("wo_status"):
            wo_conds.append("{0}status = %(dim_wo_status)s")
        if wo_conds:
            if doctype == "Work Order":
                out.extend(c.format(a) for c in wo_conds)
            elif "work_order" in cols:
                out.append(f"{a}work_order in (select wf.name from `tabWork Order` wf where "
                           + " and ".join(c.format("wf.") for c in wo_conds) + ")")
            if doctype == "Work Order" or "work_order" in cols:
                self.dims_used.update(k for k in ("stream", "wo_status") if self.dims.get(k))
        if self.dims.get("asset_category"):
            if "asset_category" in cols:
                out.append(f"{a}asset_category = %(dim_asset_category)s")
            elif "asset" in cols:
                out.append(f"{a}asset in (select name from `tabAsset` where asset_category = %(dim_asset_category)s)")
            elif doctype == "Asset Movement":
                out.append(f"""exists (select 1 from `tabAsset Movement Item` dm join `tabAsset` da on da.name = dm.asset
                    where dm.parent = {a}name and da.asset_category = %(dim_asset_category)s)""")
            else:
                return (" and " + " and ".join(out)) if out else ""
            self.dims_used.add("asset_category")
        return (" and " + " and ".join(out)) if out else ""

    def monthly(self, rows, key="m", fields=("v",)):
        """Rows keyed by 'YYYY-MM' → one dict per month in the period (zeros filled), labelled 'Jul 26'."""
        by = {r[key]: r for r in rows}
        out = []
        for k, lbl in self.months:
            r = by.get(k, {})
            out.append({"month": lbl, **{fld: flt(r.get(fld)) for fld in fields}})
        return out


def _kpi(key, label, value, fmt="money", monthly=None, invert=False, hint=None, avg_label="per month"):
    """KPI tile: total, monthly average, last-vs-previous-month change and a sparkline."""
    spark = [flt(x) for x in (monthly or [])]
    delta = None
    filled = [i for i, v in enumerate(spark) if v]
    if len(filled) >= 2:
        last, prev = spark[filled[-1]], spark[filled[-2]]
        if prev:
            delta = round((last - prev) / abs(prev) * 100, 1)
    avg = round(sum(spark) / len(filled), 2) if filled and fmt != "percent" else None
    return {"key": key, "label": label, "value": flt(value, 2), "format": fmt, "avg": avg, "avg_label": avg_label,
            "delta": delta, "spark": spark, "invert": invert, "hint": hint}


def _w(id, title, type, data, subtitle=None, series=None, xKey="month", money=False, span=1, **kw):
    return {"id": id, "title": title, "subtitle": subtitle, "type": type, "data": data, "series": series or [],
            "xKey": xKey, "money": money, "span": span, **kw}


def _pairs(rows, label="label", value="v", limit=None, other=True):
    """[{label, value}] sorted desc; the tail beyond `limit` folded into 'Other'."""
    rows = sorted(({"label": r[label] or _("Not set"), "value": flt(r[value])} for r in rows), key=lambda r: -r["value"])
    rows = [r for r in rows if r["value"]]
    if limit and len(rows) > limit:
        head, tail = rows[:limit], rows[limit:]
        if other:
            head.append({"label": _("Other"), "value": sum(r["value"] for r in tail)})
        rows = head
    return rows


AGEING_BUCKETS = [("Not due", 0), ("1-30", 30), ("31-60", 60), ("61-90", 90), ("91-180", 180), ("181-365", 365), ("365+", None)]
AGEING_COLORS = ["hsl(160 84% 39%)", "hsl(142 71% 45%)", "hsl(48 96% 48%)", "hsl(35 92% 50%)", "hsl(20 90% 50%)", "hsl(351 95% 59%)", "hsl(345 83% 41%)"]
OVERDUE_90 = ("91-180", "181-365", "365+")


def _bucket_of(days):
    for label, upto in AGEING_BUCKETS:
        if upto is None or days <= upto:
            return label


def _ageing_invoices(ctx, doctype):
    """Open invoices as of the period end with days overdue (due date, else posting date) and their bucket."""
    party = "customer" if doctype == "Sales Invoice" else "supplier"
    rows = ctx.sql(f"""
        select name, posting_date, ifnull(due_date, posting_date) due, {party} party, {party}_name party_name,
               grand_total, outstanding_amount, datediff(%(t)s, ifnull(due_date, posting_date)) days
        from `tab{doctype}` where docstatus = 1 and outstanding_amount > 0 and posting_date <= %(t)s {ctx.co()}""")
    for r in rows:
        r.bucket = _bucket_of(r.days)
    return rows


def _ageing(ctx, doctype):
    by = defaultdict(float)
    for r in _ageing_invoices(ctx, doctype):
        by[r.bucket] += flt(r.outstanding_amount)
    return [{"bucket": b, "v": round(by.get(b, 0), 2)} for b, _u in AGEING_BUCKETS]


def _ageing_by_party(ctx, doctype):
    """One row per party: outstanding split across the ageing buckets, largest total first."""
    parties = {}
    for r in _ageing_invoices(ctx, doctype):
        p = parties.setdefault(r.party, {"_party": "Customer" if doctype == "Sales Invoice" else "Supplier", "party": r.party,
                                         "party_name": r.party_name or r.party, "invoices": 0, "total": 0.0, "days": 0.0,
                                         **{b: 0.0 for b, _u in AGEING_BUCKETS}})
        amt = flt(r.outstanding_amount)
        p[r.bucket] += amt
        p["total"] += amt
        p["invoices"] += 1
        p["days"] += max(r.days, 0) * amt
    out = []
    for p in parties.values():
        p["days"] = round(p["days"] / p["total"]) if p["total"] else 0          # amount-weighted average days overdue
        p["over90"] = round(sum(p[b] for b in OVERDUE_90), 2)
        out.append(p)
    return sorted(out, key=lambda x: -x["total"])


def _ageing_columns(party_label):
    return ([{"key": "party", "label": party_label, "doctype_key": "_party"}, {"key": "invoices", "label": _("Invoices"), "format": "number", "align": "right"}]
            + [{"key": b, "label": _(b) if b == "Not due" else f"{b} d", "format": "money", "align": "right"} for b, _u in AGEING_BUCKETS]
            + [{"key": "total", "label": _("Total"), "format": "money", "align": "right"},
               {"key": "days", "label": _("Avg days overdue"), "format": "days", "align": "right"}])


# ----------------------------------------------------------------------------- accounts
def _accounts(ctx):
    # One pass over the P&L postings (month × account × cost centre); the monthly totals, the expense-account
    # mix and the cost-centre split are all rolled up from it. The year-end Period Closing Voucher is left
    # out, or it zeroes the last month's income and expenses.
    pl = ctx.sql(f"""
        select date_format(g.posting_date, '%%Y-%%m') m, a.root_type rt, a.account_name acc, max(a.parent_account) grp,
               ifnull(g.cost_center, 'Not set') cc, sum(g.debit - g.credit) bal
        from `tabGL Entry` g join `tabAccount` a on a.name = g.account
        where g.is_cancelled = 0 and g.posting_date between %(f)s and %(t)s {ctx.co('g')}
          and a.root_type in ('Income', 'Expense')
          and g.voucher_type != 'Period Closing Voucher'
        group by m, g.account, g.cost_center""")
    inc, exp, by_acc, by_cc, inc_grp, exp_grp = {}, {}, {}, {}, {}, {}
    grp_label = lambda g: re.sub(r"\s+-\s+[^-]+$", "", g or _("Ungrouped"))  # drop the " - MEPL" company suffix  # noqa: E731
    for r in pl:
        if r.rt == "Income":
            inc[r.m] = inc.get(r.m, 0) - flt(r.bal)
            inc_grp[grp_label(r.grp)] = inc_grp.get(grp_label(r.grp), 0) - flt(r.bal)
        else:
            exp_grp[grp_label(r.grp)] = exp_grp.get(grp_label(r.grp), 0) + flt(r.bal)
            exp[r.m] = exp.get(r.m, 0) + flt(r.bal)
            by_acc[r.acc] = by_acc.get(r.acc, 0) + flt(r.bal)
            by_cc[r.cc] = by_cc.get(r.cc, 0) + flt(r.bal)
    monthly = [{"month": lbl, "income": inc.get(k, 0), "expense": exp.get(k, 0), "profit": inc.get(k, 0) - exp.get(k, 0)}
               for k, lbl in ctx.months]
    tot_inc, tot_exp = sum(inc.values()), sum(exp.values())
    margin = [(r["profit"] / r["income"] * 100) if r["income"] else 0 for r in monthly]
    top_exp = [{"label": k, "v": v} for k, v in by_acc.items()]
    cc = [{"label": k, "v": v} for k, v in by_cc.items()]
    cash = ctx.sql(f"""
        select a.account_name label, sum(g.debit - g.credit) v
        from `tabGL Entry` g join `tabAccount` a on a.name = g.account
        where g.is_cancelled = 0 and g.posting_date <= %(t)s {ctx.co('g')} and a.account_type in ('Bank', 'Cash')
        group by g.account having abs(v) > 0.5""")
    vouchers = ctx.sql(" union all ".join(
        f"""select '{dt}' label, count(*) v from `tab{dt}` where docstatus = 1 and posting_date between %(f)s and %(t)s {ctx.co()}"""
        for dt in ("Sales Invoice", "Purchase Invoice", "Payment Entry", "Journal Entry", "Stock Entry", "Delivery Note", "Purchase Receipt")))
    ar, ap = _ageing(ctx, "Sales Invoice"), _ageing(ctx, "Purchase Invoice")
    ageing = [{"bucket": a["bucket"], "receivable": a["v"], "payable": p["v"]} for a, p in zip(ar, ap)]
    ar_party, ap_party = _ageing_by_party(ctx, "Sales Invoice"), _ageing_by_party(ctx, "Purchase Invoice")
    ar90, ap90 = sum(a["v"] for a in ar if a["bucket"] in OVERDUE_90), sum(p["v"] for p in ap if p["bucket"] in OVERDUE_90)
    ar_tot, ap_tot = sum(a["v"] for a in ar), sum(p["v"] for p in ap)
    tot_cash = sum(flt(r.v) for r in cash)

    kpis = [
        _kpi("income", _("Income"), tot_inc, monthly=[r["income"] for r in monthly]),
        _kpi("expense", _("Expenses"), tot_exp, monthly=[r["expense"] for r in monthly], invert=True),
        _kpi("profit", _("Net profit"), tot_inc - tot_exp, monthly=[r["profit"] for r in monthly]),
        _kpi("margin", _("Net margin"), (tot_inc - tot_exp) / tot_inc * 100 if tot_inc else 0, "percent", monthly=margin),
        _kpi("cash", _("Cash & bank"), tot_cash, hint=_("Balance on {0}").format(frappe.format(ctx.t, "Date"))),
        _kpi("receivable", _("Receivables"), sum(a["v"] for a in ar), invert=True, hint=_("Outstanding on {0}").format(frappe.format(ctx.t, "Date"))),
        _kpi("payable", _("Payables"), sum(p["v"] for p in ap), hint=_("Outstanding on {0}").format(frappe.format(ctx.t, "Date"))),
        _kpi("ar_over90", _("Receivables 90+ days"), ar90, invert=True,
             hint=_("{0} of receivables · {1} customers").format(f"{round(ar90 / ar_tot * 100, 1) if ar_tot else 0}%", sum(1 for r in ar_party if r["over90"]))),
        _kpi("ap_over90", _("Payables 90+ days"), ap90, invert=True,
             hint=_("{0} of payables · {1} suppliers").format(f"{round(ap90 / ap_tot * 100, 1) if ap_tot else 0}%", sum(1 for r in ap_party if r["over90"]))),
    ]
    widgets = [
        _w("pl", _("Income vs expenses"), "combo", monthly, _("Monthly, with net profit"), money=True, span=2,
           series=[{"key": "income", "label": _("Income")}, {"key": "expense", "label": _("Expenses"), "color": "hsl(351 95% 59%)"},
                   {"key": "profit", "label": _("Net profit"), "type": "line", "color": "hsl(160 84% 39%)"}]),
        _w("exp_mix", _("Where the money went"), "gauge", _pairs(top_exp, limit=8), _("Top expense accounts, share of total expenses"), money=True),
        _w("inc_grp", _("Income by group"), "donut", _pairs([{"label": k, "v": v} for k, v in inc_grp.items() if v > 0], limit=7),
           _("Income by parent account group"), money=True),
        _w("exp_grp", _("Expenses by group"), "donut", _pairs([{"label": k, "v": v} for k, v in exp_grp.items() if v > 0], limit=7),
           _("Expenses by parent account group"), money=True),
        _w("margin", _("Net margin trend"), "line", [{"month": r["month"], "margin": round(m, 1)} for r, m in zip(monthly, margin)],
           _("Net profit as % of income"), series=[{"key": "margin", "label": _("Margin %")}], percent=True),
        _w("ageing", _("Receivables vs payables ageing"), "bar", ageing, _("Outstanding by days overdue, on {0}").format(frappe.format(ctx.t, "Date")),
           xKey="bucket", money=True, span=2,
           series=[{"key": "receivable", "label": _("Receivable"), "color": "hsl(221 83% 53%)"}, {"key": "payable", "label": _("Payable"), "color": "hsl(35 92% 50%)"}]),
        _w("ar_buckets", _("Receivable ageing buckets"), "gauge",
           [{"label": b["bucket"] if b["bucket"] == "Not due" else f"{b['bucket']} days", "value": b["v"], "color": c} for b, c in zip(ar, AGEING_COLORS)],
           _("Share of receivables in each bucket"), money=True),
        _w("ap_buckets", _("Payable ageing buckets"), "gauge",
           [{"label": b["bucket"] if b["bucket"] == "Not due" else f"{b['bucket']} days", "value": b["v"], "color": c} for b, c in zip(ap, AGEING_COLORS)],
           _("Share of payables in each bucket"), money=True),
        _w("cash", _("Cash & bank balances"), "barlist", _pairs(cash, limit=8, other=False), money=True, span=2),
        _w("ar_party", _("Receivable ageing by customer"), "table", ar_party, _("Outstanding sales invoices by bucket, largest balance first"),
           span=3, xKey="", columns=_ageing_columns(_("Customer"))),
        _w("ap_party", _("Payable ageing by supplier"), "table", ap_party, _("Outstanding purchase invoices by bucket, largest balance first"),
           span=3, xKey="", columns=_ageing_columns(_("Supplier"))),
        _w("cc", _("Expenses by cost centre"), "bar", [{"cc": r["label"], "v": r["value"]} for r in _pairs(cc, limit=8)], xKey="cc",
           money=True, series=[{"key": "v", "label": _("Expenses")}], span=2),
        _w("vouchers", _("Documents posted"), "pie", _pairs(vouchers), _("Submitted documents by type")),
    ]
    return kpis, widgets


# ----------------------------------------------------------------------------- sales
def _sales(ctx):
    si = ctx.sql(f"""
        select date_format(posting_date, '%%Y-%%m') m, sum(base_net_total) v, count(*) n, sum(total_qty) q
        from `tabSales Invoice` where docstatus = 1 and posting_date between %(f)s and %(t)s {ctx.co()} group by m""")
    so = ctx.sql(f"""
        select date_format(transaction_date, '%%Y-%%m') m, sum(base_net_total) v from `tabSales Order`
        where docstatus = 1 and transaction_date between %(f)s and %(t)s {ctx.co()} group by m""")
    pe = ctx.sql(f"""
        select date_format(posting_date, '%%Y-%%m') m, sum(base_received_amount) v from `tabPayment Entry`
        where docstatus = 1 and payment_type = 'Receive' and party_type = 'Customer'
          and posting_date between %(f)s and %(t)s {ctx.co()} group by m""")
    m_si = ctx.monthly(si, fields=("v", "n", "q"))
    m_so = {r["month"]: r["v"] for r in ctx.monthly(so)}
    m_pe = {r["month"]: r["v"] for r in ctx.monthly(pe)}
    flow = [{"month": r["month"], "ordered": m_so[r["month"]], "invoiced": r["v"], "collected": m_pe[r["month"]]} for r in m_si]
    tot, n, q = sum(r["v"] for r in m_si), sum(r["n"] for r in m_si), sum(r["q"] for r in m_si)
    rate = [(r["v"] / r["q"]) if r["q"] else 0 for r in m_si]

    cust = ctx.sql(f"""select customer label, sum(base_net_total) v from `tabSales Invoice`
        where docstatus = 1 and posting_date between %(f)s and %(t)s {ctx.co()} group by customer""")
    cgrp = ctx.sql(f"""select ifnull(customer_group, 'Not set') label, sum(base_net_total) v from `tabSales Invoice`
        where docstatus = 1 and posting_date between %(f)s and %(t)s {ctx.co()} group by customer_group""")
    items = ctx.sql(f"""
        select i.item_group grp, i.item_code code, max(i.item_name) name, sum(i.base_net_amount) v, sum(i.stock_qty) q
        from `tabSales Invoice Item` i join `tabSales Invoice` s on s.name = i.parent
        where s.docstatus = 1 and s.posting_date between %(f)s and %(t)s {ctx.co('s')} group by i.item_group, i.item_code""")
    groups = {}
    for r in items:
        groups[r.grp or _("Not set")] = groups.get(r.grp or _("Not set"), 0) + flt(r.v)
    status = ctx.sql(f"""select status label, count(*) v from `tabSales Invoice`
        where docstatus = 1 and posting_date between %(f)s and %(t)s {ctx.co()} group by status""")
    outstanding = ctx.sql(f"""select sum(outstanding_amount) v from `tabSales Invoice`
        where docstatus = 1 and outstanding_amount > 0 and posting_date <= %(t)s {ctx.co()}""")[0].v

    kpis = [
        _kpi("sales", _("Net sales"), tot, monthly=[r["v"] for r in m_si]),
        _kpi("invoices", _("Invoices"), n, "number", monthly=[r["n"] for r in m_si]),
        _kpi("avg_invoice", _("Average invoice"), tot / n if n else 0, monthly=[(r["v"] / r["n"]) if r["n"] else 0 for r in m_si]),
        _kpi("qty", _("Quantity sold"), q, "number", monthly=[r["q"] for r in m_si]),
        _kpi("rate", _("Average rate / unit"), tot / q if q else 0, monthly=rate),
        _kpi("orders", _("Orders booked"), sum(m_so.values()), monthly=list(m_so.values())),
        _kpi("collected", _("Collected"), sum(m_pe.values()), monthly=list(m_pe.values())),
        _kpi("outstanding", _("Outstanding"), outstanding, invert=True, hint=_("Unpaid invoices on {0}").format(frappe.format(ctx.t, "Date"))),
    ]
    widgets = [
        _w("trend", _("Sales & quantity"), "combo", [{"month": r["month"], "v": r["v"], "q": r["q"]} for r in m_si], _("Net sales (bars) and quantity (line)"),
           money=True, span=2, dualAxis=True,
           series=[{"key": "v", "label": _("Net sales")}, {"key": "q", "label": _("Quantity"), "format": "number", "type": "line", "axis": "right", "color": "hsl(35 92% 50%)"}]),
        _w("groups", _("Sales by item group"), "donut", _pairs([{"label": k, "v": v} for k, v in groups.items()], limit=6), money=True),
        _w("flow", _("Ordered → invoiced → collected"), "line", flow, _("Monthly order book, billing and cash in"), money=True, span=2,
           series=[{"key": "ordered", "label": _("Ordered")}, {"key": "invoiced", "label": _("Invoiced")}, {"key": "collected", "label": _("Collected")}]),
        _w("customers", _("Top customers"), "barlist", _pairs(cust, limit=8, other=False), money=True),
        _w("items", _("Top items by value"), "bar", [{"item": r.name or r.code, "v": flt(r.v)} for r in sorted(items, key=lambda r: -flt(r.v))[:8]],
           xKey="item", money=True, series=[{"key": "v", "label": _("Net sales")}], span=2),
        _w("status", _("Invoice status"), "pie", _pairs(status)),
        _w("cgroup", _("Sales by customer group"), "donut", _pairs(cgrp, limit=6), money=True),
        _w("rate", _("Average rate per unit"), "area", [{"month": r["month"], "rate": round(x, 2)} for r, x in zip(m_si, rate)],
           _("Net sales ÷ quantity"), money=True, series=[{"key": "rate", "label": _("Rate")}], span=2),
    ]
    return kpis, widgets


# ----------------------------------------------------------------------------- purchase
def _purchase(ctx):
    pi = ctx.sql(f"""
        select date_format(posting_date, '%%Y-%%m') m, sum(base_net_total) v, count(*) n, sum(base_total_taxes_and_charges) tax
        from `tabPurchase Invoice` where docstatus = 1 and posting_date between %(f)s and %(t)s {ctx.co()} group by m""")
    po = ctx.sql(f"""
        select date_format(transaction_date, '%%Y-%%m') m, sum(base_net_total) v, count(*) n from `tabPurchase Order`
        where docstatus = 1 and transaction_date between %(f)s and %(t)s {ctx.co()} group by m""")
    pr = ctx.sql(f"""
        select date_format(posting_date, '%%Y-%%m') m, sum(total_qty) v from `tabPurchase Receipt`
        where docstatus = 1 and posting_date between %(f)s and %(t)s {ctx.co()} group by m""")
    pe = ctx.sql(f"""
        select date_format(posting_date, '%%Y-%%m') m, sum(base_paid_amount) v from `tabPayment Entry`
        where docstatus = 1 and payment_type = 'Pay' and party_type = 'Supplier'
          and posting_date between %(f)s and %(t)s {ctx.co()} group by m""")
    m_pi, m_po = ctx.monthly(pi, fields=("v", "n", "tax")), ctx.monthly(po, fields=("v", "n"))
    m_pr, m_pe = ctx.monthly(pr), ctx.monthly(pe)
    po_qty = ctx.sql(f"""
        select date_format(transaction_date, '%%Y-%%m') m, sum(total_qty) v from `tabPurchase Order`
        where docstatus = 1 and transaction_date between %(f)s and %(t)s {ctx.co()} group by m""")
    m_poq = {r["month"]: r["v"] for r in ctx.monthly(po_qty)}
    po_tot, po_n = sum(r["v"] for r in m_po), sum(r["n"] for r in m_po)

    sup = ctx.sql(f"""select supplier label, sum(base_net_total) v from `tabPurchase Invoice`
        where docstatus = 1 and posting_date between %(f)s and %(t)s {ctx.co()} group by supplier""")
    items = ctx.sql(f"""
        select i.item_group grp, i.item_code code, max(i.item_name) name, sum(i.base_net_amount) v
        from `tabPurchase Invoice Item` i join `tabPurchase Invoice` p on p.name = i.parent
        where p.docstatus = 1 and p.posting_date between %(f)s and %(t)s {ctx.co('p')} group by i.item_group, i.item_code""")
    groups = {}
    for r in items:
        groups[r.grp or _("Not set")] = groups.get(r.grp or _("Not set"), 0) + flt(r.v)
    po_status = ctx.sql(f"""select status label, count(*) v from `tabPurchase Order`
        where docstatus = 1 and transaction_date between %(f)s and %(t)s {ctx.co()} group by status""")
    mr_status = ctx.sql(f"""select status label, count(*) v from `tabMaterial Request`
        where docstatus = 1 and transaction_date between %(f)s and %(t)s {ctx.co()} group by status""")
    outstanding = ctx.sql(f"""select sum(outstanding_amount) v from `tabPurchase Invoice`
        where docstatus = 1 and outstanding_amount > 0 and posting_date <= %(t)s {ctx.co()}""")[0].v

    kpis = [
        _kpi("purchases", _("Purchases invoiced"), sum(r["v"] for r in m_pi), monthly=[r["v"] for r in m_pi], invert=True),
        _kpi("po_value", _("Orders placed"), po_tot, monthly=[r["v"] for r in m_po]),
        _kpi("po_count", _("Purchase orders"), po_n, "number", monthly=[r["n"] for r in m_po]),
        _kpi("avg_po", _("Average order"), po_tot / po_n if po_n else 0, monthly=[(r["v"] / r["n"]) if r["n"] else 0 for r in m_po]),
        _kpi("received", _("Quantity received"), sum(r["v"] for r in m_pr), "number", monthly=[r["v"] for r in m_pr]),
        _kpi("paid", _("Paid to suppliers"), sum(r["v"] for r in m_pe), monthly=[r["v"] for r in m_pe]),
        _kpi("tax", _("Input tax"), sum(r["tax"] for r in m_pi), monthly=[r["tax"] for r in m_pi]),
        _kpi("payable", _("Payables"), outstanding, hint=_("Unpaid bills on {0}").format(frappe.format(ctx.t, "Date"))),
    ]
    widgets = [
        _w("trend", _("Ordered vs invoiced vs paid"), "area",
           [{"month": a["month"], "ordered": a["v"], "invoiced": b["v"], "paid": c["v"]} for a, b, c in zip(m_po, m_pi, m_pe)],
           _("Monthly purchase flow"), money=True, span=2,
           series=[{"key": "ordered", "label": _("Ordered")}, {"key": "invoiced", "label": _("Invoiced")}, {"key": "paid", "label": _("Paid")}]),
        _w("groups", _("Spend by item group"), "donut", _pairs([{"label": k, "v": v} for k, v in groups.items()], limit=6), money=True),
        _w("suppliers", _("Top suppliers"), "barlist", _pairs(sup, limit=8, other=False), money=True),
        _w("items", _("Top items by spend"), "bar", [{"item": r.name or r.code, "v": flt(r.v)} for r in sorted(items, key=lambda r: -flt(r.v))[:8]],
           xKey="item", money=True, series=[{"key": "v", "label": _("Spend")}], span=2),
        _w("po_status", _("Purchase order status"), "pie", _pairs(po_status)),
        _w("mr_status", _("Material requests by status"), "bar", [{"status": r["label"], "v": r["value"]} for r in _pairs(mr_status)],
           xKey="status", series=[{"key": "v", "label": _("Requests")}], span=2),
        _w("received", _("Quantity ordered vs received"), "bar",
           [{"month": r["month"], "ordered": m_poq.get(r["month"], 0), "v": r["v"]} for r in m_pr], _("Purchase orders and receipts, stock units"),
           series=[{"key": "ordered", "label": _("Ordered"), "color": "hsl(215 16% 65%)"}, {"key": "v", "label": _("Received"), "color": "hsl(199 89% 48%)"}],
           span=3),
    ]
    return kpis, widgets


# ----------------------------------------------------------------------------- stock
def _stock(ctx):
    # Internal transfers (godown → WIP etc.) move value out of one warehouse and into another; counting them
    # would inflate both sides, so they are left out. Deliveries to customers are kept apart for turnover.
    sle = ctx.sql(f"""
        select date_format(s.posting_date, '%%Y-%%m') m,
               sum(case when s.stock_value_difference > 0 then s.stock_value_difference else 0 end) inward,
               sum(case when s.stock_value_difference < 0 then -s.stock_value_difference else 0 end) outward,
               sum(case when s.stock_value_difference < 0 and s.voucher_type in ('Delivery Note', 'Sales Invoice')
                        then -s.stock_value_difference else 0 end) delivered
        from `tabStock Ledger Entry` s
        left join `tabStock Entry` se on s.voucher_type = 'Stock Entry' and se.name = s.voucher_no
        where s.is_cancelled = 0 and s.posting_date between %(f)s and %(t)s {ctx.co('s')}
          and ifnull(se.purpose, '') not in ('Material Transfer', 'Material Transfer for Manufacture')
        group by m""")
    m_sle = ctx.monthly(sle, fields=("inward", "outward", "delivered"))
    for r in m_sle:
        r["net"] = r["inward"] - r["outward"]
    wh_co = " and w.company = %(co)s" if ctx.company else ""
    by_wh = ctx.sql(f"""select b.warehouse label, sum(b.stock_value) v from `tabBin` b join `tabWarehouse` w on w.name = b.warehouse
        where b.actual_qty != 0 {wh_co} group by b.warehouse""")
    by_grp = ctx.sql(f"""select ifnull(i.item_group, 'Not set') label, sum(b.stock_value) v
        from `tabBin` b join `tabItem` i on i.name = b.item_code join `tabWarehouse` w on w.name = b.warehouse
        where b.actual_qty != 0 {wh_co} group by i.item_group""")
    top_items = ctx.sql(f"""select max(i.item_name) label, sum(b.stock_value) v
        from `tabBin` b join `tabItem` i on i.name = b.item_code join `tabWarehouse` w on w.name = b.warehouse
        where b.actual_qty > 0 {wh_co} group by b.item_code order by v desc limit 8""")
    summary = ctx.sql(f"""select sum(b.stock_value) v, count(distinct case when b.actual_qty > 0 then b.item_code end) n_items,
        count(distinct case when b.actual_qty > 0 then b.warehouse end) n_whs
        from `tabBin` b join `tabWarehouse` w on w.name = b.warehouse where 1 = 1 {wh_co}""")[0]
    se = ctx.sql(f"""select date_format(posting_date, '%%Y-%%m') m, count(*) n,
        sum(case when purpose = 'Manufacture' then fg_completed_qty else 0 end) fg
        from `tabStock Entry` where docstatus = 1 and posting_date between %(f)s and %(t)s {ctx.co()} group by m""")
    m_se = ctx.monthly(se, fields=("n", "fg"))
    se_type = ctx.sql(f"""select stock_entry_type label, count(*) v from `tabStock Entry`
        where docstatus = 1 and posting_date between %(f)s and %(t)s {ctx.co()} group by stock_entry_type""")
    moved = ctx.sql(f"""select count(distinct item_code) n from `tabStock Ledger Entry`
        where is_cancelled = 0 and posting_date between %(f)s and %(t)s {ctx.co()}""")[0].n
    turnover = sum(r["delivered"] for r in m_sle) / flt(summary.v) if flt(summary.v) else 0

    kpis = [
        _kpi("value", _("Stock value"), summary.v, hint=_("Current, all warehouses")),
        _kpi("items", _("Items in stock"), summary.n_items, "number", hint=_("{0} warehouses hold stock").format(summary.n_whs)),
        _kpi("inward", _("Inward value"), sum(r["inward"] for r in m_sle), monthly=[r["inward"] for r in m_sle]),
        _kpi("outward", _("Outward value"), sum(r["outward"] for r in m_sle), monthly=[r["outward"] for r in m_sle]),
        _kpi("entries", _("Stock entries"), sum(r["n"] for r in m_se), "number", monthly=[r["n"] for r in m_se]),
        _kpi("fg", _("Produced (Manufacture)"), sum(r["fg"] for r in m_se), "number", monthly=[r["fg"] for r in m_se]),
        _kpi("moved", _("Items moved"), moved, "number", hint=_("Distinct items with a movement")),
        _kpi("turnover", _("Stock turnover"), turnover, "number", hint=_("Cost of goods delivered ÷ current stock value")),
    ]
    widgets = [
        _w("flow", _("Inward vs outward value"), "combo", [{k: r[k] for k in ("month", "inward", "outward", "net")} for r in m_sle],
           _("Monthly, excluding internal transfers, with net change"), money=True, span=2,
           series=[{"key": "inward", "label": _("Inward")}, {"key": "outward", "label": _("Outward"), "color": "hsl(351 95% 59%)"},
                   {"key": "net", "label": _("Net"), "type": "line", "color": "hsl(160 84% 39%)"}]),
        _w("groups", _("Stock value by item group"), "donut", _pairs(by_grp, limit=6), money=True),
        _w("warehouses", _("Stock value by warehouse"), "bar", [{"wh": r["label"], "v": r["value"]} for r in _pairs(by_wh, limit=8)],
           xKey="wh", money=True, series=[{"key": "v", "label": _("Value")}], span=2),
        _w("top_items", _("Highest-value items"), "barlist", _pairs(top_items, other=False), money=True),
        _w("fg", _("Manufactured output"), "line", [{"month": r["month"], "fg": r["fg"]} for r in m_se], _("Finished quantity from Manufacture entries"),
           series=[{"key": "fg", "label": _("Qty produced"), "color": "hsl(262 83% 58%)"}], span=2),
        _w("types", _("Stock entries by type"), "pie", _pairs(se_type, limit=6)),
    ]
    return kpis, widgets


# ----------------------------------------------------------------------------- hr
def _hr(ctx):
    co = ctx.co()
    active = ctx.sql(f"select count(*) n from `tabEmployee` where status = 'Active' {co}")[0].n
    joins = ctx.sql(f"""select date_format(date_of_joining, '%%Y-%%m') m, count(*) v from `tabEmployee`
        where date_of_joining between %(f)s and %(t)s {co} group by m""")
    leaves = ctx.sql(f"""select date_format(relieving_date, '%%Y-%%m') m, count(*) v from `tabEmployee`
        where relieving_date between %(f)s and %(t)s {co} group by m""")
    m_j, m_l = ctx.monthly(joins), ctx.monthly(leaves)
    by_day = ctx.sql(f"""select attendance_date d, status, count(*) n from `tabAttendance`
        where docstatus = 1 and attendance_date between %(f)s and %(t)s {co} group by attendance_date, status""")
    code = {"Present": "p", "Absent": "a", "On Leave": "l", "Half Day": "h"}
    roll, days_seen = {}, {}
    for r in by_day:
        k = str(r.d)[:7]
        b = roll.setdefault(k, {"m": k, "p": 0, "a": 0, "l": 0, "h": 0, "d": 0})
        if r.status in code:
            b[code[r.status]] += r.n
        days_seen.setdefault(k, set()).add(r.d)
    for k, b in roll.items():
        b["d"] = len(days_seen[k])
    strength = ctx.sql(f"""select date_format(attendance_date, '%%Y-%%m') m, count(distinct employee) v from `tabAttendance`
        where docstatus = 1 and attendance_date between %(f)s and %(t)s {co} group by m""")
    m_str = {r["month"]: r["v"] for r in ctx.monthly(strength)}
    m_att = ctx.monthly(list(roll.values()), fields=("p", "a", "l", "h", "d"))
    rate = [((r["p"] + r["h"] / 2) / (r["p"] + r["a"] + r["l"] + r["h"]) * 100) if (r["p"] + r["a"] + r["l"] + r["h"]) else 0 for r in m_att]
    tp, ta, tl, th = (sum(r[k] for r in m_att) for k in ("p", "a", "l", "h"))
    days = sum(r["d"] for r in m_att)
    dept = ctx.sql(f"select department label, count(*) v from `tabEmployee` where status = 'Active' {co} group by department")
    gender = ctx.sql(f"select gender label, count(*) v from `tabEmployee` where status = 'Active' {co} group by gender")
    etype = ctx.sql(f"select ifnull(employment_type, 'Not set') label, count(*) v from `tabEmployee` where status = 'Active' {co} group by employment_type")
    ages = ctx.sql(f"""select case when age < 20 then '< 20' when age < 30 then '20-29' when age < 40 then '30-39'
            when age < 50 then '40-49' when age < 60 then '50-59' else '60+' end label, count(*) v
        from (select timestampdiff(year, date_of_birth, %(t)s) age from `tabEmployee`
              where status = 'Active' and date_of_birth is not null {co}) x group by label""")
    age_order = ["< 20", "20-29", "30-39", "40-49", "50-59", "60+"]
    age_by = {r.label: r.v for r in ages}
    leave_apps = ctx.sql(f"""select date_format(from_date, '%%Y-%%m') m, count(*) v from `tabLeave Application`
        where docstatus = 1 and from_date between %(f)s and %(t)s {co} group by m""")
    m_la = ctx.monthly(leave_apps)
    joined, left = sum(r["v"] for r in m_j), sum(r["v"] for r in m_l)
    female = sum(flt(r.v) for r in gender if r.label == "Female")

    kpis = [
        _kpi("headcount", _("Active employees"), active, "number"),
        _kpi("joiners", _("Joiners"), joined, "number", monthly=[r["v"] for r in m_j]),
        _kpi("leavers", _("Leavers"), left, "number", monthly=[r["v"] for r in m_l], invert=True),
        _kpi("attrition", _("Attrition"), left / (active + left) * 100 if (active + left) else 0, "percent", invert=True,
             hint=_("Leavers ÷ (active + leavers)")),
        _kpi("attendance", _("Attendance rate"), (tp + th / 2) / (tp + ta + tl + th) * 100 if (tp + ta + tl + th) else 0, "percent", monthly=rate),
        _kpi("present_day", _("Present per day"), tp / days if days else 0, "number", monthly=[(r["p"] / r["d"]) if r["d"] else 0 for r in m_att]),
        _kpi("absent", _("Absences"), ta, "number", monthly=[r["a"] for r in m_att], invert=True),
        _kpi("leave_apps", _("Leave applications"), sum(r["v"] for r in m_la), "number", monthly=[r["v"] for r in m_la]),
        _kpi("female", _("Female staff"), female / active * 100 if active else 0, "percent", hint=_("{0} of {1} active").format(int(female), active)),
    ]
    widgets = [
        _w("att", _("Attendance by month"), "bar", [{"month": r["month"], "present": r["p"], "absent": r["a"], "leave": r["l"]} for r in m_att],
           _("Attendance records by status"), span=2, stacked=True,
           series=[{"key": "present", "label": _("Present"), "color": "hsl(160 84% 39%)"}, {"key": "absent", "label": _("Absent"), "color": "hsl(351 95% 59%)"},
                   {"key": "leave", "label": _("On leave"), "color": "hsl(35 92% 50%)"}]),
        _w("gender", _("Gender mix"), "donut", _pairs(gender)),
        _w("rate", _("Attendance rate trend"), "combo",
           [{"month": r["month"], "strength": m_str.get(r["month"], 0), "rate": round(x, 1)} for r, x in zip(m_att, rate)],
           _("Workforce strength (employees marked, bars) and attendance % (line, half days count ½)"), span=2, dualAxis=True,
           series=[{"key": "strength", "label": _("Strength"), "format": "number", "color": "hsl(215 16% 65%)"},
                   {"key": "rate", "label": _("Attendance %"), "type": "line", "axis": "right", "color": "hsl(160 84% 39%)"}]),
        _w("etype", _("Employment type"), "pie", _pairs(etype)),
        _w("dept", _("Headcount by department"), "barlist", _pairs(dept, limit=10, other=False)),
        _w("ages", _("Age profile"), "bar", [{"band": b, "v": flt(age_by.get(b))} for b in age_order], xKey="band",
           series=[{"key": "v", "label": _("Employees"), "color": "hsl(262 83% 58%)"}]),
        _w("turnover", _("Joiners vs leavers"), "bar", [{"month": a["month"], "joined": a["v"], "left": b["v"]} for a, b in zip(m_j, m_l)],
           series=[{"key": "joined", "label": _("Joined")}, {"key": "left", "label": _("Left"), "color": "hsl(351 95% 59%)"}]),
    ]
    return kpis, widgets


# ----------------------------------------------------------------------------- payroll
def _payroll(ctx):
    co = ctx.co()
    slips = ctx.sql(f"""select date_format(start_date, '%%Y-%%m') m, sum(base_gross_pay) g, sum(base_net_pay) n,
            sum(base_total_deduction) d, count(*) c, count(distinct employee) e
        from `tabSalary Slip` where docstatus = 1 and start_date between %(f)s and %(t)s {co} group by m""")
    m = ctx.monthly(slips, fields=("g", "n", "d", "c", "e"))
    g, n, d, c = (sum(r[k] for r in m) for k in ("g", "n", "d", "c"))
    emps = ctx.sql(f"""select count(distinct employee) e from `tabSalary Slip`
        where docstatus = 1 and start_date between %(f)s and %(t)s {co}""")[0].e
    dept = ctx.sql(f"""select ifnull(department, 'Not set') label, sum(base_gross_pay) v from `tabSalary Slip`
        where docstatus = 1 and start_date between %(f)s and %(t)s {co} group by department""")
    comp = ctx.sql(f"""select d.parentfield pf, d.salary_component label, sum(d.amount) v
        from `tabSalary Detail` d join `tabSalary Slip` s on s.name = d.parent
        where s.docstatus = 1 and s.start_date between %(f)s and %(t)s {ctx.co('s')} and d.parenttype = 'Salary Slip'
        group by d.parentfield, d.salary_component""")
    bands = ctx.sql(f"""select case when base_net_pay < 15000 then '< 15k' when base_net_pay < 25000 then '15-25k'
            when base_net_pay < 35000 then '25-35k' when base_net_pay < 50000 then '35-50k' else '50k+' end label, count(*) v
        from `tabSalary Slip` where docstatus = 1 and start_date between %(f)s and %(t)s {co} group by label""")
    band_by = {r.label: r.v for r in bands}
    runs = ctx.sql(f"""select count(*) n from `tabPayroll Entry` where docstatus = 1 and posting_date between %(f)s and %(t)s {co}""")[0].n
    slab_rows, tax_total, slab_name = _payroll_tax_slabs(ctx)
    ded_total = sum(flt(r.v) for r in comp if r.pf == "deductions")
    comp_rows = [{"component": r.label, "type": _("Earning") if r.pf == "earnings" else _("Deduction"), "amount": round(flt(r.v), 2),
                  "share": round(flt(r.v) / (g if r.pf == "earnings" else ded_total) * 100, 1) if (g if r.pf == "earnings" else ded_total) else 0,
                  "per_slip": round(flt(r.v) / c, 2) if c else 0}
                 for r in sorted(comp, key=lambda r: (r.pf != "earnings", -flt(r.v)))]

    kpis = [
        _kpi("gross", _("Gross pay"), g, monthly=[r["g"] for r in m]),
        _kpi("net", _("Net pay"), n, monthly=[r["n"] for r in m]),
        _kpi("deductions", _("Deductions"), d, monthly=[r["d"] for r in m]),
        _kpi("ded_ratio", _("Deduction ratio"), d / g * 100 if g else 0, "percent", monthly=[(r["d"] / r["g"] * 100) if r["g"] else 0 for r in m]),
        _kpi("employees", _("Employees paid"), emps, "number", monthly=[r["e"] for r in m]),
        _kpi("avg_gross", _("Average gross / slip"), g / c if c else 0, monthly=[(r["g"] / r["c"]) if r["c"] else 0 for r in m]),
        _kpi("slips", _("Salary slips"), c, "number", monthly=[r["c"] for r in m], hint=_("{0} payroll runs").format(runs)),
        _kpi("income_tax", _("Income tax deducted"), tax_total, invert=True,
             hint=_("Slab: {0}").format(slab_name) if slab_name else _("No income tax slab assigned")),
    ]
    widgets = [
        _w("trend", _("Payroll cost by month"), "combo", [{"month": r["month"], "gross": r["g"], "net": r["n"], "emp": r["e"]} for r in m],
           _("Gross and net pay, with employees paid"), money=True, span=2, dualAxis=True,
           series=[{"key": "gross", "label": _("Gross")}, {"key": "net", "label": _("Net"), "color": "hsl(160 84% 39%)"},
                   {"key": "emp", "label": _("Employees"), "format": "number", "type": "line", "axis": "right", "color": "hsl(35 92% 50%)"}]),
        _w("earnings", _("Earnings mix"), "donut", _pairs([r for r in comp if r.pf == "earnings"], limit=6), money=True),
        _w("dept", _("Gross pay by department"), "barlist", _pairs(dept, limit=10, other=False), money=True),
        _w("bands", _("Net pay distribution"), "bar", [{"band": b, "v": flt(band_by.get(b))} for b in ["< 15k", "15-25k", "25-35k", "35-50k", "50k+"]],
           _("Salary slips by net pay"), xKey="band", series=[{"key": "v", "label": _("Slips"), "color": "hsl(199 89% 48%)"}]),
        _w("deductions", _("Deductions mix"), "pie", _pairs([r for r in comp if r.pf == "deductions"], limit=6), money=True),
        _w("avg", _("Average gross per slip"), "area", [{"month": r["month"], "avg": round(r["g"] / r["c"], 2) if r["c"] else 0} for r in m],
           money=True, series=[{"key": "avg", "label": _("Average gross")}], span=2),
        _w("components", _("Earnings & deductions"), "table", comp_rows, _("Every salary component paid or deducted in the period"), span=2, xKey="",
           columns=[{"key": "component", "label": _("Component")}, {"key": "type", "label": _("Type")},
                    {"key": "amount", "label": _("Amount"), "format": "money", "align": "right"},
                    {"key": "share", "label": _("% of gross / deductions"), "format": "percent", "align": "right"},
                    {"key": "per_slip", "label": _("Per slip"), "format": "money", "align": "right"}]),
        _w("tax_slabs", _("Income tax slabs"), "table", slab_rows,
           _("{0}: annual taxable salary bands, employees in each (annualised from the period) and tax deducted").format(slab_name)
           if slab_name else _("No Income Tax Slab is assigned to this company's employees"), span=3, xKey="",
           columns=[{"key": "band", "label": _("Annual taxable income")}, {"key": "rate", "label": _("Rate on excess"), "format": "percent", "align": "right"},
                    {"key": "fixed", "label": _("Tax at band start"), "format": "money", "align": "right"},
                    {"key": "employees", "label": _("Employees"), "format": "number", "align": "right"},
                    {"key": "tax", "label": _("Tax deducted"), "format": "money", "align": "right"}]),
    ]
    return kpis, widgets


def _payroll_tax_slabs(ctx):
    """The Income Tax Slab on this company's salary structure assignments, each band with the tax due at its start,
    the employees whose annualised taxable pay falls in it and the income tax deducted from them in the period."""
    if not frappe.db.exists("DocType", "Income Tax Slab"):
        return [], 0.0, None
    slab = ctx.sql(f"""select income_tax_slab s, count(*) n from `tabSalary Structure Assignment`
        where docstatus = 1 and ifnull(income_tax_slab, '') != '' {ctx.co()} group by income_tax_slab order by n desc limit 1""")
    tax_comp = set(frappe.get_all("Salary Component", {"variable_based_on_taxable_salary": 1}, pluck="name"))
    per_emp = ctx.sql(f"""select s.employee, count(distinct s.name) slips,
            sum(case when d.parentfield = 'earnings' and ifnull(c.is_tax_applicable, 1) = 1 then d.amount else 0 end) taxable,
            sum(case when d.parentfield = 'deductions' and d.salary_component in %(tc)s then d.amount else 0 end) tax
        from `tabSalary Slip` s join `tabSalary Detail` d on d.parent = s.name and d.parenttype = 'Salary Slip'
        left join `tabSalary Component` c on c.name = d.salary_component
        where s.docstatus = 1 and s.start_date between %(f)s and %(t)s {ctx.co('s')} group by s.employee""",
                      extra={"tc": tuple(tax_comp) or ("",)})
    tax_total = sum(flt(r.tax) for r in per_emp)
    if not slab:
        return [], tax_total, None
    bands = frappe.get_all("Taxable Salary Slab", {"parent": slab[0].s, "parenttype": "Income Tax Slab"},
                           ["from_amount", "to_amount", "percent_deduction"], order_by="from_amount")
    rows, fixed = [], 0.0
    for b in bands:
        lo, hi = flt(b.from_amount), flt(b.to_amount)
        inside = [r for r in per_emp if r.slips and lo <= flt(r.taxable) / r.slips * 12 < (hi or float("inf"))]
        rows.append({"band": f"{_money_short(lo)} – {_money_short(hi)}" if hi else _("Above {0}").format(_money_short(lo)),
                     "rate": flt(b.percent_deduction), "fixed": round(fixed, 2), "employees": len(inside),
                     "tax": round(sum(flt(r.tax) for r in inside), 2)})
        if hi:
            fixed += (hi - lo) * flt(b.percent_deduction) / 100
    return rows, tax_total, slab[0].s


def _money_short(v):
    v = flt(v)
    return f"{v / 1e6:g}M" if v >= 1e6 else f"{v / 1e3:g}k" if v >= 1e3 else f"{v:g}"


# ----------------------------------------------------------------------------- production
def _production(ctx):
    co = ctx.co()
    # Yield is weighted by output and only uses orders whose recorded yield is physically possible (1–100 %);
    # a few hundred migrated orders carry values above 100 % and would inflate the average.
    wo = ctx.sql(f"""select date_format(work_order_date, '%%Y-%%m') m, count(*) n, sum(qty) planned, sum(produced_qty) produced,
            sum(actual_waste) waste, sum(material_issued) issued,
            sum(case when actual_yield between 1 and 100 then actual_yield * produced_qty end) yw,
            sum(case when actual_yield between 1 and 100 then produced_qty end) yq,
            sum(case when target_yield > 0 then target_yield * qty end) tyw, sum(case when target_yield > 0 then qty end) tyq,
            sum(case when actual_ops > 0 then actual_ops * spindle_worked end) ow, sum(case when actual_ops > 0 then spindle_worked end) oq,
            sum(case when target_ops > 0 then target_ops * spindle_required end) tow, sum(case when target_ops > 0 then spindle_required end) toq,
            sum(spindle_worked) spindles
        from `tabWork Order` where docstatus = 1 and work_order_date between %(f)s and %(t)s {co} group by m""")
    m = ctx.monthly(wo, fields=("n", "planned", "produced", "waste", "issued", "yw", "yq", "tyw", "tyq", "ow", "oq", "tow", "toq", "spindles"))
    for r in m:
        r["achv"] = round(r["produced"] / r["planned"] * 100, 1) if r["planned"] else 0
        r["yield"] = round(r["yw"] / r["yq"], 2) if r["yq"] else 0
        r["target"] = round(r["tyw"] / r["tyq"], 2) if r["tyq"] else 0
        r["ops"] = round(r["ow"] / r["oq"], 2) if r["oq"] else 0
        r["tops"] = round(r["tow"] / r["toq"], 2) if r["toq"] else 0
        r["waste_pct"] = round(r["waste"] / (r["produced"] + r["waste"]) * 100, 2) if (r["produced"] + r["waste"]) else 0
    tot = {k: sum(r[k] for r in m) for k in ("n", "planned", "produced", "waste", "yw", "yq", "tyw", "tyq", "ow", "oq", "tow", "toq")}
    over100 = ctx.sql(f"""select count(*) n from `tabWork Order` where docstatus = 1 and actual_yield > 100
        and work_order_date between %(f)s and %(t)s {co}""")[0].n
    status = ctx.sql(f"""select status label, count(*) v from `tabWork Order`
        where docstatus = 1 and work_order_date between %(f)s and %(t)s {co} group by status""")
    items = ctx.sql(f"""select max(item_name) label, sum(produced_qty) v from `tabWork Order`
        where docstatus = 1 and work_order_date between %(f)s and %(t)s {co} group by production_item order by v desc limit 8""")
    dt_co = " and w.company = %(co)s" if ctx.company else ""
    dt_base = f"""from `tabDowntime Entry` d left join `tabWork Order` w on w.name = d.work_order
        where d.docstatus < 2 and date(d.from_time) between %(f)s and %(t)s {dt_co}"""
    dt_month = ctx.sql(f"select date_format(d.from_time, '%%Y-%%m') m, sum(d.downtime) / 60 v, count(*) n {dt_base} group by m")
    m_dt = ctx.monthly(dt_month, fields=("v", "n"))
    dt_reason = ctx.sql(f"select ifnull(d.stop_reason, 'Not set') label, sum(d.downtime) / 60 v {dt_base} group by d.stop_reason")
    dt_ws = ctx.sql(f"select ifnull(d.workstation, 'Not set') label, sum(d.downtime) / 60 v {dt_base} group by d.workstation")
    # Cost per spindle: net expense on the accounts flagged "CPS Applicable" ÷ spindles required by the month's work orders.
    cps_cost = ctx.sql(f"""select date_format(g.posting_date, '%%Y-%%m') m, sum(g.debit - g.credit) v
        from `tabGL Entry` g join `tabAccount` a on a.name = g.account and a.cps_applicable = 1
        where g.is_cancelled = 0 and g.voucher_type != 'Period Closing Voucher' and g.posting_date between %(f)s and %(t)s {ctx.co('g')} group by m""")
    cps_sp = ctx.sql(f"""select date_format(work_order_date, '%%Y-%%m') m, sum(spindle_required) s from `tabWork Order`
        where docstatus = 1 and work_order_date between %(f)s and %(t)s {co} group by m""")
    m_cps = ctx.monthly(cps_cost, fields=("v",))
    m_sp = {r["month"]: r["s"] for r in ctx.monthly(cps_sp, fields=("s",))}
    for r in m_cps:
        r["cps"] = round(r["v"] / m_sp[r["month"]], 2) if m_sp.get(r["month"]) else 0
    cps_total, sp_total = sum(r["v"] for r in m_cps), sum(m_sp.values())
    fleet = ctx.sql(f"""select sum(spindles) installed, sum(if(status = 'Production', spindles, 0)) active from `tabWorkstation`
        where ifnull(disabled, 0) = 0""")[0] if frappe.db.has_column("Workstation", "spindles") else frappe._dict(installed=0, active=0)

    # The unit most of the period's output is made in (a spinning mill: Kg), shown on quantity tiles and charts.
    uom_row = ctx.sql(f"""select stock_uom u, sum(qty) q from `tabWork Order` where docstatus = 1
        and work_order_date between %(f)s and %(t)s {co} group by stock_uom order by q desc limit 1""")
    uom = f" ({uom_row[0].u})" if uom_row and uom_row[0].u else ""
    kpis = [
        _kpi("planned", _("Planned quantity") + uom, tot["planned"], "number", monthly=[r["planned"] for r in m]),
        _kpi("produced", _("Achieved quantity") + uom, tot["produced"], "number", monthly=[r["produced"] for r in m]),
        _kpi("achievement", _("Plan achievement"), tot["produced"] / tot["planned"] * 100 if tot["planned"] else 0, "percent",
             monthly=[r["achv"] for r in m]),
        _kpi("yield", _("Actual yield"), tot["yw"] / tot["yq"] if tot["yq"] else 0, "percent", monthly=[r["yield"] for r in m],
             hint=_("Target {0}% · {1} orders >100% excluded").format(round(tot["tyw"] / tot["tyq"], 1) if tot["tyq"] else 0, over100)),
        _kpi("waste", _("Waste") + uom, tot["waste"], "number", monthly=[r["waste"] for r in m], invert=True),
        _kpi("ops", _("Average OPS"), tot["ow"] / tot["oq"] if tot["oq"] else 0, "number", monthly=[r["ops"] for r in m],
             hint=_("Target {0} · weighted by spindles").format(round(tot["tow"] / tot["toq"], 2) if tot["toq"] else 0)),
        _kpi("orders", _("Work orders"), tot["n"], "number", monthly=[r["n"] for r in m]),
        _kpi("downtime", _("Downtime hours"), sum(r["v"] for r in m_dt), "number", monthly=[r["v"] for r in m_dt], invert=True,
             hint=_("{0} stoppages logged").format(int(sum(r["n"] for r in m_dt)))),
        _kpi("cps", _("Cost per spindle"), cps_total / sp_total if sp_total else 0, "money", monthly=[r["cps"] for r in m_cps], invert=True,
             hint=_("{0} installed · {1} running spindles").format(int(flt(fleet.installed)), int(flt(fleet.active)))),
    ]
    widgets = [
        _w("output", _("Planned vs produced"), "combo", [{"month": r["month"], "planned": r["planned"], "produced": r["produced"], "achv": r["achv"]} for r in m],
           _("Quantity{0} by month, with plan achievement %").format(uom), span=2, dualAxis=True,
           series=[{"key": "planned", "label": _("Planned") + uom, "color": "hsl(215 16% 65%)"}, {"key": "produced", "label": _("Achieved") + uom},
                   {"key": "achv", "label": _("Achievement %"), "type": "line", "axis": "right", "color": "hsl(35 92% 50%)"}]),
        _w("status", _("Work order status"), "donut", _pairs(status)),
        _w("cps", _("Cost and cost per spindle"), "combo", [{"month": r["month"], "cost": round(r["v"], 2), "cps": r["cps"]} for r in m_cps],
           _("CPS-applicable expenses (bars) and cost per required spindle (line)"), money=True, span=3, dualAxis=True,
           series=[{"key": "cost", "label": _("CPS cost"), "color": "hsl(215 16% 65%)"},
                   {"key": "cps", "label": _("Cost per spindle"), "type": "line", "axis": "right", "color": "hsl(351 95% 59%)"}]),
        _w("yield", _("Yield: target vs actual"), "line", [{"month": r["month"], "target": r["target"], "actual": r["yield"]} for r in m],
           _("Output-weighted, % (axis fitted to the data; target dashed)"), percent=True, span=2, zoom=True,
           series=[{"key": "target", "label": _("Target"), "color": "hsl(35 92% 50%)", "dashed": True},
                   {"key": "actual", "label": _("Actual"), "color": "hsl(160 84% 39%)"}]),
        _w("items", _("Top products"), "barlist", _pairs(items, other=False), _("Produced quantity")),
        _w("waste", _("Waste by month"), "combo", [{"month": r["month"], "waste": r["waste"], "pct": r["waste_pct"]} for r in m],
           _("Quantity{0}, with waste as % of output + waste").format(uom), span=2, dualAxis=True,
           series=[{"key": "waste", "label": _("Waste") + uom, "color": "hsl(351 95% 59%)"},
                   {"key": "pct", "label": _("Waste %"), "type": "line", "axis": "right", "color": "hsl(262 83% 58%)"}]),
        _w("reasons", _("Downtime by reason"), "pie", _pairs(dt_reason, limit=6), _("Hours")),
        _w("ops", _("OPS: target vs actual"), "line", [{"month": r["month"], "target": r["tops"], "actual": r["ops"]} for r in m],
           _("Output per spindle per shift"), span=2,
           series=[{"key": "target", "label": _("Target"), "color": "hsl(215 16% 65%)"}, {"key": "actual", "label": _("Actual")}]),
        _w("machines", _("Downtime by workstation"), "barlist", _pairs(dt_ws, limit=8, other=False), _("Hours")),
        _w("dt_trend", _("Downtime by month"), "bar", [{"month": r["month"], "v": round(r["v"], 1)} for r in m_dt], _("Hours of stoppage"),
           series=[{"key": "v", "label": _("Hours"), "color": "hsl(351 95% 59%)"}], span=3),
    ]
    return kpis, widgets


# ----------------------------------------------------------------------------- assets
def _assets(ctx):
    co = ctx.co("a")
    # Book values live on the finance book rows (the asset header's own value_after_depreciation is not kept up to date).
    base = f"""from `tabAsset` a left join `tabAsset Finance Book` fb on fb.parent = a.name and fb.parenttype = 'Asset'
        where a.docstatus = 1 and a.purchase_date <= %(t)s {co}"""
    tot = ctx.sql(f"select count(distinct a.name) n, sum(a.net_purchase_amount) gross, sum(fb.value_after_depreciation) nbv {base}")[0]
    by_cat = ctx.sql(f"select a.asset_category label, sum(a.net_purchase_amount) gross, sum(fb.value_after_depreciation) nbv {base} group by a.asset_category")
    by_loc = ctx.sql(f"select ifnull(a.location, 'Not set') label, sum(fb.value_after_depreciation) v {base} group by a.location")
    status = ctx.sql(f"select a.status label, count(*) v from `tabAsset` a where a.docstatus < 2 {co} group by a.status")
    drafts = ctx.sql(f"select count(*) n, sum(a.net_purchase_amount) v from `tabAsset` a where a.docstatus = 0 {co}")[0]
    dep = ctx.sql(f"""select date_format(d.schedule_date, '%%Y-%%m') m, sum(d.depreciation_amount) v,
            sum(case when d.journal_entry is not null then d.depreciation_amount else 0 end) posted
        from `tabDepreciation Schedule` d join `tabAsset Depreciation Schedule` s on s.name = d.parent
        join `tabAsset` a on a.name = s.asset
        where s.docstatus = 1 and d.schedule_date between %(f)s and %(t)s {co} group by m""")
    m_dep = ctx.monthly(dep, fields=("v", "posted"))
    adds = ctx.sql(f"""select date_format(a.purchase_date, '%%Y-%%m') m, sum(a.net_purchase_amount) v, count(*) n from `tabAsset` a
        where a.docstatus = 1 and a.purchase_date between %(f)s and %(t)s {co} group by m""")
    m_add = ctx.monthly(adds, fields=("v", "n"))
    moves = ctx.sql(f"""select date_format(transaction_date, '%%Y-%%m') m, count(*) v from `tabAsset Movement`
        where docstatus = 1 and date(transaction_date) between %(f)s and %(t)s {ctx.co()} group by m""")
    m_mv = ctx.monthly(moves)
    gross, nbv = flt(tot.gross), flt(tot.nbv)
    dep_total, dep_posted = sum(r["v"] for r in m_dep), sum(r["posted"] for r in m_dep)
    down = sum(flt(r.v) for r in status if r.label in ("Out of Order", "In Maintenance"))

    kpis = [
        _kpi("gross", _("Gross asset value"), gross, hint=_("{0} capitalised assets").format(tot.n)),
        _kpi("nbv", _("Net book value"), nbv, hint=_("{0} of cost remaining").format(f"{nbv / gross * 100:.1f}%" if gross else "0%")),
        _kpi("accumulated", _("Accumulated depreciation"), gross - nbv, invert=True),
        _kpi("depreciation", _("Depreciation in period"), dep_total, monthly=[r["v"] for r in m_dep], invert=True),
        _kpi("posted", _("Depreciation posted"), dep_posted / dep_total * 100 if dep_total else 0, "percent",
             monthly=[(r["posted"] / r["v"] * 100) if r["v"] else 0 for r in m_dep], hint=_("Scheduled entries booked to the ledger")),
        _kpi("additions", _("Additions"), sum(r["v"] for r in m_add), monthly=[r["v"] for r in m_add],
             hint=_("{0} assets bought in the period").format(int(sum(r["n"] for r in m_add)))),
        _kpi("drafts", _("Not yet capitalised"), drafts.n, "number", invert=True, hint=_("Draft assets worth {0}").format(_money(drafts.v))),
        _kpi("down", _("Out of order / maintenance"), down, "number", invert=True),
    ]
    widgets = [
        _w("dep", _("Depreciation by month"), "combo", [{"month": r["month"], "scheduled": r["v"], "posted": r["posted"]} for r in m_dep],
           _("Scheduled vs booked to the ledger"), money=True, span=2,
           series=[{"key": "scheduled", "label": _("Scheduled"), "color": "hsl(215 16% 65%)"},
                   {"key": "posted", "label": _("Posted"), "type": "line", "color": "hsl(262 83% 58%)"}]),
        _w("cat", _("Net book value by category"), "donut", _pairs(by_cat, value="nbv", limit=6), money=True),
        _w("cat_bar", _("Cost vs book value by category"), "bar",
           [{"cat": r.label, "gross": flt(r.gross), "nbv": flt(r.nbv)} for r in sorted(by_cat, key=lambda r: -flt(r.gross))],
           _("How much of each category's cost is left"), xKey="cat", money=True, span=2,
           series=[{"key": "gross", "label": _("Cost")}, {"key": "nbv", "label": _("Book value"), "color": "hsl(160 84% 39%)"}]),
        _w("status", _("Assets by status"), "pie", _pairs(status)),
        _w("loc", _("Book value by location"), "barlist", _pairs(by_loc, other=False), money=True),
        _w("adds", _("Additions by month"), "bar", [{"month": r["month"], "v": r["v"]} for r in m_add], _("Cost of assets bought"),
           money=True, series=[{"key": "v", "label": _("Additions"), "color": "hsl(199 89% 48%)"}]),
        _w("moves", _("Asset movements"), "bar", [{"month": r["month"], "v": r["v"]} for r in m_mv], _("Receipts and transfers"),
           series=[{"key": "v", "label": _("Movements"), "color": "hsl(35 92% 50%)"}]),
    ]
    return kpis, widgets


# ----------------------------------------------------------------------------- financial statements
# Level-2 expense groups that are cost of sales. Word-bounded, so "Indirect Expenses" is not caught by "direct expense".
COST_OF_SALES = re.compile(r"\b(cost of sales?|cost of goods|direct expenses?|manufacturing)\b", re.I)


def _account_groups(ctx):
    """account name → (root_type, second-level group label) using the nested-set tree (lft/rgt)."""
    co = " and company = %(co)s" if ctx.company else ""
    accts = ctx.sql(f"select name, account_name, root_type, lft, rgt, is_group, ifnull(parent_account, '') parent from `tabAccount` where 1 = 1 {co}")
    roots = {a.name for a in accts if not a.parent}
    level2 = [a for a in accts if a.parent in roots]
    out = {}
    for a in accts:
        grp = next((g for g in level2 if g.lft <= a.lft and g.rgt >= a.rgt), None)
        label = (grp.account_name if grp else a.account_name) or a.name
        out[a.name] = (a.root_type, label)
    return out


def _financials(ctx):
    groups = _account_groups(ctx)
    co = " and company = %(co)s" if ctx.company else ""
    cash_accts = {a.name for a in ctx.sql(f"select name from `tabAccount` where account_type in ('Bank', 'Cash') {co}")}
    # ONE pass over the ledger up to the period end: per account, per month inside the period (earlier postings
    # collapse into 'open'), split by whether the row is the year-end Period Closing Voucher. Profit & loss,
    # balance sheet and cash flow are all rolled up from it.
    rows = ctx.sql(f"""select g.account,
            case when g.posting_date >= %(f)s then date_format(g.posting_date, '%%Y-%%m') else 'open' end m,
            g.voucher_type = 'Period Closing Voucher' pcv, sum(g.debit) dr, sum(g.credit) cr
        from `tabGL Entry` g where g.is_cancelled = 0 and g.posting_date <= %(t)s {ctx.co('g')}
        group by g.account, m, pcv""")

    months = {k: {"income": 0.0, "cogs": 0.0, "opex": 0.0, "cin": 0.0, "cout": 0.0} for k, _l in ctx.months}
    lines, side, unclosed, cash_open = {}, {"Asset": {}, "Liability": {}, "Equity": {}}, 0.0, 0.0
    for r in rows:
        bal = flt(r.dr) - flt(r.cr)
        rt, grp = groups.get(r.account, (None, r.account))
        # balance sheet (everything up to the period end)
        if rt in ("Income", "Expense"):
            unclosed -= bal
        elif rt == "Asset":
            side["Asset"][grp] = side["Asset"].get(grp, 0) + bal
        elif rt in ("Liability", "Equity"):
            side[rt][grp] = side[rt].get(grp, 0) - bal
        # cash flow
        if r.account in cash_accts:
            if r.m == "open":
                cash_open += bal
            elif r.m in months:
                months[r.m]["cin"] += flt(r.dr)
                months[r.m]["cout"] += flt(r.cr)
        # profit & loss (inside the period, closing entries excluded)
        if rt in ("Income", "Expense") and r.m in months and not r.pcv:
            amt = -bal if rt == "Income" else bal
            low = grp.lower()
            bucket = "income" if rt == "Income" else ("cogs" if COST_OF_SALES.search(low) else "opex")
            months[r.m][bucket] += amt
            lines[(bucket, grp)] = lines.get((bucket, grp), 0) + amt
    if abs(unclosed) > 0.5:
        side["Equity"][_("Current period profit")] = unclosed

    monthly = []
    for k, lbl in ctx.months:
        b = months[k]
        gp = b["income"] - b["cogs"]
        monthly.append({"month": lbl, "revenue": b["income"], "gross": gp, "net": gp - b["opex"],
                        "gm": round(gp / b["income"] * 100, 2) if b["income"] else 0})
    rev, cogs, opex = (sum(months[k][x] for k in months) for x in ("income", "cogs", "opex"))
    gross, net = rev - cogs, rev - cogs - opex
    tot = {k: sum(v.values()) for k, v in side.items()}
    ca = sum(v for k, v in side["Asset"].items() if "current" in k.lower() and "non" not in k.lower())
    cl = sum(v for k, v in side["Liability"].items() if "current" in k.lower() and "non" not in k.lower())

    m_cash, bal = [], cash_open
    for k, lbl in ctx.months:
        b = months[k]
        bal += b["cin"] - b["cout"]
        m_cash.append({"month": lbl, "cin": b["cin"], "cout": b["cout"], "net": b["cin"] - b["cout"], "closing": bal})
    net_cash = sum(r["net"] for r in m_cash)

    kpis = [
        _kpi("revenue", _("Revenue"), rev, monthly=[r["revenue"] for r in monthly]),
        _kpi("gross_profit", _("Gross profit"), gross, monthly=[r["gross"] for r in monthly],
             hint=_("Gross margin {0}").format(_pct(gross / rev * 100 if rev else 0))),
        _kpi("net_profit", _("Net profit"), net, monthly=[r["net"] for r in monthly],
             hint=_("Net margin {0}").format(_pct(net / rev * 100 if rev else 0))),
        _kpi("net_cash", _("Net cash flow"), net_cash, monthly=[r["net"] for r in m_cash],
             hint=_("Closing cash & bank {0}").format(_money(bal))),
        _kpi("total_assets", _("Total assets"), tot["Asset"], hint=_("As on {0}").format(frappe.format(ctx.t, "Date"))),
        _kpi("total_liabilities", _("Total liabilities"), tot["Liability"], invert=True),
        _kpi("equity", _("Equity"), tot["Equity"], hint=_("Assets − liabilities {0}").format(_money(tot["Asset"] - tot["Liability"]))),
        _kpi("current_ratio", _("Current ratio"), ca / cl if cl else 0, "number", hint=_("Current assets ÷ current liabilities")),
    ]
    stmt = [
        {"line": _("Revenue"), "v": rev, "color": "hsl(221 83% 53%)"},
        {"line": _("Cost of sales"), "v": -cogs, "color": "hsl(351 95% 59%)"},
        {"line": _("Gross profit"), "v": gross, "color": "hsl(160 84% 39%)"},
        {"line": _("Operating expenses"), "v": -opex, "color": "hsl(35 92% 50%)"},
        {"line": _("Net profit"), "v": net, "color": "hsl(160 84% 39%)" if net >= 0 else "hsl(351 95% 59%)"},
    ]
    widgets = [
        _w("stmt", _("Income statement"), "bar", stmt, _("Revenue down to net profit for the period"), xKey="line", money=True,
           series=[{"key": "v", "label": _("Amount")}], colorKey="color", span=2),
        _w("bs_check", _("Balance sheet"), "bar",
           [{"side": _("Assets"), "v": tot["Asset"], "color": "hsl(221 83% 53%)"},
            {"side": _("Liabilities"), "v": tot["Liability"], "color": "hsl(351 95% 59%)"},
            {"side": _("Equity"), "v": tot["Equity"], "color": "hsl(160 84% 39%)"}],
           _("As on {0}").format(frappe.format(ctx.t, "Date")), xKey="side", money=True, series=[{"key": "v", "label": _("Amount")}], colorKey="color"),
        _w("margins", _("Profit by month"), "combo", monthly, _("Revenue, gross profit and net profit"), money=True, span=2,
           series=[{"key": "revenue", "label": _("Revenue"), "color": "hsl(215 16% 65%)"}, {"key": "gross", "label": _("Gross profit")},
                   {"key": "net", "label": _("Net profit"), "type": "line", "color": "hsl(160 84% 39%)"}]),
        _w("gm", _("Gross margin"), "line", [{"month": r["month"], "gm": r["gm"]} for r in monthly], _("% of revenue"), percent=True,
           series=[{"key": "gm", "label": _("Gross margin %")}]),
        _w("assets_mix", _("What the company owns"), "donut", _pairs([{"label": k, "v": v} for k, v in side["Asset"].items()], limit=6), money=True),
        _w("funding", _("How it is funded"), "donut",
           _pairs([{"label": k, "v": v} for k, v in {**side["Liability"], **side["Equity"]}.items()], limit=7), money=True),
        _w("opex", _("Expenses by group"), "barlist",
           _pairs([{"label": g, "v": v} for (b, g), v in lines.items() if b in ("cogs", "opex")], other=False), money=True),
        _w("cash", _("Cash flow"), "combo", m_cash, _("Money in and out of bank & cash, with closing balance"), money=True, span=3, dualAxis=True,
           series=[{"key": "cin", "label": _("Cash in"), "color": "hsl(160 84% 39%)"}, {"key": "cout", "label": _("Cash out"), "color": "hsl(351 95% 59%)"},
                   {"key": "closing", "label": _("Closing balance"), "type": "line", "axis": "right", "color": "hsl(221 83% 53%)"}]),
    ]
    return kpis, widgets


# ----------------------------------------------------------------------------- buying cycle / procurement
def _procurement(ctx):
    """Request → order → receipt → invoice timing, PO coverage, vendor rate variation and purchase price variance.

    Every measure is per PO line (PO date in the period). Stage days:
      request → order  = PO date − Material Request date
      order → receipt  = first receipt against the PO line − PO date
      receipt → invoice = first invoice against the PO line − first receipt
    Negative stage durations (documents dated out of order) are left out of the averages and counted instead.

    Standard rate (for PPV and the tolerance check), per stock unit: the item's "Standard Buying" price when it
    has one within 0.5×–2× of what is actually paid (older or other-unit prices are ignored), otherwise its
    quantity-weighted average purchase rate over the period.
    Lines more than ±200 % off standard are almost always unit or price-entry errors; they are counted as
    "Suspect" and kept out of PPV and the vendor-variation ranking.
      PPV       = (actual rate − standard rate) × stock qty        (positive = paid more than standard)
      PPV rate  = PPV ÷ (standard rate × stock qty)
    """
    tol = flt(getattr(ctx, "tolerance", 5.0))
    lines = ctx.sql(f"""
        select poi.name, poi.item_code, max(poi.item_name) item_name, poi.item_group, po.supplier,
               po.transaction_date po_d, ifnull(poi.schedule_date, po.schedule_date) req_d, mr.transaction_date mr_d,
               rc.d pr_d, iv.d pi_d, poi.stock_qty sq, poi.base_amount amt, poi.qty q, poi.received_qty rq,
               poi.amount a, poi.billed_amt b,
               mr.name mr_n, mr.owner mr_o, po.name po_n, po.owner po_o, rc.n pr_n, rc.o pr_o, iv.n pi_n, iv.o pi_o
        from `tabPurchase Order Item` poi
        join `tabPurchase Order` po on po.name = poi.parent
        left join `tabMaterial Request` mr on mr.name = poi.material_request and mr.docstatus = 1
        left join (select pri.purchase_order_item k, min(pr.posting_date) d,
                          substring_index(group_concat(pr.name order by pr.posting_date, pr.name), ',', 1) n,
                          substring_index(group_concat(pr.owner order by pr.posting_date, pr.name), ',', 1) o
                   from `tabPurchase Receipt Item` pri join `tabPurchase Receipt` pr on pr.name = pri.parent
                   where pr.docstatus = 1 group by pri.purchase_order_item) rc
               on rc.k = poi.name
        left join (select pii.po_detail k, min(pi.posting_date) d,
                          substring_index(group_concat(pi.name order by pi.posting_date, pi.name), ',', 1) n,
                          substring_index(group_concat(pi.owner order by pi.posting_date, pi.name), ',', 1) o
                   from `tabPurchase Invoice Item` pii join `tabPurchase Invoice` pi on pi.name = pii.parent
                   where pi.docstatus = 1 group by pii.po_detail) iv
               on iv.k = poi.name
        where po.docstatus = 1 and po.transaction_date between %(f)s and %(t)s {ctx.co('po')}
        group by poi.name""")
    std_price = {r.item_code: flt(r.price_list_rate) for r in ctx.sql("""
        select ip.item_code, avg(ip.price_list_rate) price_list_rate from `tabItem Price` ip join `tabItem` i on i.name = ip.item_code
        where ip.price_list = 'Standard Buying' and ip.buying = 1 and ifnull(ip.uom, i.stock_uom) = i.stock_uom group by ip.item_code""")}
    wavg = {}
    for r in lines:
        if flt(r.sq) > 0:
            a = wavg.setdefault(r.item_code, [0.0, 0.0])
            a[0] += flt(r.amt)
            a[1] += flt(r.sq)
    std = {}
    for k, v in wavg.items():
        avg_rate = v[0] / v[1] if v[1] else 0
        pl = std_price.get(k)
        std[k] = pl if pl and avg_rate and 0.5 <= pl / avg_rate <= 2 else avg_rate

    def days(a, b):
        return (getdate(b) - getdate(a)).days if a and b else None

    # Out-of-order cycle entries: the later document is dated before the one it follows. The user responsible is
    # whoever created that later document (e.g. a PO entered with a date before its Material Request).
    stage_docs = {
        "mp": (_("Request → order"), "mr_n", "mr_d", "po_n", "po_d", "po_o", "Material Request", "Purchase Order"),
        "pr": (_("Order → receipt"), "po_n", "po_d", "pr_n", "pr_d", "pr_o", "Purchase Order", "Purchase Receipt"),
        "ri": (_("Receipt → invoice"), "pr_n", "pr_d", "pi_n", "pi_d", "pi_o", "Purchase Receipt", "Purchase Invoice"),
    }
    wrong, seen_wrong = [], set()
    planned_all, actual_all, late_all = [], [], []
    quick, odd = [], []
    line_rows = []
    buckets = {k: 0 for k in ("Same day", "1-3 days", "4-7 days", "8-15 days", "16-30 days", "31-60 days", "60+ days")}
    today = getdate(ctx.t)

    months = {k: {"mp": [], "pr": [], "ri": [], "ontime": [0, 0], "ppv": 0.0, "stdval": 0.0, "plan": [], "act": []} for k, _l in ctx.months}
    stage = {"mp": [], "pr": [], "ri": []}
    by_item, by_sup, pairs = {}, {}, {}
    band = {"Above tolerance": 0, "Within tolerance": 0, "Below tolerance": 0, "Suspect (>±200%)": 0}
    band_val = dict.fromkeys(band, 0.0)
    cover = {"Fully received": 0, "Partly received": 0, "Not received": 0}
    bad_dates = 0
    ppv_tot = std_tot = 0.0
    q_tot = rq_tot = a_tot = b_tot = 0.0
    for r in lines:
        mk = str(r.po_d)[:7]
        mb = months.get(mk)
        d_mp, d_pr, d_ri = days(r.mr_d, r.po_d), days(r.po_d, r.pr_d), days(r.pr_d, r.pi_d)
        for key, dv in (("mp", d_mp), ("pr", d_pr), ("ri", d_ri)):
            if dv is None:
                continue
            if dv < 0:
                bad_dates += 1
                lbl, fn_, fd_, tn_, td_, to_, fdt, tdt = stage_docs[key]
                sig = (key, r[fn_], r[tn_])
                if sig not in seen_wrong:  # one row per document pair, not per PO line
                    seen_wrong.add(sig)
                    wrong.append({"stage": lbl, "from_doctype": fdt, "from_doc": r[fn_], "from_date": str(r[fd_]),
                                  "to_doctype": tdt, "to_doc": r[tn_], "to_date": str(r[td_]), "days": dv, "user": r[to_] or ""})
                continue
            stage[key].append(dv)
            if mb:
                mb[key].append(dv)
        tot_days = sum(x for x in (d_mp, d_pr, d_ri) if x is not None and x >= 0)
        # planned vs actual lead time (order → first receipt)
        d_plan = days(r.po_d, r.req_d)
        if d_plan is not None and d_plan >= 0:
            planned_all.append(d_plan)
            if mb:
                mb["plan"].append(d_plan)
        if d_pr is not None and d_pr >= 0:
            actual_all.append(d_pr)
            if mb:
                mb["act"].append(d_pr)
            buckets["Same day" if d_pr == 0 else "1-3 days" if d_pr <= 3 else "4-7 days" if d_pr <= 7 else "8-15 days" if d_pr <= 15
                    else "16-30 days" if d_pr <= 30 else "31-60 days" if d_pr <= 60 else "60+ days"] += 1
            if d_pr <= 1:
                quick.append({"_po": "Purchase Order", "_pr": "Purchase Receipt", "po": r.po_n, "supplier": r.supplier, "item": r.item_name or r.item_code, "po_date": str(r.po_d),
                              "receipt": r.pr_n, "received": str(r.pr_d), "days": d_pr, "value": round(flt(r.amt), 2),
                              "request": r.mr_n or _("Direct (no request)")})
        late = days(r.req_d, r.pr_d) if r.pr_d and r.req_d else None
        if late is not None and late >= 0 and d_pr is not None and d_pr >= 0:
            late_all.append(late)
        # odd cases
        reason = None
        if d_plan is not None and d_plan <= 0:
            reason = _("Required-by date on/before PO date")
        if d_pr is not None and d_pr > 60:
            reason = _("Lead time {0} days").format(d_pr)
        elif late is not None and late > 30 and (d_pr or 0) >= 0:
            reason = _("Received {0} days after required-by").format(late)
        elif not r.pr_d and flt(r.rq) <= 0 and (today - getdate(r.po_d)).days > 60:
            reason = _("Not received after {0} days").format((today - getdate(r.po_d)).days)
        if reason and not (d_plan is not None and d_plan <= 0 and reason.startswith(_("Required-by")) and d_pr is not None and d_pr <= 1):
            odd.append({"_po": "Purchase Order", "po": r.po_n, "supplier": r.supplier, "item": r.item_name or r.item_code, "po_date": str(r.po_d),
                        "required_by": str(r.req_d or ""), "received": str(r.pr_d or ""), "lead": d_pr if d_pr is not None else "",
                        "reason": reason, "value": round(flt(r.amt), 2)})
        it = by_item.setdefault(r.item_code, {"name": r.item_name or r.item_code, "n": 0, "days": [], "ppv": 0.0, "std": 0.0})
        it["n"] += 1
        if r.pr_d:
            it["days"].append(tot_days)
        sp = by_sup.setdefault(r.supplier, {"n": 0, "ontime": 0, "late": 0, "ppv": 0.0, "std": 0.0})
        sp["n"] += 1
        if r.pr_d and r.req_d:
            on = getdate(r.pr_d) <= getdate(r.req_d)
            sp["ontime" if on else "late"] += 1
            if mb:
                mb["ontime"][0 if on else 1] += 1
        # coverage
        q_tot += flt(r.q); rq_tot += flt(r.rq); a_tot += flt(r.a); b_tot += flt(r.b)
        cover["Fully received" if flt(r.rq) >= flt(r.q) - 1e-6 else "Partly received" if flt(r.rq) > 0 else "Not received"] += 1
        # one drill-down row per PO line (read by get_drilldown)
        rec = {"_po": "Purchase Order", "_mr": "Material Request", "_pr": "Purchase Receipt", "_pi": "Purchase Invoice",
               "po": r.po_n, "po_date": str(r.po_d), "supplier": r.supplier, "item": r.item_name or r.item_code,
               "request": r.mr_n or "", "receipt": r.pr_n or "", "invoice": r.pi_n or "",
               "mr_po": d_mp, "po_pr": d_pr, "pr_pi": d_ri, "cycle": tot_days if r.pr_d else None,
               "planned": d_plan, "late": late, "required_by": str(r.req_d or ""),
               "ontime": ("Yes" if getdate(r.pr_d) <= getdate(r.req_d) else "No") if (r.pr_d and r.req_d) else "",
               "qty": flt(r.q), "received_qty": flt(r.rq), "received_pct": round(flt(r.rq) / flt(r.q) * 100, 1) if flt(r.q) else 0,
               "billed_pct": round(flt(r.b) / flt(r.a) * 100, 1) if flt(r.a) else 0, "value": round(flt(r.amt), 2),
               "std_rate": None, "rate": None, "dev": None, "ppv": None, "band": ""}
        line_rows.append(rec)
        # price variance vs standard
        sr = std.get(r.item_code)
        if sr and flt(r.sq) > 0:
            actual = flt(r.amt) / flt(r.sq)
            dev = (actual - sr) / sr * 100
            rec.update({"std_rate": round(sr, 2), "rate": round(actual, 2), "dev": round(dev, 1)})
            if abs(dev) > 200:
                rec["band"] = "Suspect (>±200%)"
                band["Suspect (>±200%)"] += 1
                band_val["Suspect (>±200%)"] += flt(r.amt)
                continue
            ppv = (actual - sr) * flt(r.sq)
            stdv = sr * flt(r.sq)
            ppv_tot += ppv; std_tot += stdv
            it["ppv"] += ppv; it["std"] += stdv
            sp["ppv"] += ppv; sp["std"] += stdv
            if mb:
                mb["ppv"] += ppv; mb["stdval"] += stdv
            b_ = "Above tolerance" if dev > tol else "Below tolerance" if dev < -tol else "Within tolerance"
            rec.update({"band": b_, "ppv": round(ppv, 2)})
            band[b_] += 1
            band_val[b_] += flt(r.amt)
            pk = (r.supplier, it["name"])
            pr_ = pairs.setdefault(pk, {"dev": 0.0, "w": 0.0, "n": 0})
            pr_["dev"] += dev * flt(r.sq); pr_["w"] += flt(r.sq); pr_["n"] += 1

    avg = lambda xs: round(sum(xs) / len(xs), 1) if xs else 0  # noqa: E731
    a_mp, a_pr, a_ri = avg(stage["mp"]), avg(stage["pr"]), avg(stage["ri"])
    ontime_n = sum(m["ontime"][0] for m in months.values()); late_n = sum(m["ontime"][1] for m in months.values())
    monthly = []
    for k, lbl in ctx.months:
        m = months[k]
        monthly.append({"month": lbl, "mp": avg(m["mp"]), "pr": avg(m["pr"]), "ri": avg(m["ri"]),
                        "ontime": round(m["ontime"][0] / sum(m["ontime"]) * 100, 1) if sum(m["ontime"]) else 0,
                        "ppv": round(m["ppv"], 2), "ppv_rate": round(m["ppv"] / m["stdval"] * 100, 2) if m["stdval"] else 0})
    priced = sum(v for k, v in band.items() if not k.startswith("Suspect"))

    items_busy = sorted(by_item.values(), key=lambda x: -x["n"])[:10]
    slow_items = sorted([x for x in by_item.values() if len(x["days"]) >= 3], key=lambda x: -avg(x["days"]))[:8]
    sup_rows = [(k, v) for k, v in by_sup.items() if v["ontime"] + v["late"] >= 5]
    ppv_sup = sorted(((k, v["ppv"]) for k, v in by_sup.items() if abs(v["ppv"]) > 0.5), key=lambda x: -abs(x[1]))[:8]
    ppv_item = sorted(((v["name"], v["ppv"]) for v in by_item.values() if v["ppv"] > 0.5), key=lambda x: -x[1])[:8]
    abnormal = sorted(((f"{k[0]} · {k[1]}", v["dev"] / v["w"], v["n"]) for k, v in pairs.items() if v["w"] and v["n"] >= 2),
                      key=lambda x: -abs(x[1]))[:10]
    red, green = "hsl(351 95% 59%)", "hsl(160 84% 39%)"

    kpis = [
        _kpi("cycle", _("Average buying cycle"), a_mp + a_pr + a_ri, "number", monthly=[r["mp"] + r["pr"] + r["ri"] for r in monthly], invert=True,
             hint=_("Days, request → invoice")),
        _kpi("mr_po", _("Request → order"), a_mp, "number", monthly=[r["mp"] for r in monthly], invert=True, hint=_("Average days")),
        _kpi("po_pr", _("Order → receipt"), a_pr, "number", monthly=[r["pr"] for r in monthly], invert=True, hint=_("Average days")),
        _kpi("pr_pi", _("Receipt → invoice"), a_ri, "number", monthly=[r["ri"] for r in monthly], invert=True, hint=_("Average days")),
        _kpi("ontime", _("On-time receipts"), ontime_n / (ontime_n + late_n) * 100 if (ontime_n + late_n) else 0, "percent",
             monthly=[r["ontime"] for r in monthly], hint=_("First receipt by the PO's required-by date")),
        _kpi("received_pct", _("PO qty received"), rq_tot / q_tot * 100 if q_tot else 0, "percent", hint=_("Received ÷ ordered quantity")),
        _kpi("billed_pct", _("PO value billed"), b_tot / a_tot * 100 if a_tot else 0, "percent", hint=_("Billed ÷ ordered value")),
        _kpi("lines", _("PO lines"), len(lines), "number", hint=_("{0} suppliers · {1} items").format(len(by_sup), len(by_item))),
        _kpi("ppv", _("Purchase price variance"), ppv_tot, monthly=[r["ppv"] for r in monthly], invert=True,
             hint=_("Positive = paid above standard")),
        _kpi("ppv_rate", _("PPV rate"), ppv_tot / std_tot * 100 if std_tot else 0, "percent", monthly=[r["ppv_rate"] for r in monthly], invert=True,
             hint=_("PPV ÷ standard value")),
        _kpi("above_tol", _("Lines above tolerance"), band["Above tolerance"], "number", invert=True,
             hint=_("{0} of priced lines, > +{1}%").format(_pct(band["Above tolerance"] / priced * 100 if priced else 0), flt(tol, 1))),
        _kpi("below_tol", _("Lines below tolerance"), band["Below tolerance"], "number",
             hint=_("{0} of priced lines, < −{1}%").format(_pct(band["Below tolerance"] / priced * 100 if priced else 0), flt(tol, 1))),
    ]
    a_plan, a_act, a_late = avg(planned_all), avg(actual_all), avg(late_all)
    received_lines = len(actual_all)
    kpis += [
        _kpi("plan_days", _("Scheduled lead time"), a_plan, "number", monthly=[avg(m["plan"]) for m in months.values()],
             hint=_("Avg days, PO → required-by")),
        _kpi("act_days", _("Actual lead time"), a_act, "number", monthly=[avg(m["act"]) for m in months.values()], invert=True,
             hint=_("Avg days, PO → first receipt ({0:+.1f} vs schedule)").format(a_act - a_plan)),
        _kpi("late_days", _("Average delay when late"), avg([x for x in late_all if x > 0]), "number", invert=True,
             hint=_("Days past required-by, late lines only")),
        _kpi("quick", _("Quick purchases"), len(quick), "number",
             hint=_("{0} of received lines, within 1 day of the PO").format(_pct(len(quick) / received_lines * 100 if received_lines else 0))),
        _kpi("odd", _("Odd cases"), len(odd), "number", invert=True, hint=_("Very long, very late, never received or back-dated schedule")),
    ]
    kpis.append(_kpi("suspect", _("Suspect rate lines"), band["Suspect (>±200%)"], "number", invert=True,
                     hint=_("> ±200% off standard — check UOM / price ({0})").format(_money(band_val["Suspect (>±200%)"]))))
    kpis.append(_kpi("bad_dates", _("Out-of-order dates"), bad_dates, "number", invert=True,
                     hint=_("{0} document pairs by {1} users — see list below").format(len(wrong), len({w_["user"] for w_ in wrong}))))
    names = {u.name: u.full_name for u in frappe.get_all("User", filters={"name": ["in", list({w_["user"] for w_ in wrong}) or [""]]},
                                                         fields=["name", "full_name"])}
    by_user = {}
    for w_ in wrong:
        w_["user_name"] = names.get(w_["user"]) or w_["user"] or _("Unknown")
        b_ = by_user.setdefault(w_["user_name"], {"user": w_["user_name"], "mp": 0, "pr": 0, "ri": 0, "n": 0})
        b_[{v_[0]: k_ for k_, v_ in stage_docs.items()}[w_["stage"]]] += 1
        b_["n"] += 1
    wrong.sort(key=lambda x: x["days"])
    widgets = [
        _w("stages", _("Buying cycle by month"), "bar", [{"month": r["month"], "mp": r["mp"], "pr": r["pr"], "ri": r["ri"]} for r in monthly],
           _("Average days per stage, stacked (by PO month)"), span=2, stacked=True,
           series=[{"key": "mp", "label": _("Request → order"), "color": "hsl(262 83% 58%)"}, {"key": "pr", "label": _("Order → receipt"), "color": "hsl(199 89% 48%)"},
                   {"key": "ri", "label": _("Receipt → invoice"), "color": "hsl(35 92% 50%)"}]),
        _w("stage_avg", _("Average cycle"), "bar",
           [{"stage": _("Request → order"), "v": a_mp, "color": "hsl(262 83% 58%)"}, {"stage": _("Order → receipt"), "v": a_pr, "color": "hsl(199 89% 48%)"},
            {"stage": _("Receipt → invoice"), "v": a_ri, "color": "hsl(35 92% 50%)"}, {"stage": _("Total"), "v": a_mp + a_pr + a_ri, "color": "hsl(221 83% 53%)"}],
           _("Days"), xKey="stage", series=[{"key": "v", "label": _("Days")}], colorKey="color"),
        _w("po_pr", _("PO to receipt trend"), "combo", [{"month": r["month"], "days": r["pr"], "ontime": r["ontime"]} for r in monthly],
           _("Average days to first receipt, with on-time %"), span=2, dualAxis=True,
           series=[{"key": "days", "label": _("Days to receipt"), "color": "hsl(199 89% 48%)"},
                   {"key": "ontime", "label": _("On-time %"), "type": "line", "axis": "right", "color": "hsl(160 84% 39%)"}]),
        _w("coverage", _("PO coverage"), "donut", [{"label": k, "value": v} for k, v in cover.items() if v], _("PO lines by receipt status")),
        _w("items", _("Item-wise buying cycle"), "bar",
           [{"item": (x["name"][:22] + "…") if len(x["name"]) > 23 else x["name"], "days": avg(x["days"]), "lines": x["n"]} for x in items_busy],
           _("Average request → invoice days for the 10 most-bought items"), xKey="item", span=2, angledLabels=True,
           series=[{"key": "days", "label": _("Days"), "color": "hsl(221 83% 53%)"}]),
        _w("slow", _("Slowest items to buy"), "barlist", [{"label": x["name"], "value": avg(x["days"])} for x in slow_items], _("Average days, 3+ PO lines")),
        _w("ppv_trend", _("Purchase price variance"), "combo", [{"month": r["month"], "ppv": r["ppv"], "rate": r["ppv_rate"]} for r in monthly],
           _("PPV (bars) and PPV rate % (line); above zero = paid more than standard"), money=True, span=2, dualAxis=True,
           series=[{"key": "ppv", "label": _("PPV"), "color": "hsl(351 95% 59%)"},
                   {"key": "rate", "label": _("PPV rate %"), "type": "line", "axis": "right", "color": "hsl(262 83% 58%)"}]),
        _w("bands", _("Rate abnormality"), "pie", [{"label": k, "value": v, "color": {"Above tolerance": red, "Within tolerance": "hsl(215 16% 65%)", "Below tolerance": green,
                                                                                    "Suspect (>±200%)": "hsl(35 92% 50%)"}[k]}
                                                   for k, v in band.items() if v], _("PO lines vs standard rate, ±{0}%").format(flt(tol, 1))),
        _w("ppv_sup", _("PPV by supplier"), "bar", [{"sup": (k[:20] + "…") if len(k) > 21 else k, "v": round(v, 2), "color": red if v > 0 else green} for k, v in ppv_sup],
           _("Red = paid above standard, green = below"), xKey="sup", money=True, span=2, angledLabels=True, colorKey="color",
           series=[{"key": "v", "label": _("PPV")}]),
        _w("ppv_item", _("Items costing most above standard"), "barlist", [{"label": k, "value": v} for k, v in ppv_item], money=True),
        _w("abnormal", _("Vendor-wise rate variation"), "barlist", [{"label": k, "value": round(v, 1)} for k, v, _n in abnormal],
           _("Average % from the item's standard rate (supplier · item, 2+ lines)"), percent=True, span=2),
        _w("ontime_sup", _("Supplier on-time delivery"), "barlist",
           [{"label": k, "value": round(v["ontime"] / (v["ontime"] + v["late"]) * 100, 1)} for k, v in sorted(sup_rows, key=lambda x: -(x[1]["ontime"] + x[1]["late"]))[:8]],
           _("% of receipts by the required-by date (5+ receipts)"), percent=True),
        _w("lead_trend", _("Scheduled vs actual lead time"), "line",
           [{"month": lbl, "plan": avg(months[k]["plan"]), "act": avg(months[k]["act"])} for k, lbl in ctx.months],
           _("Average days from PO to required-by (plan) and to first receipt (actual)"), span=2,
           series=[{"key": "plan", "label": _("Scheduled"), "color": "hsl(215 16% 65%)"}, {"key": "act", "label": _("Actual"), "color": "hsl(199 89% 48%)"}]),
        _w("lead_dist", _("Lead-time distribution"), "bar",
           [{"band": k, "v": v, "color": "hsl(160 84% 39%)" if k in ("Same day", "1-3 days") else "hsl(351 95% 59%)" if k in ("31-60 days", "60+ days")
             else "hsl(199 89% 48%)"} for k, v in buckets.items()],
           _("PO lines by days from order to first receipt"), xKey="band", colorKey="color", angledLabels=True,
           series=[{"key": "v", "label": _("PO lines")}]),
        _w("quick_items", _("Items bought quickest"), "barlist",
           _pairs([{"label": k_, "v": v_} for k_, v_ in Counter(q_["item"] for q_ in quick).items()], limit=8, other=False),
           _("Quick-purchase lines per item")),
        _w("quick_sup", _("Suppliers delivering same/next day"), "barlist",
           _pairs([{"label": k_, "v": v_} for k_, v_ in Counter(q_["supplier"] for q_ in quick).items()], limit=8, other=False),
           _("Quick-purchase lines per supplier"), span=2),
        _w("quick_list", _("Quick purchases"), "table", sorted(quick, key=lambda x: -x["value"])[:1000],
           _("{0} PO lines received within 1 day, largest first").format(len(quick)), span=3, xKey="",
           columns=[{"key": "po", "label": _("PO"), "doctype_key": "_po"}, {"key": "po_date", "label": _("Ordered")},
                    {"key": "receipt", "label": _("Receipt"), "doctype_key": "_pr"}, {"key": "received", "label": _("Received")},
                    {"key": "days", "label": _("Days"), "align": "right"}, {"key": "supplier", "label": _("Supplier")},
                    {"key": "item", "label": _("Item")}, {"key": "request", "label": _("Request")},
                    {"key": "value", "label": _("Value"), "align": "right"}]),
        _w("odd_list", _("Odd cases"), "table", sorted(odd, key=lambda x: -x["value"])[:1000],
           _("{0} PO lines outside the normal pattern, largest first").format(len(odd)), span=3, xKey="",
           columns=[{"key": "reason", "label": _("Why it is odd")}, {"key": "po", "label": _("PO"), "doctype_key": "_po"},
                    {"key": "po_date", "label": _("Ordered")}, {"key": "required_by", "label": _("Required by")},
                    {"key": "received", "label": _("Received")}, {"key": "lead", "label": _("Lead days"), "align": "right"},
                    {"key": "supplier", "label": _("Supplier")}, {"key": "item", "label": _("Item")},
                    {"key": "value", "label": _("Value"), "align": "right"}]),
        _w("wrong_users", _("Who dates the cycle out of order"), "bar",
           sorted(by_user.values(), key=lambda x: -x["n"])[:12], _("Document pairs where the later document is dated earlier, by its creator"),
           xKey="user", span=3, stacked=True, angledLabels=True,
           series=[{"key": "mp", "label": _("PO before request"), "color": "hsl(262 83% 58%)"},
                   {"key": "pr", "label": _("Receipt before PO"), "color": "hsl(199 89% 48%)"},
                   {"key": "ri", "label": _("Invoice before receipt"), "color": "hsl(35 92% 50%)"}]),
        _w("wrong_list", _("Out-of-order cycle entries"), "table", wrong[:1000],
           _("{0} document pairs, most out of order first").format(len(wrong)), span=3, xKey="",
           columns=[{"key": "stage", "label": _("Stage")},
                    {"key": "from_doc", "label": _("Earlier step"), "doctype_key": "from_doctype"},
                    {"key": "from_date", "label": _("Dated")},
                    {"key": "to_doc", "label": _("Later step"), "doctype_key": "to_doctype"},
                    {"key": "to_date", "label": _("Dated")},
                    {"key": "days", "label": _("Days out"), "align": "right"},
                    {"key": "user_name", "label": _("Created by")}]),
    ]
    ctx.proc_rows, ctx.proc_quick, ctx.proc_odd, ctx.proc_wrong = line_rows, quick, odd, wrong
    ctx.proc_meta = {"bad_dates": bad_dates, "band_val": band_val, "abnormal": abnormal[:3], "ppv_sup": ppv_sup[:1], "tol": tol}
    return kpis, widgets


# ----------------------------------------------------------------------------- sales order analysis
def _so_analysis(ctx):
    """Order book and fulfilment, per SO line (SO date in the period). A line is on time in full (OTIF) when it
    was fully delivered on or before its promised delivery date; open lines past that date are overdue."""
    t = getdate(ctx.t)
    lines = ctx.sql(f"""
        select soi.name, so.name so, so.transaction_date so_d, ifnull(soi.delivery_date, so.delivery_date) promise, so.customer,
               ifnull(so.customer_group, '') cgroup, soi.item_code, max(soi.item_name) item, soi.qty, soi.delivered_qty, soi.base_net_amount amt,
               soi.base_net_rate rate, soi.billed_amt, so.status, dl.first_d, dl.last_d, dl.dn, so.owner
        from `tabSales Order Item` soi join `tabSales Order` so on so.name = soi.parent
        left join (select dni.so_detail k, min(dn.posting_date) first_d, max(dn.posting_date) last_d,
                          substring_index(group_concat(dn.name order by dn.posting_date), ',', 1) dn
                   from `tabDelivery Note Item` dni join `tabDelivery Note` dn on dn.name = dni.parent
                   where dn.docstatus = 1 group by dni.so_detail) dl on dl.k = soi.name
        where so.docstatus = 1 and so.transaction_date between %(f)s and %(t)s {ctx.co('so')}
        group by soi.name""")
    days = lambda a, b: (getdate(b) - getdate(a)).days if a and b else None  # noqa: E731
    months = {k: {"booked": 0.0, "n": set(), "otif": [0, 0], "lead": [], "promise": []} for k, _l in ctx.months}
    rows, cust, cust_otif, item_pending, ageing = [], {}, {}, {}, {k: 0.0 for k in ("Not due", "1-7 days", "8-30 days", "31-60 days", "60+ days")}
    status = Counter()
    for r in lines:
        mb = months.get(str(r.so_d)[:7])
        full = flt(r.delivered_qty) >= flt(r.qty) - 1e-6
        closed = r.status in ("Closed", "Completed")
        pend_q = 0 if (full or closed) else max(flt(r.qty) - flt(r.delivered_qty), 0)
        pend_v = pend_q * flt(r.rate)
        lead = days(r.so_d, r.first_d)
        promised = days(r.so_d, r.promise)
        late_by = days(r.promise, r.last_d) if full and r.last_d else None
        overdue = days(r.promise, t) if pend_q and r.promise and getdate(r.promise) < t else None
        otif = None
        if full and r.last_d and r.promise:
            otif = getdate(r.last_d) <= getdate(r.promise)
        state = ("Delivered on time" if otif else "Delivered late") if full else ("Closed short" if closed else ("Overdue" if overdue else "Open, not due"))
        status[state] += 1
        if mb:
            mb["booked"] += flt(r.amt)
            mb["n"].add(r.so)
            if otif is not None:
                mb["otif"][0 if otif else 1] += 1
            if lead is not None and lead >= 0:
                mb["lead"].append(lead)
            if promised is not None and promised >= 0:
                mb["promise"].append(promised)
        c = cust.setdefault(r.customer, {"booked": 0.0, "pending": 0.0})
        c["booked"] += flt(r.amt)
        c["pending"] += pend_v
        if otif is not None:
            co_ = cust_otif.setdefault(r.customer, [0, 0])
            co_[0 if otif else 1] += 1
        if pend_q:
            item_pending[r.item or r.item_code] = item_pending.get(r.item or r.item_code, 0) + pend_q
            ageing["Not due" if not overdue else "1-7 days" if overdue <= 7 else "8-30 days" if overdue <= 30
                   else "31-60 days" if overdue <= 60 else "60+ days"] += pend_v
        rows.append({"_so": "Sales Order", "_dn": "Delivery Note", "so": r.so, "so_date": str(r.so_d), "customer": r.customer, "item": r.item or r.item_code,
                     "promise": str(r.promise or ""), "qty": flt(r.qty), "delivered": flt(r.delivered_qty),
                     "delivered_pct": round(flt(r.delivered_qty) / flt(r.qty) * 100, 1) if flt(r.qty) else 0,
                     "pending_qty": pend_q, "pending_value": round(pend_v, 2), "value": round(flt(r.amt), 2),
                     "billed_pct": round(flt(r.billed_amt) / flt(r.amt) * 100, 1) if flt(r.amt) else 0,
                     "first_dn": r.dn or "", "first_delivery": str(r.first_d or ""), "lead": lead, "promised_days": promised,
                     "late_by": late_by, "overdue": overdue, "state": state, "status": r.status})
    avg = lambda xs: round(sum(xs) / len(xs), 1) if xs else 0  # noqa: E731
    # delivered value by month comes from the delivery notes themselves (what left the mill in that month)
    dn_m = ctx.monthly(ctx.sql(f"""select date_format(posting_date, '%%Y-%%m') m, sum(base_net_total) v from `tabDelivery Note`
        where docstatus = 1 and posting_date between %(f)s and %(t)s {ctx.co()} group by m"""))
    booked = sum(m["booked"] for m in months.values())
    n_so = len({r["so"] for r in rows})
    q = sum(r["qty"] for r in rows); dq = sum(min(r["delivered"], r["qty"]) for r in rows)
    val = sum(r["value"] for r in rows)
    billed = sum(r["value"] * r["billed_pct"] / 100 for r in rows)
    otif_ok = sum(m["otif"][0] for m in months.values()); otif_n = otif_ok + sum(m["otif"][1] for m in months.values())
    pend_v = sum(r["pending_value"] for r in rows)
    overdue_rows = [r for r in rows if r["overdue"]]
    monthly = []
    for (k, lbl), d in zip(ctx.months, dn_m):
        m = months[k]
        monthly.append({"month": lbl, "booked": round(m["booked"], 2), "delivered": d["v"], "orders": len(m["n"]),
                        "otif": round(m["otif"][0] / sum(m["otif"]) * 100, 1) if sum(m["otif"]) else 0,
                        "lead": avg(m["lead"]), "promise": avg(m["promise"])})
    ctx.so_rows = rows

    kpis = [
        _kpi("booked", _("Orders booked"), booked, monthly=[r["booked"] for r in monthly]),
        _kpi("orders", _("Sales orders"), n_so, "number", monthly=[r["orders"] for r in monthly], hint=_("{0} order lines").format(len(rows))),
        _kpi("avg_order", _("Average order"), booked / n_so if n_so else 0, monthly=[(r["booked"] / r["orders"]) if r["orders"] else 0 for r in monthly]),
        _kpi("qty", _("Quantity ordered"), q, "number"),
        _kpi("delivered_pct", _("Delivered"), dq / q * 100 if q else 0, "percent", hint=_("Delivered ÷ ordered quantity")),
        _kpi("billed_pct", _("Billed"), billed / val * 100 if val else 0, "percent", hint=_("Billed ÷ ordered value")),
        _kpi("open_book", _("Open order book"), pend_v, hint=_("Undelivered value on open orders")),
        _kpi("otif", _("On time in full"), otif_ok / otif_n * 100 if otif_n else 0, "percent", monthly=[r["otif"] for r in monthly],
             hint=_("Fully delivered by the promised date")),
        _kpi("lead", _("Order → first delivery"), avg([r["lead"] for r in rows if r["lead"] is not None and r["lead"] >= 0]), "number",
             monthly=[r["lead"] for r in monthly], invert=True, hint=_("Average days")),
        _kpi("promise", _("Promised lead time"), avg([r["promised_days"] for r in rows if r["promised_days"] is not None and r["promised_days"] >= 0]),
             "number", monthly=[r["promise"] for r in monthly], hint=_("Average days, order → promised date")),
        _kpi("overdue", _("Overdue open lines"), len(overdue_rows), "number", invert=True,
             hint=_("Worth {0}, past the promised date").format(_money(sum(r["pending_value"] for r in overdue_rows)))),
        _kpi("late_lines", _("Delivered late"), status["Delivered late"], "number", invert=True,
             hint=_("Average {0} days after promise").format(avg([r["late_by"] for r in rows if r["late_by"] and r["late_by"] > 0]))),
    ]
    widgets = [
        _w("flow", _("Booked vs delivered"), "combo", [{k: r[k] for k in ("month", "booked", "delivered", "otif")} for r in monthly],
           _("Order value booked and delivered each month, with OTIF %"), money=True, span=2, dualAxis=True,
           series=[{"key": "booked", "label": _("Booked")}, {"key": "delivered", "label": _("Delivered"), "color": "hsl(160 84% 39%)"},
                   {"key": "otif", "label": _("OTIF %"), "type": "line", "axis": "right", "color": "hsl(35 92% 50%)"}]),
        _w("state", _("Order line status"), "donut", [{"label": k, "value": v, "color": {"Delivered on time": "hsl(160 84% 39%)", "Delivered late": "hsl(35 92% 50%)",
                                                                                         "Overdue": "hsl(351 95% 59%)", "Open, not due": "hsl(199 89% 48%)",
                                                                                         "Closed short": "hsl(215 16% 65%)"}.get(k)} for k, v in status.items()]),
        _w("lead", _("Promised vs actual lead time"), "line", [{"month": r["month"], "promise": r["promise"], "lead": r["lead"]} for r in monthly],
           _("Average days from order to promised date and to first delivery"), span=2,
           series=[{"key": "promise", "label": _("Promised"), "color": "hsl(215 16% 65%)"}, {"key": "lead", "label": _("Actual"), "color": "hsl(221 83% 53%)"}]),
        _w("ageing", _("Open order ageing"), "bar", [{"band": k, "v": round(v, 2), "color": "hsl(160 84% 39%)" if k == "Not due" else "hsl(351 95% 59%)" if k in ("31-60 days", "60+ days")
                                                     else "hsl(35 92% 50%)"} for k, v in ageing.items()],
           _("Undelivered value by days past promise"), xKey="band", money=True, colorKey="color", angledLabels=True, series=[{"key": "v", "label": _("Value")}]),
        _w("customers", _("Top customers by orders"), "bar",
           [{"c": (k[:20] + "…") if len(k) > 21 else k, "booked": round(v["booked"], 2), "pending": round(v["pending"], 2)}
            for k, v in sorted(cust.items(), key=lambda x: -x[1]["booked"])[:10]],
           _("Booked value and what is still undelivered"), xKey="c", money=True, span=2, angledLabels=True,
           series=[{"key": "booked", "label": _("Booked")}, {"key": "pending", "label": _("Undelivered"), "color": "hsl(351 95% 59%)"}]),
        _w("otif_cust", _("OTIF by customer"), "barlist",
           [{"label": k, "value": round(v[0] / sum(v) * 100, 1)} for k, v in sorted(cust_otif.items(), key=lambda x: -sum(x[1])) if sum(v) >= 5][:8],
           _("% of lines on time in full (5+ lines)"), percent=True),
        _w("pending_items", _("Items waiting to be delivered"), "barlist", _pairs([{"label": k, "v": v} for k, v in item_pending.items()], limit=8, other=False),
           _("Undelivered quantity")),
        _w("overdue_list", _("Overdue open order lines"), "table", sorted(overdue_rows, key=lambda x: -x["pending_value"])[:1000],
           _("{0} lines past their promised date, largest first").format(len(overdue_rows)), span=3, xKey="",
           columns=[{"key": "so", "label": _("Sales order"), "doctype_key": "_so"}, {"key": "so_date", "label": _("Ordered")},
                    {"key": "promise", "label": _("Promised")}, {"key": "overdue", "label": _("Days overdue"), "format": "days", "align": "right"},
                    {"key": "customer", "label": _("Customer")}, {"key": "item", "label": _("Item")},
                    {"key": "pending_qty", "label": _("Pending qty"), "format": "number", "align": "right"},
                    {"key": "pending_value", "label": _("Pending value"), "format": "money", "align": "right"},
                    {"key": "delivered_pct", "label": _("Delivered %"), "format": "percent", "align": "right"}]),
    ]
    return kpis, widgets


# ----------------------------------------------------------------------------- delivery (DO) analysis
def _do_analysis(ctx):
    """Deliveries in the period, per Delivery Note line: timeliness against the SO line's promised date, speed of
    invoicing (delivery → first invoice line against it), and what has been delivered but not billed."""
    lines = ctx.sql(f"""
        select dni.name, dn.name dn, dn.posting_date d, dn.customer, max(dni.item_name) item, dni.item_code, dni.qty, dni.base_net_amount amt,
               dni.against_sales_order so, ifnull(soi.delivery_date, so.delivery_date) promise, so.transaction_date so_d,
               ifnull(dni.warehouse, dn.set_warehouse) wh, iv.d inv_d, iv.n inv, dn.owner, dayofweek(dn.posting_date) dow
        from `tabDelivery Note Item` dni join `tabDelivery Note` dn on dn.name = dni.parent
        left join `tabSales Order Item` soi on soi.name = dni.so_detail
        left join `tabSales Order` so on so.name = dni.against_sales_order
        left join (select sii.dn_detail k, min(si.posting_date) d, substring_index(group_concat(si.name order by si.posting_date), ',', 1) n
                   from `tabSales Invoice Item` sii join `tabSales Invoice` si on si.name = sii.parent
                   where si.docstatus = 1 and ifnull(sii.dn_detail, '') != '' group by sii.dn_detail) iv on iv.k = dni.name
        where dn.docstatus = 1 and dn.is_return = 0 and dn.posting_date between %(f)s and %(t)s {ctx.co('dn')}
        group by dni.name""")
    days = lambda a, b: (getdate(b) - getdate(a)).days if a and b else None  # noqa: E731
    months = {k: {"dn": set(), "qty": 0.0, "val": 0.0, "ontime": [0, 0], "inv": []} for k, _l in ctx.months}
    rows, cust, items, wh, users = [], Counter(), Counter(), Counter(), Counter()
    late_band = {k: 0 for k in ("Early", "On the day", "1-3 days late", "4-7 days late", "8-15 days late", "15+ days late")}
    inv_band = {k: 0 for k in ("Same day", "1-3 days", "4-7 days", "8-30 days", "30+ days", "Not invoiced")}
    weekday = {k: 0 for k in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")}
    wk = {2: "Mon", 3: "Tue", 4: "Wed", 5: "Thu", 6: "Fri", 7: "Sat", 1: "Sun"}
    names = {u.name: u.full_name for u in frappe.get_all("User", fields=["name", "full_name"])}
    for r in lines:
        mb = months.get(str(r.d)[:7])
        vs = days(r.promise, r.d)  # >0 late
        to_inv = days(r.d, r.inv_d)
        if mb:
            mb["dn"].add(r.dn); mb["qty"] += flt(r.qty); mb["val"] += flt(r.amt)
            if vs is not None:
                mb["ontime"][0 if vs <= 0 else 1] += 1
            if to_inv is not None and to_inv >= 0:
                mb["inv"].append(to_inv)
        if vs is not None:
            late_band["Early" if vs < 0 else "On the day" if vs == 0 else "1-3 days late" if vs <= 3 else "4-7 days late" if vs <= 7
                      else "8-15 days late" if vs <= 15 else "15+ days late"] += 1
        inv_band["Not invoiced" if to_inv is None else "Same day" if to_inv <= 0 else "1-3 days" if to_inv <= 3 else "4-7 days" if to_inv <= 7
                 else "8-30 days" if to_inv <= 30 else "30+ days"] += 1
        cust[r.customer] += flt(r.qty); items[r.item or r.item_code] += flt(r.qty); wh[r.wh or _("Not set")] += flt(r.amt)
        weekday[wk.get(r.dow, "Mon")] += 1
        users[names.get(r.owner) or r.owner] += 1
        rows.append({"_dn": "Delivery Note", "_so": "Sales Order", "_si": "Sales Invoice", "dn": r.dn, "date": str(r.d), "customer": r.customer,
                     "item": r.item or r.item_code, "qty": flt(r.qty), "value": round(flt(r.amt), 2), "so": r.so or "", "promise": str(r.promise or ""),
                     "vs_promise": vs, "invoice": r.inv or "", "invoiced_on": str(r.inv_d or ""), "to_invoice": to_inv, "warehouse": r.wh or "",
                     "created_by": names.get(r.owner) or r.owner})
    avg = lambda xs: round(sum(xs) / len(xs), 1) if xs else 0  # noqa: E731
    n_dn = len({r["dn"] for r in rows})
    qty, val = sum(r["qty"] for r in rows), sum(r["value"] for r in rows)
    timed = [r for r in rows if r["vs_promise"] is not None]
    ontime = sum(1 for r in timed if r["vs_promise"] <= 0)
    unbilled = [r for r in rows if not r["invoice"]]
    inv_days = [r["to_invoice"] for r in rows if r["to_invoice"] is not None and r["to_invoice"] >= 0]
    monthly = [{"month": lbl, "deliveries": len(months[k]["dn"]), "qty": round(months[k]["qty"], 2), "value": round(months[k]["val"], 2),
                "ontime": round(months[k]["ontime"][0] / sum(months[k]["ontime"]) * 100, 1) if sum(months[k]["ontime"]) else 0,
                "inv": avg(months[k]["inv"])} for k, lbl in ctx.months]
    ctx.do_rows = rows

    kpis = [
        _kpi("deliveries", _("Deliveries"), n_dn, "number", monthly=[r["deliveries"] for r in monthly], hint=_("{0} delivery lines").format(len(rows))),
        _kpi("qty", _("Quantity delivered"), qty, "number", monthly=[r["qty"] for r in monthly]),
        _kpi("value", _("Value delivered"), val, monthly=[r["value"] for r in monthly]),
        _kpi("avg_qty", _("Average per delivery"), qty / n_dn if n_dn else 0, "number",
             monthly=[(r["qty"] / r["deliveries"]) if r["deliveries"] else 0 for r in monthly], hint=_("Quantity per delivery note")),
        _kpi("ontime", _("Delivered by promise"), ontime / len(timed) * 100 if timed else 0, "percent", monthly=[r["ontime"] for r in monthly],
             hint=_("Lines delivered on or before the SO's promised date")),
        _kpi("late_days", _("Average lateness"), avg([r["vs_promise"] for r in timed if r["vs_promise"] > 0]), "number", invert=True,
             hint=_("Days after promise, late lines only")),
        _kpi("to_invoice", _("Delivery → invoice"), avg(inv_days), "number", monthly=[r["inv"] for r in monthly], invert=True, hint=_("Average days")),
        _kpi("same_day_inv", _("Invoiced same day"), inv_band["Same day"] / len(rows) * 100 if rows else 0, "percent"),
        _kpi("unbilled", _("Delivered, not invoiced"), sum(r["value"] for r in unbilled), invert=True,
             hint=_("{0} delivery lines").format(len(unbilled))),
        _kpi("against_so", _("Against a sales order"), sum(1 for r in rows if r["so"]) / len(rows) * 100 if rows else 0, "percent",
             hint=_("Delivery lines linked to an SO")),
    ]
    widgets = [
        _w("trend", _("Deliveries by month"), "combo", [{k: r[k] for k in ("month", "value", "deliveries")} for r in monthly],
           _("Value delivered (bars) and number of delivery notes (line)"), money=True, span=2, dualAxis=True,
           series=[{"key": "value", "label": _("Value")}, {"key": "deliveries", "label": _("Deliveries"), "format": "number", "type": "line", "axis": "right", "color": "hsl(35 92% 50%)"}]),
        _w("wh", _("Delivered from"), "donut", _pairs([{"label": k, "v": v} for k, v in wh.items()], limit=6), _("Value by warehouse"), money=True),
        _w("ontime", _("Delivery timeliness"), "line", [{"month": r["month"], "ontime": r["ontime"]} for r in monthly],
           _("% of lines delivered by the promised date"), percent=True, span=2, series=[{"key": "ontime", "label": _("On time %"), "color": "hsl(160 84% 39%)"}]),
        _w("lateness", _("Lateness vs promise"), "bar", [{"band": k, "v": v, "color": "hsl(160 84% 39%)" if k in ("Early", "On the day") else "hsl(35 92% 50%)"
                                                          if k in ("1-3 days late", "4-7 days late") else "hsl(351 95% 59%)"} for k, v in late_band.items()],
           _("Delivery lines"), xKey="band", colorKey="color", angledLabels=True, series=[{"key": "v", "label": _("Lines")}]),
        _w("to_inv", _("Delivery → invoice"), "bar", [{"band": k, "v": v, "color": "hsl(351 95% 59%)" if k in ("30+ days", "Not invoiced") else "hsl(199 89% 48%)"}
                                                       for k, v in inv_band.items()],
           _("Delivery lines by days until invoiced"), xKey="band", colorKey="color", angledLabels=True, series=[{"key": "v", "label": _("Lines")}]),
        _w("customers", _("Top customers by quantity"), "bar", [{"c": (k[:20] + "…") if len(k) > 21 else k, "v": round(v, 2)} for k, v in cust.most_common(10)],
           xKey="c", span=2, angledLabels=True, series=[{"key": "v", "label": _("Quantity"), "color": "hsl(160 84% 39%)"}]),
        _w("items", _("Top items delivered"), "barlist", [{"label": k, "value": round(v, 2)} for k, v in items.most_common(8)], _("Quantity")),
        _w("weekday", _("Deliveries by weekday"), "bar", [{"day": k, "v": v} for k, v in weekday.items()], _("Delivery lines"), xKey="day",
           series=[{"key": "v", "label": _("Lines"), "color": "hsl(262 83% 58%)"}]),
        _w("users", _("Delivery notes by user"), "barlist", [{"label": k, "value": v} for k, v in users.most_common(8)], _("Lines created"), span=2),
        _w("unbilled_list", _("Delivered but not invoiced"), "table", sorted(unbilled, key=lambda x: -x["value"])[:1000],
           _("{0} delivery lines without an invoice, largest first").format(len(unbilled)), span=3, xKey="",
           columns=[{"key": "dn", "label": _("Delivery note"), "doctype_key": "_dn"}, {"key": "date", "label": _("Delivered")},
                    {"key": "customer", "label": _("Customer")}, {"key": "item", "label": _("Item")},
                    {"key": "qty", "label": _("Qty"), "format": "number", "align": "right"}, {"key": "value", "label": _("Value"), "format": "money", "align": "right"},
                    {"key": "so", "label": _("Sales order"), "doctype_key": "_so"}, {"key": "created_by", "label": _("Created by")}]),
    ]
    return kpis, widgets


# ----------------------------------------------------------------------------- export analysis
EXPORT_STAGES = ["Planned", "In Production", "Ready to Ship", "Shipped", "Closed"]
LC_OPEN = ("Draft", "Submitted", "Buyer Approval", "LC Requested", "LC Received", "Confirmed")


def _export_analysis(ctx):
    """Export orders (Sales Orders flagged as export), their LCs and export shipments.
    LC amounts are in USD; PKR figures use each LC's own exchange rate."""
    t = getdate(ctx.t)
    so = ctx.sql(f"""select name, transaction_date d, customer, country_of_destination country, export_status st, base_net_total v,
            total_qty q, per_delivered pd, lc_proforma, incoterm, port_of_discharge port
        from `tabSales Order` where docstatus = 1 and export_order_flag = '1' and transaction_date between %(f)s and %(t)s {ctx.co()}""")
    dom = ctx.monthly(ctx.sql(f"""select date_format(transaction_date, '%%Y-%%m') m, sum(base_net_total) v from `tabSales Order`
        where docstatus = 1 and ifnull(export_order_flag, '') != '1' and transaction_date between %(f)s and %(t)s {ctx.co()} group by m"""))
    lcs = ctx.sql(f"""select name, proforma_date d, customer, country_of_destination country, lc_no, lc_status st, lc_amount usd,
            ifnull(nullif(exchange_rate, 0), 1) fx, lc_expiry_date exp, latest_shipment_date latest, export_order, lc_issuing_bank bank
        from `tabLC Proforma` where docstatus = 1 and proforma_date between %(f)s and %(t)s {ctx.co()}""")
    ships = ctx.sql(f"""select es.name, es.customer, es.sales_order, es.lc_proforma, date(es.etd) etd, date(es.eta) eta, es.actual_shipment_date shipped,
            es.shipment_status st, es.shipping_line line, es.port_of_discharge port, so.country_of_destination country, so.transaction_date so_d,
            lc.latest_shipment_date latest, lc.lc_no
        from `tabExport Shipment` es left join `tabSales Order` so on so.name = es.sales_order
        left join `tabLC Proforma` lc on lc.name = es.lc_proforma
        where date(es.etd) between %(f)s and %(t)s {ctx.co('so') if ctx.company else ''}""")
    days = lambda a, b: (getdate(b) - getdate(a)).days if a and b else None  # noqa: E731
    months = {k: {"v": 0.0, "n": 0, "ship": 0} for k, _l in ctx.months}
    by_country, by_cust, stage = {}, {}, Counter()
    for r in so:
        mb = months.get(str(r.d)[:7])
        if mb:
            mb["v"] += flt(r.v); mb["n"] += 1
        c = by_country.setdefault(r.country or _("Not set"), {"v": 0.0, "n": 0, "transit": []})
        c["v"] += flt(r.v); c["n"] += 1
        by_cust[r.customer] = by_cust.get(r.customer, 0) + flt(r.v)
        stage[r.st or "Planned"] += 1
    exp_v = sum(flt(r.v) for r in so)
    dom_v = sum(r["v"] for r in dom)
    ship_rows, transit, lead, ontime = [], [], [], [0, 0]
    for r in ships:
        tr = days(r.etd, r.eta)
        ld = days(r.so_d, r.shipped)
        late = days(r.latest, r.shipped) if r.shipped and r.latest else None
        if tr is not None and tr >= 0:
            transit.append(tr)
            if r.country in by_country:
                by_country[r.country]["transit"].append(tr)
        if ld is not None and ld >= 0:
            lead.append(ld)
        if late is not None:
            ontime[0 if late <= 0 else 1] += 1
        mb = months.get(str(r.etd)[:7])
        if mb and r.shipped:
            mb["ship"] += 1
        ship_rows.append({"_es": "Export Shipment", "_so": "Sales Order", "shipment": r.name, "customer": r.customer, "so": r.sales_order or "",
                          "country": r.country or "", "lc_no": r.lc_no or "", "etd": str(r.etd or ""), "eta": str(r.eta or ""),
                          "shipped": str(r.shipped or ""), "latest": str(r.latest or ""), "late_by": late, "transit": tr,
                          "status": r.st, "line": r.line or "", "port": r.port or ""})
    lc_rows, open_usd, open_pkr, exp_soon = [], 0.0, 0.0, []
    lc_state = Counter()
    for r in lcs:
        pkr = flt(r.usd) * flt(r.fx)
        is_open = r.st in LC_OPEN
        left = days(t, r.exp) if r.exp else None
        lc_state[r.st or "Draft"] += 1
        row = {"_lc": "LC Proforma", "_so": "Sales Order", "lc": r.name, "lc_no": r.lc_no or "", "date": str(r.d), "customer": r.customer,
               "country": r.country or "", "status": r.st, "usd": round(flt(r.usd), 2), "pkr": round(pkr, 2), "expiry": str(r.exp or ""),
               "days_left": left, "latest": str(r.latest or ""), "so": r.export_order or "", "bank": r.bank or ""}
        lc_rows.append(row)
        if is_open:
            open_usd += flt(r.usd); open_pkr += pkr
            if left is not None and left <= 30:
                exp_soon.append(row)
    avg = lambda xs: round(sum(xs) / len(xs), 1) if xs else 0  # noqa: E731
    in_transit = [r for r in ship_rows if r["status"] in ("Shipped", "In Transit")]
    shipped_orders = stage["Shipped"] + stage["Closed"]
    top_country = max(by_country.items(), key=lambda x: x[1]["v"])[0] if by_country else ""
    ctx.exp_rows = {"so": so, "lc": lc_rows, "ship": ship_rows, "exp_soon": exp_soon}
    monthly = [{"month": lbl, "export": months[k]["v"], "domestic": dm["v"],
                "share": round(months[k]["v"] / (months[k]["v"] + dm["v"]) * 100, 1) if (months[k]["v"] + dm["v"]) else 0,
                "orders": months[k]["n"], "shipments": months[k]["ship"]} for (k, lbl), dm in zip(ctx.months, dom)]

    kpis = [
        _kpi("export_value", _("Export orders"), exp_v, monthly=[r["export"] for r in monthly], hint=_("{0} orders from {1} buyers").format(len(so), len(by_cust))),
        _kpi("export_share", _("Export share of sales"), exp_v / (exp_v + dom_v) * 100 if (exp_v + dom_v) else 0, "percent", monthly=[r["share"] for r in monthly]),
        _kpi("destinations", _("Destination countries"), len([c for c in by_country if c != _("Not set")]), "number", hint=_("Largest: {0}").format(top_country)),
        _kpi("shipped_pct", _("Orders shipped"), shipped_orders / len(so) * 100 if so else 0, "percent", hint=_("Shipped or closed export orders")),
        _kpi("open_lc", _("Open LC value"), open_pkr, hint=_("USD {0} on {1} open LCs").format(f"{open_usd:,.0f}", sum(1 for r in lc_rows if r["status"] in LC_OPEN))),
        _kpi("lc_expiring", _("LCs expired / expiring ≤ 30d"), len(exp_soon), "number", invert=True,
             hint=_("{0} already expired · USD {1} at risk (as on {2})").format(sum(1 for r in exp_soon if (r["days_left"] or 0) < 0),
                                                                              f"{sum(r['usd'] for r in exp_soon):,.0f}", frappe.format(ctx.t, "Date"))),
        _kpi("ontime_lc", _("Shipped within LC date"), ontime[0] / sum(ontime) * 100 if sum(ontime) else 0, "percent",
             hint=_("Shipment on/before the LC's latest shipment date")),
        _kpi("transit", _("Average transit"), avg(transit), "number", hint=_("Days, ETD → ETA")),
        _kpi("lead", _("Order → shipment"), avg(lead), "number", invert=True, hint=_("Average days")),
        _kpi("in_transit", _("Shipments at sea"), len(in_transit), "number", hint=_("Shipped / in transit at period end")),
    ]
    cc = sorted(by_country.items(), key=lambda x: -x[1]["v"])
    palette = ["hsl(221 83% 53%)", "hsl(160 84% 39%)", "hsl(35 92% 50%)", "hsl(262 83% 58%)", "hsl(351 95% 59%)", "hsl(199 89% 48%)",
               "hsl(173 80% 36%)", "hsl(28 80% 45%)", "hsl(243 75% 59%)", "hsl(142 71% 35%)", "hsl(330 81% 60%)", "hsl(215 16% 55%)"]
    buckets = {k: 0.0 for k in ("Expired, still open", "0-15 days", "16-30 days", "31-60 days", "61-90 days", "90+ days")}
    for r in lc_rows:
        if r["status"] in LC_OPEN and r["days_left"] is not None:
            dl = r["days_left"]
            buckets["Expired, still open" if dl < 0 else "0-15 days" if dl <= 15 else "16-30 days" if dl <= 30 else "31-60 days" if dl <= 60
                    else "61-90 days" if dl <= 90 else "90+ days"] += r["usd"]
    widgets = [
        _w("mix", _("Export vs domestic orders"), "combo", [{k: r[k] for k in ("month", "export", "domestic", "share")} for r in monthly],
           _("Order value by month, with export share %"), money=True, span=2, dualAxis=True,
           series=[{"key": "domestic", "label": _("Domestic"), "color": "hsl(215 16% 65%)"}, {"key": "export", "label": _("Export"), "color": "hsl(221 83% 53%)"},
                   {"key": "share", "label": _("Export %"), "type": "line", "axis": "right", "color": "hsl(35 92% 50%)"}]),
        _w("countries", _("Where exports go"), "donut", [{"label": k, "value": round(v["v"], 2), "color": palette[i % len(palette)]} for i, (k, v) in enumerate(cc)],
           _("Export order value by destination"), money=True),
        _w("country_bar", _("Destinations: value & transit"), "combo",
           [{"country": k, "v": round(v["v"], 2), "transit": avg(v["transit"])} for k, v in cc], _("Order value (bars) and average transit days (line)"),
           xKey="country", money=True, span=2, dualAxis=True,
           series=[{"key": "v", "label": _("Export value")}, {"key": "transit", "label": _("Transit days"), "format": "number", "type": "line", "axis": "right", "color": "hsl(351 95% 59%)"}]),
        _w("funnel", _("Export order pipeline"), "bar", [{"stage": s_, "n": stage.get(s_, 0), "color": palette[i]} for i, s_ in enumerate(EXPORT_STAGES)],
           _("Orders by export status"), xKey="stage", colorKey="color", angledLabels=True, series=[{"key": "n", "label": _("Orders")}]),
        _w("buyers", _("Top export buyers"), "barlist", _pairs([{"label": k, "v": v} for k, v in by_cust.items()], limit=8, other=False), money=True),
        _w("lc_status", _("LC status"), "pie", [{"label": k, "value": v} for k, v in lc_state.most_common()], _("LC proformas by workflow state")),
        _w("lc_expiry", _("Open LC expiry profile"), "bar",
           [{"band": k, "usd": round(v, 2), "color": "hsl(351 95% 59%)" if k in ("Expired, still open", "0-15 days") else "hsl(35 92% 50%)" if k == "16-30 days"
             else "hsl(160 84% 39%)"} for k, v in buckets.items()],
           _("USD on open LCs by days to expiry (as on the period end)"), xKey="band", colorKey="color", angledLabels=True,
           series=[{"key": "usd", "label": _("USD")}]),
        _w("shipments", _("Export shipments by month"), "bar", [{"month": r["month"], "n": r["shipments"]} for r in monthly], _("Containers shipped (by ETD)"),
           series=[{"key": "n", "label": _("Shipments"), "color": "hsl(199 89% 48%)"}]),
        _w("lines", _("Shipping lines used"), "pie", _pairs([{"label": k, "v": v} for k, v in Counter(r["line"] for r in ship_rows if r["line"]).items()], limit=7)),
        _w("lc_soon", _("Open LCs expired or expiring within 30 days"), "table", sorted(exp_soon, key=lambda x: x["days_left"] if x["days_left"] is not None else 0),
           _("{0} LCs expired or expiring — ship, amend or close").format(len(exp_soon)), span=3, xKey="",
           columns=[{"key": "lc", "label": _("LC proforma"), "doctype_key": "_lc"}, {"key": "lc_no", "label": _("LC no")}, {"key": "customer", "label": _("Buyer")},
                    {"key": "country", "label": _("Country")}, {"key": "status", "label": _("Status")}, {"key": "expiry", "label": _("Expires")},
                    {"key": "days_left", "label": _("Days left"), "format": "days", "align": "right"}, {"key": "latest", "label": _("Latest shipment")},
                    {"key": "usd", "label": _("USD"), "format": "number", "align": "right"}, {"key": "so", "label": _("Export order"), "doctype_key": "_so"}]),
    ]
    return kpis, widgets


# ----------------------------------------------------------------------------- import analysis
# Pakistan treatment (see import_cost_sheet.py): these are capitalised into the stock's landed cost…
CHARGES = [("freight", "Freight"), ("insurance", "Insurance"), ("customs_duty", "Customs duty"), ("additional_duty", "Additional duty"),
           ("regulatory_duty", "Regulatory duty"), ("clearing_charges", "Clearing"), ("port_charges", "Port charges"), ("other_charges", "Other")]
# …while these are adjustable taxes paid at import (tax assets, not stock cost).
ADJUSTABLE = [("sales_tax", "Input sales tax (s.7 STA)"), ("income_tax_148", "Income tax u/s 148")]


def _import_analysis(ctx):
    """Import shipments with their landed-cost sheets: value, duties & charges, transit and port dwell."""
    rows = ctx.sql(f"""select sh.name, sh.supplier, sh.purchase_order po, sh.purchase_receipt pr, sh.import_cost_sheet cs, date(sh.etd) etd, date(sh.eta) eta,
            sh.actual_arrival arr, sh.clearance_date clr, sh.shipment_status st, sh.port_of_loading lport, sh.port_of_discharge dport,
            sh.shipping_line line, sh.duty_amount duty, sh.tax_amount tax, ifnull(c.total_purchase_value, 0) pv, ifnull(c.total_landed_cost, 0) lc
        from `tabImport Shipment` sh left join `tabImport Cost Sheet` c on c.name = sh.import_cost_sheet
        where date(sh.etd) between %(f)s and %(t)s {ctx.co('c')}""")
    all_fields = CHARGES + ([x for x in ADJUSTABLE if frappe.db.has_column("Import Cost Sheet Item", x[0])])
    charges = {k: 0.0 for k, _l in all_fields}
    if rows:
        cs_names = tuple(r.cs for r in rows if r.cs) or ("",)
        for r in frappe.db.sql(f"select {', '.join(f'sum({k}) {k}' for k, _l in all_fields)} from `tabImport Cost Sheet Item` where parent in %s",
                               (cs_names,), as_dict=1):
            charges = {k: flt(r.get(k)) for k, _l in all_fields}
    total_pur = flt(ctx.sql(f"""select sum(base_net_total) v from `tabPurchase Invoice` where docstatus = 1 and posting_date between %(f)s and %(t)s {ctx.co()}""")[0].v)
    days = lambda a, b: (getdate(b) - getdate(a)).days if a and b else None  # noqa: E731
    t = getdate(ctx.t)
    months = {k: {"pv": 0.0, "lc": 0.0, "n": 0, "dwell": []} for k, _l in ctx.months}
    by_port, by_sup, recs = {}, Counter(), []
    transit, dwell, delays = [], [], []
    for r in rows:
        mb = months.get(str(r.etd)[:7])
        tr = days(r.etd, r.arr)
        dw = days(r.arr, r.clr)
        dl = days(r.eta, r.arr)
        if mb:
            mb["pv"] += flt(r.pv); mb["lc"] += flt(r.lc); mb["n"] += 1
            if dw is not None and dw >= 0:
                mb["dwell"].append(dw)
        if tr is not None and tr >= 0:
            transit.append(tr)
        if dw is not None and dw >= 0:
            dwell.append(dw)
        if dl is not None:
            delays.append(dl)
        p = by_port.setdefault(r.lport or _("Not set"), {"pv": 0.0, "transit": [], "dwell": []})
        p["pv"] += flt(r.pv)
        if tr is not None and tr >= 0:
            p["transit"].append(tr)
        if dw is not None and dw >= 0:
            p["dwell"].append(dw)
        by_sup[r.supplier] += flt(r.pv)
        recs.append({"_sh": "Import Shipment", "_po": "Purchase Order", "_pr": "Purchase Receipt", "shipment": r.name, "supplier": r.supplier,
                     "po": r.po or "", "receipt": r.pr or "", "loading": r.lport or "", "etd": str(r.etd or ""), "eta": str(r.eta or ""),
                     "arrived": str(r.arr or ""), "cleared": str(r.clr or ""), "transit": tr, "dwell": dw, "delay": dl, "status": r.st,
                     "purchase": round(flt(r.pv), 2), "landed": round(flt(r.lc), 2),
                     "uplift": round((flt(r.lc) - flt(r.pv)) / flt(r.pv) * 100, 1) if flt(r.pv) else None,
                     "duty": round(flt(r.duty), 2), "tax": round(flt(r.tax), 2)})
    avg = lambda xs: round(sum(xs) / len(xs), 1) if xs else 0  # noqa: E731
    pv, lc = sum(r["purchase"] for r in recs), sum(r["landed"] for r in recs)
    duties = sum(r["duty"] for r in recs)            # customs + additional + regulatory duty (non-refundable, in landed cost)
    adjustable = sum(r["tax"] for r in recs)         # input sales tax + s.148 income tax (tax assets, not stock cost)
    duty_tax = duties + adjustable
    late = [r for r in recs if r["delay"] is not None and r["delay"] > 2]
    at_sea = [r for r in recs if not r["arrived"]]
    ctx.imp_rows = recs
    monthly = [{"month": lbl, "purchase": round(months[k]["pv"], 2), "landed": round(months[k]["lc"], 2),
                "uplift": round((months[k]["lc"] - months[k]["pv"]) / months[k]["pv"] * 100, 1) if months[k]["pv"] else 0,
                "n": months[k]["n"], "dwell": avg(months[k]["dwell"])} for k, lbl in ctx.months]

    kpis = [
        _kpi("import_value", _("Import purchases"), pv, monthly=[r["purchase"] for r in monthly], hint=_("{0} shipments").format(len(recs))),
        _kpi("landed", _("Landed cost"), lc, monthly=[r["landed"] for r in monthly], invert=True),
        _kpi("uplift", _("Landing uplift"), (lc - pv) / pv * 100 if pv else 0, "percent", monthly=[r["uplift"] for r in monthly], invert=True,
             hint=_("Landed ÷ purchase value − 1 (excl. adjustable taxes)")),
        _kpi("duty_tax", _("Duties (non-refundable)"), duties, invert=True,
             hint=_("Customs + additional + regulatory · {0} of purchase value").format(_pct(duties / pv * 100 if pv else 0))),
        _kpi("adjustable", _("Adjustable taxes paid"), adjustable,
             hint=_("Input sales tax + s.148 income tax — claimable, not stock cost")),
        _kpi("import_share", _("Imports share of purchases"), pv / total_pur * 100 if total_pur else 0, "percent"),
        _kpi("transit", _("Average transit"), avg(transit), "number", hint=_("Days, ETD → arrival")),
        _kpi("dwell", _("Average port dwell"), avg(dwell), "number", monthly=[r["dwell"] for r in monthly], invert=True, hint=_("Days, arrival → customs clearance")),
        _kpi("late", _("Delayed arrivals"), len(late), "number", invert=True, hint=_("Arrived more than 2 days after ETA")),
        _kpi("at_sea", _("Shipments at sea"), len(at_sea), "number", hint=_("Not yet arrived")),
    ]
    charge_rows = [{"label": _(lbl), "value": round(charges[k], 2)} for k, lbl in CHARGES if charges[k]]
    widgets = [
        _w("trend", _("Import value vs landed cost"), "combo", [{k: r[k] for k in ("month", "purchase", "landed", "uplift")} for r in monthly],
           _("By month of departure, with landing uplift %"), money=True, span=2, dualAxis=True,
           series=[{"key": "purchase", "label": _("Purchase value"), "color": "hsl(215 16% 65%)"}, {"key": "landed", "label": _("Landed cost"), "color": "hsl(28 80% 45%)"},
                   {"key": "uplift", "label": _("Uplift %"), "type": "line", "axis": "right", "color": "hsl(351 95% 59%)"}]),
        _w("charges", _("What imports add to stock cost"), "donut", charge_rows, _("Capitalised: freight, insurance, duties, clearing, port, other"), money=True),
        _w("taxes", _("Taxes paid at import (adjustable)"), "pie",
           [{"label": _(lbl), "value": round(charges.get(k, 0), 2)} for k, lbl in ADJUSTABLE if charges.get(k)],
           _("Booked to tax assets — Sales Tax Act s.7, Income Tax Ordinance s.148"), money=True),
        _w("charge_pct", _("Charges as % of purchase value"), "bar",
           [{"charge": r["label"], "pct": round(r["value"] / pv * 100, 2) if pv else 0} for r in sorted(charge_rows, key=lambda x: -x["value"])],
           xKey="charge", percent=True, angledLabels=True, span=2, series=[{"key": "pct", "label": _("% of purchase"), "color": "hsl(28 80% 45%)"}]),
        _w("origins", _("Import value by loading port"), "barlist", _pairs([{"label": k, "v": v["pv"]} for k, v in by_port.items()], other=False), money=True),
        _w("port_times", _("Transit & dwell by origin"), "bar",
           [{"port": k, "transit": avg(v["transit"]), "dwell": avg(v["dwell"])} for k, v in sorted(by_port.items(), key=lambda x: -x[1]["pv"])],
           _("Average days at sea and at the Pakistani port"), xKey="port", span=2, stacked=True,
           series=[{"key": "transit", "label": _("Transit"), "color": "hsl(199 89% 48%)"}, {"key": "dwell", "label": _("Port dwell"), "color": "hsl(351 95% 59%)"}]),
        _w("suppliers", _("Top import suppliers"), "barlist", _pairs([{"label": k, "v": v} for k, v in by_sup.items()], limit=8, other=False), money=True),
        _w("dwell_trend", _("Port dwell by month"), "line", [{"month": r["month"], "dwell": r["dwell"]} for r in monthly], _("Average days from arrival to clearance"),
           span=2, series=[{"key": "dwell", "label": _("Dwell days"), "color": "hsl(351 95% 59%)"}]),
        _w("status", _("Shipment status"), "pie", [{"label": k, "value": v} for k, v in Counter(r["status"] for r in recs).most_common()]),
        _w("late_list", _("Delayed arrivals"), "table", sorted(late, key=lambda x: -x["delay"]), _("{0} shipments more than 2 days late").format(len(late)),
           span=3, xKey="",
           columns=[{"key": "shipment", "label": _("Shipment"), "doctype_key": "_sh"}, {"key": "supplier", "label": _("Supplier")},
                    {"key": "loading", "label": _("From")}, {"key": "eta", "label": _("ETA")}, {"key": "arrived", "label": _("Arrived")},
                    {"key": "delay", "label": _("Days late"), "format": "days", "align": "right"}, {"key": "dwell", "label": _("Dwell days"), "format": "days", "align": "right"},
                    {"key": "landed", "label": _("Landed cost"), "format": "money", "align": "right"}, {"key": "po", "label": _("PO"), "doctype_key": "_po"}]),
    ]
    return kpis, widgets


# ----------------------------------------------------------------------------- quality (QA / QC)
QI_STAGE = {"Incoming": "Incoming fibre", "In Process": "In process & lab", "Outgoing": "Outgoing / packing"}
AVG3 = "(cast(nullif(r.reading_1, '') as decimal(14,4)) + cast(nullif(r.reading_2, '') as decimal(14,4)) + cast(nullif(r.reading_3, '') as decimal(14,4))) / 3"


def _quality(ctx):
    """Quality inspections (incoming fibre, in-process sliver/roving, yarn lab, outgoing packing) with their readings,
    plus the Quality module: non-conformances, goal reviews and corrective / preventive actions."""
    qis = ctx.sql(f"""select q.name, q.report_date d, q.inspection_type itype, q.quality_inspection_template tpl, q.status, q.item_code, q.item_name,
            q.reference_type rtype, q.reference_name ref, q.inspected_by, pr.supplier
        from `tabQuality Inspection` q left join `tabPurchase Receipt` pr on q.reference_type = 'Purchase Receipt' and pr.name = q.reference_name
        where q.docstatus = 1 and q.report_date between %(f)s and %(t)s {ctx.co('q')}""")
    params = ctx.sql(f"""select r.specification spec, q.quality_inspection_template tpl, date_format(q.report_date, '%%Y-%%m') m,
            count(*) n, sum(r.status = 'Rejected') bad, avg({AVG3}) av
        from `tabQuality Inspection Reading` r join `tabQuality Inspection` q on q.name = r.parent
        where q.docstatus = 1 and q.report_date between %(f)s and %(t)s {ctx.co('q')} group by r.specification, q.quality_inspection_template, m""")
    ncs = ctx.sql("""select name, subject, `procedure`, status, creation from `tabNon Conformance`
        where date(creation) <= %(t)s""")
    reviews = ctx.sql("select name, goal, date, status from `tabQuality Review` where date between %(f)s and %(t)s")
    actions = ctx.sql("select name, goal, date, status, corrective_preventive cp from `tabQuality Action` where date between %(f)s and %(t)s")

    months = {k: {"n": 0, "ok": 0} for k, _l in ctx.months}
    by_tpl, by_type, by_sup = {}, Counter(), {}
    for q in qis:
        mb = months.get(str(q.d)[:7])
        acc = q.status == "Accepted"
        if mb:
            mb["n"] += 1; mb["ok"] += acc
        t = by_tpl.setdefault(q.tpl or _("No template"), [0, 0])
        t[0] += 1; t[1] += (not acc)
        by_type[QI_STAGE.get(q.itype, q.itype)] += 1
        if q.itype == "Incoming" and q.supplier:
            sp = by_sup.setdefault(q.supplier, [0, 0])
            sp[0] += 1; sp[1] += (not acc)
    n = len(qis)
    ok = sum(1 for q in qis if q.status == "Accepted")
    inc = [q for q in qis if q.itype == "Incoming"]
    yarn = [q for q in qis if (q.tpl or "").startswith("Ring Spun Yarn")]
    pct = lambda a, b: round(a / b * 100, 1) if b else 0  # noqa: E731
    # parameter statistics
    spec_fail = Counter()
    trend = defaultdict(dict)
    for r in params:
        spec_fail[r.spec] += int(flt(r.bad))
        if (r.tpl or "").startswith("Ring Spun Yarn") and r.av is not None:
            trend[r.m][r.spec] = round(flt(r.av), 2)

    def yarn_avg(spec):
        vals = [(flt(r.av), flt(r.n)) for r in params if r.spec == spec and (r.tpl or "").startswith("Ring Spun Yarn") and r.av is not None]
        w = sum(n_ for _v, n_ in vals)
        return round(sum(v * n_ for v, n_ in vals) / w, 2) if w else 0

    monthly = []
    for k, lbl in ctx.months:
        tr = trend.get(k, {})
        monthly.append({"month": lbl, "n": months[k]["n"], "acc": pct(months[k]["ok"], months[k]["n"]),
                        "u": tr.get("U %", 0), "cvm": tr.get("CVm %", 0), "csp": tr.get("CSP (count strength product)", 0),
                        "thin": tr.get("Thin places -50% /km", 0), "thick": tr.get("Thick places +50% /km", 0), "neps": tr.get("Neps +200% /km", 0)})
    open_nc = [c for c in ncs if c.status == "Open"]
    rv_pass = sum(1 for r in reviews if r.status == "Passed")
    open_act = [a for a in actions if a.status == "Open"]
    goal_score = {}
    for r in reviews:
        g = goal_score.setdefault(r.goal, {"goal": r.goal, "passed": 0, "failed": 0})
        g["passed" if r.status == "Passed" else "failed"] += 1
    ctx.qa = {"qis": qis, "ncs": ncs, "reviews": reviews, "actions": actions}

    kpis = [
        _kpi("inspections", _("Inspections"), n, "number", monthly=[r["n"] for r in monthly], hint=_("{0} items inspected").format(len({q.item_code for q in qis}))),
        _kpi("acceptance", _("Acceptance rate"), pct(ok, n), "percent", monthly=[r["acc"] for r in monthly], hint=_("Accepted ÷ inspected")),
        _kpi("rejected", _("Rejected lots"), n - ok, "number", monthly=[r["n"] - round(r["n"] * r["acc"] / 100) for r in monthly], invert=True),
        _kpi("incoming", _("Incoming fibre acceptance"), pct(sum(1 for q in inc if q.status == "Accepted"), len(inc)), "percent",
             hint=_("{0} fibre lots tested").format(len(inc))),
        _kpi("rft", _("Yarn right-first-time"), pct(sum(1 for q in yarn if q.status == "Accepted"), len(yarn)), "percent",
             hint=_("{0} yarn lots tested").format(len(yarn))),
        _kpi("u_pct", _("Average yarn U%"), yarn_avg("U %"), "number", monthly=[r["u"] for r in monthly], invert=True, hint=_("Uster evenness, limit 12")),
        _kpi("csp", _("Average CSP"), yarn_avg("CSP (count strength product)"), "number", monthly=[r["csp"] for r in monthly], hint=_("Minimum 2,200")),
        _kpi("open_nc", _("Open non-conformances"), len(open_nc), "number", invert=True, hint=_("{0} raised, {1} resolved").format(len(ncs), sum(1 for c in ncs if c.status == "Resolved"))),
        _kpi("goals", _("Goal reviews passed"), pct(rv_pass, len(reviews)), "percent", hint=_("{0} of {1} monthly reviews").format(rv_pass, len(reviews))),
        _kpi("open_actions", _("Open CAPA actions"), len(open_act), "number", invert=True, hint=_("{0} actions in the period").format(len(actions))),
    ]
    widgets = [
        _w("volume", _("Inspections & acceptance"), "combo", [{k: r[k] for k in ("month", "n", "acc")} for r in monthly], _("Inspections per month with acceptance %"),
           span=2, dualAxis=True, series=[{"key": "n", "label": _("Inspections"), "color": "hsl(199 89% 48%)"},
                                          {"key": "acc", "label": _("Accepted %"), "type": "line", "axis": "right", "color": "hsl(160 84% 39%)"}]),
        _w("stages", _("Inspections by stage"), "donut", [{"label": k, "value": v} for k, v in by_type.most_common()]),
        _w("tpl_rate", _("Rejection rate by test"), "bar",
           [{"tpl": k.replace(" - ", " · "), "rate": pct(v[1], v[0]), "color": "hsl(351 95% 59%)" if pct(v[1], v[0]) > 5 else "hsl(35 92% 50%)" if pct(v[1], v[0]) > 2 else "hsl(160 84% 39%)"}
            for k, v in sorted(by_tpl.items(), key=lambda x: -pct(x[1][1], x[1][0]))],
           _("% of lots rejected per inspection template"), xKey="tpl", percent=True, colorKey="color", angledLabels=True, span=2,
           series=[{"key": "rate", "label": _("Rejected %")}]),
        _w("params", _("Parameters failing most"), "barlist", [{"label": k, "value": v} for k, v in spec_fail.most_common(8) if v], _("Out-of-limit readings")),
        _w("evenness", _("Yarn evenness & strength"), "combo", [{k: r[k] for k in ("month", "u", "cvm", "csp")} for r in monthly],
           _("Monthly averages: U% / CVm% (lines) and CSP (bars, right)"), span=2, dualAxis=True,
           series=[{"key": "csp", "label": _("CSP"), "axis": "right", "color": "hsl(215 16% 75%)"}, {"key": "u", "label": _("U %"), "type": "line", "color": "hsl(221 83% 53%)"},
                   {"key": "cvm", "label": _("CVm %"), "type": "line", "color": "hsl(262 83% 58%)"}]),
        _w("ipi", _("Imperfections per km"), "bar", [{k: r[k] for k in ("month", "thin", "thick", "neps")} for r in monthly], _("Yarn IPI components (monthly average)"),
           stacked=True, series=[{"key": "thin", "label": _("Thin -50%"), "color": "hsl(199 89% 48%)"}, {"key": "thick", "label": _("Thick +50%"), "color": "hsl(35 92% 50%)"},
                                 {"key": "neps", "label": _("Neps +200%"), "color": "hsl(351 95% 59%)"}]),
        _w("suppliers", _("Fibre suppliers by rejection rate"), "barlist",
           [{"label": k, "value": pct(v[1], v[0])} for k, v in sorted(by_sup.items(), key=lambda x: -pct(x[1][1], x[1][0])) if v[0] >= 5 and v[1]][:8],
           _("% of their lots rejected (5+ lots tested)"), percent=True),
        _w("goals", _("Quality goal scorecard"), "bar", sorted(goal_score.values(), key=lambda x: -x["failed"]), _("Monthly reviews passed vs failed"),
           xKey="goal", stacked=True, angledLabels=True, span=2,
           series=[{"key": "passed", "label": _("Passed"), "color": "hsl(160 84% 39%)"}, {"key": "failed", "label": _("Failed"), "color": "hsl(351 95% 59%)"}]),
        _w("nc_status", _("Non-conformance status"), "pie", [{"label": k, "value": v, "color": {"Open": "hsl(351 95% 59%)", "Resolved": "hsl(160 84% 39%)"}.get(k)}
                                                             for k, v in Counter(c.status for c in ncs).most_common()]),
        _w("nc_open", _("Open non-conformances"), "table",
           [{"_nc": "Non Conformance", "nc": c.name, "subject": c.subject, "procedure": c.procedure, "raised": str(c.creation)[:10]} for c in open_nc],
           _("{0} awaiting corrective action").format(len(open_nc)), span=3, xKey="",
           columns=[{"key": "nc", "label": _("Non conformance"), "doctype_key": "_nc"}, {"key": "subject", "label": _("Subject")},
                    {"key": "procedure", "label": _("Procedure")}, {"key": "raised", "label": _("Raised")}]),
    ]
    return kpis, widgets


# ----------------------------------------------------------------------------- work order analysis
OPEN_WO = ("Not Started", "In Process", "Stopped", "Draft")


def _wo_stream(fg_warehouse):
    w = (fg_warehouse or "").lower()
    if "third party" in w:
        return "Conversion"
    return "Mill 2" if "mill2" in w else "Mill 1"


def _wo_analysis(ctx):
    """Individual work orders: completion, schedule adherence, ageing, yield / OPS variance, spindles and downtime.
    On-time = the order finished (actual end date) on or before its expected delivery date."""
    t = getdate(ctx.t)
    wos = ctx.sql(f"""select w.name, w.work_order_date d, w.production_item item, w.item_name, w.status, w.qty, w.produced_qty, w.fg_warehouse,
            date(w.planned_start_date) ps, date(w.actual_start_date) ast, date(w.actual_end_date) aen, w.expected_delivery_date edd,
            w.target_yield ty, w.actual_yield ay, w.target_ops tops, w.actual_ops aops, w.actual_waste waste,
            w.spindle_required sreq, w.spindle_allocated salloc, w.spindle_worked sworked, w.owner,
            ifnull(dt.hours, 0) dt_hours, ifnull(dt.n, 0) dt_n
        from `tabWork Order` w
        left join (select work_order, sum(downtime) / 60 hours, count(*) n from `tabDowntime Entry` where docstatus < 2 group by work_order) dt
               on dt.work_order = w.name
        where w.docstatus = 1 and w.work_order_date between %(f)s and %(t)s {ctx.co('w')}""")
    names = {u.name: u.full_name for u in frappe.get_all("User", fields=["name", "full_name"])}
    days = lambda a, b: (getdate(b) - getdate(a)).days if a and b else None  # noqa: E731
    months = {k: {"n": 0, "done": 0, "ot": [0, 0]} for k, _l in ctx.months}
    streams, bands, lateness, yvar_bins = {}, {}, Counter(), Counter()
    item_short, item_yield, by_owner = {}, {}, Counter()
    ageing = {k: 0 for k in ("0-7 days", "8-15 days", "16-30 days", "31-60 days", "60+ days")}
    rows = []
    for w in wos:
        mb = months.get(str(w.d)[:7])
        done = w.status in ("Completed", "Closed")
        end = w.aen if (w.aen and w.ast and w.aen >= w.ast) else (w.ast if done else None)   # never a negative duration
        late = days(w.edd, end) if done and end and w.edd else None
        start_delay = days(w.ps, w.ast)
        stream = _wo_stream(w.fg_warehouse)
        count = re.search(r"(\d+)\s*/\s*\d", w.item_name or "")
        count = int(count.group(1)) if count else None
        band = "≤12s" if count and count <= 12 else "13-24s" if count and count <= 24 else "25-32s" if count and count <= 32 else "33s+" if count else "Other"
        if mb:
            mb["n"] += 1; mb["done"] += done
            if late is not None:
                mb["ot"][0 if late <= 0 else 1] += 1
        st = streams.setdefault(stream, {"n": 0, "ot": 0, "late": 0, "qty": 0.0, "prod": 0.0})
        st["n"] += 1; st["qty"] += flt(w.qty); st["prod"] += flt(w.produced_qty)
        if late is not None:
            st["ot" if late <= 0 else "late"] += 1
            lateness["Early" if late < 0 else "On the day" if late == 0 else "1-3 days late" if late <= 3 else "4-7 days late" if late <= 7
                     else "8-15 days late" if late <= 15 else "15+ days late"] += 1
        bd = bands.setdefault(band, {"tops": [], "aops": []})
        if flt(w.tops) and flt(w.aops):
            bd["tops"].append(flt(w.tops)); bd["aops"].append(flt(w.aops))
        yv = None
        if flt(w.ty) and 1 <= flt(w.ay) <= 100:
            yv = round(flt(w.ay) - flt(w.ty), 2)
            yvar_bins["below −2 pts" if yv < -2 else "−2 to −1" if yv < -1 else "−1 to 0" if yv < 0 else "0 to +1" if yv <= 1 else "above +1"] += 1
            iy = item_yield.setdefault(w.item_name or w.item, [])
            iy.append(yv)
        short = max(flt(w.qty) - flt(w.produced_qty), 0) if done else 0
        if short:
            item_short[w.item_name or w.item] = item_short.get(w.item_name or w.item, 0) + short
        if not done:
            age = (t - getdate(w.d)).days
            ageing["0-7 days" if age <= 7 else "8-15 days" if age <= 15 else "16-30 days" if age <= 30 else "31-60 days" if age <= 60 else "60+ days"] += 1
        by_owner[names.get(w.owner) or w.owner] += 1
        rows.append({"_wo": "Work Order", "wo": w.name, "date": str(w.d), "item": w.item_name or w.item, "stream": stream, "status": w.status,
                     "qty": flt(w.qty), "produced": flt(w.produced_qty), "achieved": round(flt(w.produced_qty) / flt(w.qty) * 100, 1) if flt(w.qty) else 0,
                     "planned_start": str(w.ps or ""), "started": str(w.ast or ""), "finished": str(end or ""), "due": str(w.edd or ""),
                     "late": late, "start_delay": start_delay, "age": (t - getdate(w.d)).days if not done else None,
                     "target_yield": flt(w.ty), "actual_yield": flt(w.ay), "yield_var": yv, "target_ops": flt(w.tops), "actual_ops": flt(w.aops),
                     "ops_var": round((flt(w.aops) - flt(w.tops)) / flt(w.tops) * 100, 1) if flt(w.tops) and flt(w.aops) else None,
                     "spindles_req": flt(w.sreq), "spindles_alloc": flt(w.salloc), "downtime_h": round(flt(w.dt_hours), 1), "stops": int(w.dt_n)})
    ctx.wo_rows = rows
    avg = lambda xs: round(sum(xs) / len(xs), 2) if xs else 0  # noqa: E731
    n = len(rows)
    done_rows = [r for r in rows if r["status"] in ("Completed", "Closed")]
    open_rows = [r for r in rows if r["status"] not in ("Completed", "Closed")]
    timed = [r for r in rows if r["late"] is not None]
    late_rows = [r for r in timed if r["late"] > 0]
    yv_all = [r["yield_var"] for r in rows if r["yield_var"] is not None]
    ops_all = [r["ops_var"] for r in rows if r["ops_var"] is not None]
    alloc = sum(r["spindles_alloc"] for r in rows)
    req_ = sum(r["spindles_req"] for r in rows)
    dt_rows = [r for r in rows if r["downtime_h"]]
    monthly = [{"month": lbl, "created": months[k]["n"], "completed": months[k]["done"],
                "ontime": round(months[k]["ot"][0] / sum(months[k]["ot"]) * 100, 1) if sum(months[k]["ot"]) else 0} for k, lbl in ctx.months]
    pct = lambda a, b: round(a / b * 100, 1) if b else 0  # noqa: E731

    kpis = [
        _kpi("orders", _("Work orders"), n, "number", monthly=[r["created"] for r in monthly], hint=_("{0} completed").format(len(done_rows))),
        _kpi("completion", _("Completion rate"), pct(len(done_rows), n), "percent", hint=_("Completed or closed")),
        _kpi("open", _("Open work orders"), len(open_rows), "number", invert=True,
             hint=_("Oldest {0} days").format(max((r["age"] or 0) for r in open_rows)) if open_rows else _("None open")),
        _kpi("ontime", _("On-time completion"), pct(len(timed) - len(late_rows), len(timed)), "percent", monthly=[r["ontime"] for r in monthly],
             hint=_("Finished by the expected delivery date")),
        _kpi("late_days", _("Average lateness"), avg([r["late"] for r in late_rows]), "number", invert=True, hint=_("Days past due, late orders only")),
        _kpi("start_delay", _("Start delay"), avg([r["start_delay"] for r in rows if r["start_delay"] is not None and r["start_delay"] >= 0]), "number",
             invert=True, hint=_("Average days, planned → actual start")),
        _kpi("achievement", _("Quantity achievement"), pct(sum(r["produced"] for r in rows), sum(r["qty"] for r in rows)), "percent",
             hint=_("Produced ÷ planned quantity")),
        _kpi("yield_var", _("Yield vs target"), avg(yv_all), "number", hint=_("Average points, actual − target yield")),
        _kpi("ops_var", _("OPS vs target"), avg(ops_all), "percent", hint=_("Average % difference, actual vs target OPS")),
        _kpi("spindles", _("Spindle allocation"), pct(alloc, req_), "percent", hint=_("Allocated ÷ required spindles")),
        _kpi("downtime", _("Orders with downtime"), len(dt_rows), "number", invert=True,
             hint=_("{0} hours lost").format(round(sum(r["downtime_h"] for r in dt_rows)))),
        _kpi("conversion", _("Conversion orders"), pct(streams.get("Conversion", {}).get("n", 0), n), "percent", hint=_("Third-party share of work orders")),
    ]
    widgets = [
        _w("flow", _("Work orders created vs completed"), "combo", monthly, _("By work-order month, with on-time %"), span=2, dualAxis=True,
           series=[{"key": "created", "label": _("Created"), "color": "hsl(215 16% 65%)"}, {"key": "completed", "label": _("Completed"), "color": "hsl(173 80% 36%)"},
                   {"key": "ontime", "label": _("On-time %"), "type": "line", "axis": "right", "color": "hsl(35 92% 50%)"}]),
        _w("status", _("Work order status"), "donut", [{"label": k, "value": v} for k, v in Counter(r["status"] for r in rows).most_common()]),
        _w("streams", _("Mill 1 · Mill 2 · Conversion"), "bar",
           [{"stream": k, "ontime": pct(v["ot"], v["ot"] + v["late"]), "achieved": pct(v["prod"], v["qty"])} for k, v in sorted(streams.items())],
           _("On-time % and quantity achievement % by stream"), xKey="stream", percent=True, span=2,
           series=[{"key": "ontime", "label": _("On time %"), "color": "hsl(160 84% 39%)"}, {"key": "achieved", "label": _("Achieved %"), "color": "hsl(221 83% 53%)"}]),
        _w("ageing", _("Open order ageing"), "bar", [{"band": k, "n": v, "color": "hsl(351 95% 59%)" if k in ("31-60 days", "60+ days") else "hsl(35 92% 50%)" if k == "16-30 days"
                                                     else "hsl(160 84% 39%)"} for k, v in ageing.items()],
           _("Open work orders by days since creation"), xKey="band", colorKey="color", angledLabels=True, series=[{"key": "n", "label": _("Orders")}]),
        _w("lateness", _("Completion vs due date"), "bar",
           [{"band": k, "n": lateness.get(k, 0), "color": "hsl(160 84% 39%)" if k in ("Early", "On the day") else "hsl(35 92% 50%)" if "1-3" in k or "4-7" in k else "hsl(351 95% 59%)"}
            for k in ("Early", "On the day", "1-3 days late", "4-7 days late", "8-15 days late", "15+ days late")],
           _("Completed orders by days early / late"), xKey="band", colorKey="color", angledLabels=True, span=2, series=[{"key": "n", "label": _("Orders")}]),
        _w("yield_bins", _("Yield variance distribution"), "bar",
           [{"band": k, "n": yvar_bins.get(k, 0), "color": "hsl(351 95% 59%)" if k.startswith("below") else "hsl(35 92% 50%)" if k.startswith("−") else "hsl(160 84% 39%)"}
            for k in ("below −2 pts", "−2 to −1", "−1 to 0", "0 to +1", "above +1")],
           _("Orders by actual − target yield"), xKey="band", colorKey="color", angledLabels=True, series=[{"key": "n", "label": _("Orders")}]),
        _w("ops_band", _("OPS: target vs actual by count"), "bar",
           [{"band": k, "target": avg(v["tops"]), "actual": avg(v["aops"])} for k, v in sorted(bands.items()) if v["tops"]],
           _("Average output per spindle by count band"), xKey="band", span=2,
           series=[{"key": "target", "label": _("Target"), "color": "hsl(215 16% 65%)"}, {"key": "actual", "label": _("Actual"), "color": "hsl(173 80% 36%)"}]),
        _w("worst_yield", _("Items furthest below target yield"), "barlist",
           [{"label": k, "value": round(sum(v) / len(v), 2)} for k, v in sorted(item_yield.items(), key=lambda x: sum(x[1]) / len(x[1])) if len(v) >= 3 and sum(v) / len(v) < 0][:8],
           _("Average points below target (3+ orders)")),
        _w("shortfall", _("Largest quantity shortfalls"), "barlist", _pairs([{"label": k, "v": v} for k, v in item_short.items()], limit=8, other=False),
           _("Planned − produced on completed orders")),
        _w("downtime", _("Work orders with most downtime"), "barlist",
           [{"label": f"{r['wo']} · {r['item']}", "value": r["downtime_h"]} for r in sorted(dt_rows, key=lambda x: -x["downtime_h"])[:8]], _("Hours stopped"), span=2),
        _w("owners", _("Work orders by planner"), "barlist", [{"label": k, "value": v} for k, v in by_owner.most_common(6)], _("Orders created")),
        _w("open_list", _("Open and late work orders"), "table",
           sorted([r for r in rows if r["status"] not in ("Completed", "Closed") or (r["late"] or 0) > 7],
                  key=lambda x: (x["status"] in ("Completed", "Closed"), -(x["age"] or 0), -(x["late"] or 0)))[:1000],
           _("Still open, or finished more than a week late"), span=3, xKey="",
           columns=[{"key": "wo", "label": _("Work order"), "doctype_key": "_wo"}, {"key": "date", "label": _("Date")}, {"key": "item", "label": _("Item")},
                    {"key": "stream", "label": _("Stream")}, {"key": "status", "label": _("Status")}, {"key": "age", "label": _("Age (days)"), "format": "days", "align": "right"},
                    {"key": "due", "label": _("Due")}, {"key": "late", "label": _("Days late"), "format": "days", "align": "right"},
                    {"key": "achieved", "label": _("Achieved %"), "format": "percent", "align": "right"}]),
    ]
    return kpis, widgets


# ----------------------------------------------------------------------------- job card analysis
def _jc_analysis(ctx):
    """Shop-floor execution per operation: time efficiency (standard ÷ actual minutes), queue wait before an operation
    starts, process loss, operation cost (actual minutes × workstation hour rate), rework and operator hours."""
    jcs = ctx.sql(f"""select j.name, j.posting_date d, j.work_order, j.item_name, j.operation, j.workstation, j.status, j.docstatus,
            j.for_quantity qty, j.total_completed_qty done_qty, j.process_loss_qty loss, j.time_required req, j.total_time_in_mins act,
            j.hour_rate, j.expected_start_date es, j.actual_start_date ast, j.expected_end_date ee, j.actual_end_date aen,
            j.is_corrective_job_card rework, j.sequence_id
        from `tabJob Card` j where j.docstatus < 2 and j.posting_date between %(f)s and %(t)s {ctx.co('j')}""")
    logs = ctx.sql(f"""select l.employee, max(e.employee_name) name, sum(l.time_in_mins) / 60 hours, sum(l.completed_qty) qty, count(distinct l.parent) cards
        from `tabJob Card Time Log` l join `tabJob Card` j on j.name = l.parent left join `tabEmployee` e on e.name = l.employee
        where j.docstatus < 2 and j.posting_date between %(f)s and %(t)s {ctx.co('j')} and l.employee is not null group by l.employee""")
    t = getdate(ctx.t)
    hrs = lambda a, b: (a - b).total_seconds() / 3600 if a and b else None  # noqa: E731
    months = {k: {"n": 0, "req": 0.0, "act": 0.0, "cost": 0.0} for k, _l in ctx.months}
    ops, wss, rows = {}, {}, []
    status = Counter()
    for j in jcs:
        done = j.status == "Completed"
        status[j.status] += 1
        cost = flt(j.act) / 60 * flt(j.hour_rate)
        wait = hrs(j.ast, j.es)
        late_finish = hrs(j.aen, j.ee)
        timed = done and flt(j.req) > 0 and flt(j.act) > 0      # cards without a routing standard stay out of efficiency
        eff = round(flt(j.req) / flt(j.act) * 100, 1) if timed else None
        lp = round(flt(j.loss) / flt(j.qty) * 100, 2) if done and flt(j.qty) else None
        mb = months.get(str(j.d)[:7])
        o = ops.setdefault(j.operation or _("Not set"), {"n": 0, "req": 0.0, "act": 0.0, "loss": 0.0, "qty": 0.0, "wait": [], "cost": 0.0, "seq": j.sequence_id or 99, "rework": 0})
        o["n"] += 1; o["cost"] += cost; o["rework"] += j.rework
        o["seq"] = min(o["seq"], j.sequence_id or 99)
        if wait is not None and wait >= 0:
            o["wait"].append(wait)
        if done:
            o["loss"] += flt(j.loss); o["qty"] += flt(j.qty)
        if timed:
            o["req"] += flt(j.req); o["act"] += flt(j.act)
            wsd = wss.setdefault(j.workstation or _("Not set"), {"req": 0.0, "act": 0.0, "n": 0, "op": j.operation})
            wsd["req"] += flt(j.req); wsd["act"] += flt(j.act); wsd["n"] += 1
            if mb:
                mb["req"] += flt(j.req); mb["act"] += flt(j.act)
        if mb:
            mb["n"] += 1; mb["cost"] += cost
        rows.append({"_jc": "Job Card", "jc": j.name, "_wo": "Work Order", "wo": j.work_order, "date": str(j.d), "item": j.item_name, "operation": j.operation,
                     "workstation": j.workstation, "status": j.status, "qty": flt(j.qty), "completed": flt(j.done_qty), "loss": flt(j.loss), "loss_pct": lp,
                     "std_h": round(flt(j.req) / 60, 1), "act_h": round(flt(j.act) / 60, 1), "over_h": round((flt(j.act) - flt(j.req)) / 60, 1) if timed else None,
                     "efficiency": eff, "wait_h": round(wait, 1) if wait is not None else None, "late_h": round(late_finish, 1) if late_finish is not None else None,
                     "cost": round(cost, 2), "rework": "Yes" if j.rework else "No",
                     "age": (t - getdate(j.d)).days if not done else None})
    ctx.jc_rows = rows
    avg = lambda xs: round(sum(xs) / len(xs), 2) if xs else 0  # noqa: E731
    pct = lambda a, b: round(a / b * 100, 1) if b else 0  # noqa: E731
    done_rows = [r for r in rows if r["status"] == "Completed"]
    open_rows = [r for r in rows if r["status"] != "Completed"]
    req_t, act_t = sum(o["req"] for o in ops.values()), sum(o["act"] for o in ops.values())
    waits = [r["wait_h"] for r in rows if r["wait_h"] is not None and r["wait_h"] >= 0]
    on_sched = [r for r in done_rows if r["late_h"] is not None and r["efficiency"] is not None]
    over = [r for r in done_rows if (r["efficiency"] or 100) < 85]
    rework = [r for r in rows if r["rework"] == "Yes"]
    cost_t = sum(r["cost"] for r in rows)
    op_list = sorted([o for o in ops.items() if o[1]["req"]], key=lambda x: x[1]["seq"])
    monthly = [{"month": lbl, "n": months[k]["n"], "eff": pct(months[k]["req"], months[k]["act"]), "cost": round(months[k]["cost"], 2)} for k, lbl in ctx.months]

    kpis = [
        _kpi("cards", _("Job cards"), len(rows), "number", monthly=[r["n"] for r in monthly], hint=_("{0} work orders").format(len({r["wo"] for r in rows}))),
        _kpi("completion", _("Completed"), pct(len(done_rows), len(rows)), "percent", hint=_("{0} completed").format(len(done_rows))),
        _kpi("open", _("Open / in progress"), len(open_rows), "number", invert=True, hint=_("{0} on hold").format(status.get("On Hold", 0))),
        _kpi("efficiency", _("Time efficiency"), pct(req_t, act_t), "percent", monthly=[r["eff"] for r in monthly], hint=_("Standard ÷ actual minutes")),
        _kpi("overrun", _("Overrun hours"), round(max(act_t - req_t, 0) / 60), "number", invert=True, hint=_("Actual beyond standard time")),
        _kpi("wait", _("Queue wait"), avg(waits), "number", invert=True, hint=_("Average hours, expected → actual start")),
        _kpi("on_schedule", _("Finished on schedule"), pct(len([r for r in on_sched if r["late_h"] <= 4]), len(on_sched)), "percent",
             hint=_("Within 4 hours of the expected end time")),
        _kpi("loss", _("Process loss"), pct(sum(o["loss"] for o in ops.values()), sum(o["qty"] for o in ops.values())), "percent", invert=True,
             hint=_("Loss ÷ input quantity, all operations")),
        _kpi("cost", _("Operation cost"), cost_t, "money", monthly=[r["cost"] for r in monthly], hint=_("Actual minutes × hour rate")),
        _kpi("slow", _("Slow job cards"), len(over), "number", invert=True, hint=_("Below 85% time efficiency")),
        _kpi("rework", _("Rework job cards"), len(rework), "number", invert=True, hint=_("Corrective job cards")),
        _kpi("operators", _("Operators"), len(logs), "number", hint=_("{0} hours logged").format(round(sum(flt(r.hours) for r in logs)))),
    ]
    ws_eff = [{"label": f"{k} · {v['op']}", "value": pct(v["req"], v["act"])} for k, v in wss.items() if v["n"] >= 5]
    widgets = [
        _w("trend", _("Job cards and time efficiency"), "combo", monthly, _("Cards by month, with standard ÷ actual time %"), span=2, dualAxis=True,
           series=[{"key": "n", "label": _("Job cards"), "color": "hsl(215 16% 65%)"},
                   {"key": "eff", "label": _("Efficiency %"), "type": "line", "axis": "right", "color": "hsl(173 80% 36%)"}]),
        _w("status", _("Job card status"), "donut", [{"label": k, "value": v} for k, v in status.most_common()]),
        _w("op_time", _("Standard vs actual hours by operation"), "bar",
           [{"op": k, "std": round(v["req"] / 60), "actual": round(v["act"] / 60)} for k, v in op_list], _("In routing sequence, completed job cards"),
           xKey="op", span=2, angledLabels=True,
           series=[{"key": "std", "label": _("Standard"), "color": "hsl(215 16% 65%)"}, {"key": "actual", "label": _("Actual"), "color": "hsl(173 80% 36%)"}]),
        _w("op_eff", _("Efficiency by operation"), "bar",
           [{"op": k, "eff": pct(v["req"], v["act"]), "color": "hsl(160 84% 39%)" if pct(v["req"], v["act"]) >= 95 else "hsl(35 92% 50%)"
             if pct(v["req"], v["act"]) >= 88 else "hsl(351 95% 59%)"} for k, v in op_list],
           _("Standard ÷ actual, %"), xKey="op", colorKey="color", percent=True, angledLabels=True, series=[{"key": "eff", "label": _("Efficiency %")}]),
        _w("op_wait", _("Queue wait before each operation"), "bar", [{"op": k, "wait": avg(v["wait"])} for k, v in op_list],
           _("Average hours from expected to actual start — the bottleneck queue"), xKey="op", angledLabels=True, span=2,
           series=[{"key": "wait", "label": _("Hours"), "color": "hsl(35 92% 50%)"}]),
        _w("op_loss", _("Process loss by operation"), "bar", [{"op": k, "loss": pct(v["loss"], v["qty"])} for k, v in op_list],
           _("Loss ÷ input, %"), xKey="op", percent=True, angledLabels=True, series=[{"key": "loss", "label": _("Loss %"), "color": "hsl(351 95% 59%)"}]),
        _w("op_cost", _("Operation cost"), "bar", [{"op": k, "cost": round(v["cost"], 2)} for k, v in op_list], _("Actual minutes × workstation hour rate"),
           xKey="op", money=True, angledLabels=True, span=2, series=[{"key": "cost", "label": _("Cost"), "color": "hsl(262 83% 58%)"}]),
        _w("slow_ws", _("Least efficient machines"), "barlist", sorted(ws_eff, key=lambda x: x["value"])[:8], _("Efficiency %, 5+ job cards")),
        _w("operators", _("Operators by hours logged"), "barlist",
           [{"label": r.name or r.employee, "value": round(flt(r.hours))} for r in sorted(logs, key=lambda x: -flt(x.hours))[:8]], _("Hours on job card time logs"), span=2),
        _w("rework_ops", _("Rework by operation"), "barlist", [{"label": k, "value": v["rework"]} for k, v in op_list if v["rework"]], _("Corrective job cards")),
        _w("exceptions", _("Slow, late and open job cards"), "table",
           sorted([r for r in rows if r["status"] != "Completed" or (r["efficiency"] or 100) < 85 or (r["wait_h"] or 0) > 24],
                  key=lambda x: (x["status"] == "Completed", (x["efficiency"] or 999)))[:1000],
           _("Open or on hold, below 85% efficiency, or waited more than a day to start"), span=3, xKey="",
           columns=[{"key": "jc", "label": _("Job card"), "doctype_key": "_jc"}, {"key": "wo", "label": _("Work order"), "doctype_key": "_wo"},
                    {"key": "operation", "label": _("Operation")}, {"key": "workstation", "label": _("Machine")}, {"key": "status", "label": _("Status")},
                    {"key": "std_h", "label": _("Std h"), "format": "number", "align": "right"}, {"key": "act_h", "label": _("Actual h"), "format": "number", "align": "right"},
                    {"key": "efficiency", "label": _("Efficiency"), "format": "percent", "align": "right"},
                    {"key": "wait_h", "label": _("Wait h"), "format": "number", "align": "right"}]),
    ]
    return kpis, widgets


# ----------------------------------------------------------------------------- insights
# Plain-language findings derived from each module's KPIs and widgets. `level` drives the colour and the
# ordering on the executive page: critical > warning > positive > info.
LEVEL_RANK = {"critical": 0, "warning": 1, "positive": 2, "info": 3}


def _ins(level, title, text, metric=None):
    return {"level": level, "title": title, "text": text, "metric": metric}


def _pct(v, d=1):
    return f"{flt(v, d):,.{d}f}%"


def _money(v):
    v = flt(v)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"{v / div:,.2f}{suf}"
    return f"{v:,.0f}"


def _best_worst(data, key):
    rows = [r for r in data if flt(r.get(key))]
    if not rows:
        return None, None
    return max(rows, key=lambda r: flt(r[key])), min(rows, key=lambda r: flt(r[key]))


def _share_of_top(rows, total):
    if not rows or not flt(total):
        return None, 0
    top = max(rows, key=lambda r: flt(r["value"]))
    return top, flt(top["value"]) / flt(total) * 100


def _insights(module, k, w):
    out = []
    v = lambda key: flt(k[key]["value"]) if key in k else 0  # noqa: E731
    d = lambda key: k[key]["delta"] if key in k else None  # noqa: E731

    if module == "accounts":
        margin = v("margin")
        if margin < 0:
            out.append(_ins("critical", _("Operating at a loss"), _("Expenses exceed income by {0} over the period.").format(_money(-v("profit"))), _pct(margin)))
        elif margin < 5:
            out.append(_ins("warning", _("Thin net margin"), _("Net profit is only {0} of income; a small cost rise would erase it.").format(_pct(margin)), _pct(margin)))
        else:
            out.append(_ins("positive", _("Healthy net margin"), _("Net profit of {0} on income of {1}.").format(_money(v("profit")), _money(v("income"))), _pct(margin)))
        best, worst = _best_worst(w["pl"]["data"], "profit")
        if worst and flt(worst["profit"]) < 0:
            out.append(_ins("warning", _("Loss-making month"), _("{0} closed with a net loss of {1}.").format(worst["month"], _money(-flt(worst["profit"]))), worst["month"]))
        if best:
            out.append(_ins("info", _("Best month"), _("{0} delivered the highest net profit: {1}.").format(best["month"], _money(best["profit"])), best["month"]))
        days = len([r for r in w["pl"]["data"] if r["income"]]) * 30 or 1
        dso = v("receivable") / (v("income") / days) if v("income") else 0
        if dso:
            out.append(_ins("warning" if dso > 45 else "info", _("Days sales outstanding"),
                            _("Receivables of {0} equal about {1} days of income.").format(_money(v("receivable")), round(dso)), f"{round(dso)} d"))
        aged = sum(flt(r["receivable"]) for r in w["ageing"]["data"] if r["bucket"] in ("61-90",) + OVERDUE_90)
        if v("receivable") and aged / v("receivable") > 0.2:
            out.append(_ins("critical", _("Old receivables"), _("{0} of receivables is more than 60 days overdue.").format(_pct(aged / v("receivable") * 100)), _money(aged)))
        if v("cash") < v("payable") * 0.25:
            out.append(_ins("warning", _("Tight liquidity"), _("Cash & bank covers only {0} of what is owed to suppliers.").format(_pct(v("cash") / v("payable") * 100 if v("payable") else 0)), _money(v("cash"))))

    elif module == "sales":
        top, share = _share_of_top(w["customers"]["data"], v("sales"))
        if top:
            out.append(_ins("warning" if share > 25 else "info", _("Customer concentration"),
                            _("{0} accounts for {1} of net sales.").format(top["label"], _pct(share)), _pct(share)))
        coll = v("collected") / v("sales") * 100 if v("sales") else 0
        if coll > 110:
            out.append(_ins("info", _("Collections above invoicing"),
                            _("Receipts were {0} of the period's invoicing — they include payments for earlier invoices and advances.").format(_pct(coll)), _pct(coll)))
        else:
            out.append(_ins("positive" if coll >= 95 else "warning", _("Cash conversion"),
                            _("Collections were {0} of the amount invoiced in the period.").format(_pct(coll)), _pct(coll)))
        if d("sales") is not None:
            lvl = "positive" if d("sales") > 0 else "warning"
            out.append(_ins(lvl, _("Sales momentum"), _("Last month's sales moved {0} against the month before.").format(_pct(d("sales"))), _pct(d("sales"))))
        best, _w = _best_worst(w["trend"]["data"], "v")
        if best:
            out.append(_ins("info", _("Peak sales month"), _("{0} was the strongest month at {1}.").format(best["month"], _money(best["v"])), best["month"]))
        if d("rate") is not None and abs(d("rate")) >= 3:
            out.append(_ins("positive" if d("rate") > 0 else "warning", _("Price per unit"),
                            _("Average rate per unit changed {0} month on month.").format(_pct(d("rate"))), _money(v("rate"))))

    elif module == "purchase":
        top, share = _share_of_top(w["suppliers"]["data"], v("purchases"))
        if top:
            out.append(_ins("warning" if share > 30 else "info", _("Supplier dependence"),
                            _("{0} supplies {1} of invoiced purchases.").format(top["label"], _pct(share)), _pct(share)))
        grp = w["groups"]["data"]
        if grp:
            g = grp[0]
            out.append(_ins("info", _("Largest spend category"), _("{0}: {1} of spend.").format(g["label"], _pct(flt(g["value"]) / sum(flt(x["value"]) for x in grp) * 100)), _money(g["value"])))
        if v("payable") > v("purchases") * 0.25:
            out.append(_ins("warning", _("High payables"), _("Unpaid supplier bills are {0} of the period's purchases.").format(_pct(v("payable") / v("purchases") * 100 if v("purchases") else 0)), _money(v("payable"))))

    elif module == "stock":
        if v("turnover"):
            out.append(_ins("positive" if v("turnover") >= 6 else "warning", _("Stock turnover"),
                            _("Stock turned over {0}× in the period (cost of goods delivered ÷ current stock value).").format(flt(v("turnover"), 1)), f"{flt(v('turnover'), 1)}×"))
        net = v("inward") - v("outward")
        out.append(_ins("info" if net <= 0 else "warning", _("Inventory build-up") if net > 0 else _("Inventory drawn down"),
                        _("Inward exceeded outward by {0}.").format(_money(net)) if net > 0 else _("Outward exceeded inward by {0}.").format(_money(-net)), _money(abs(net))))

    elif module == "hr":
        rate = v("attendance")
        out.append(_ins("critical" if rate < 60 else "warning" if rate < 85 else "positive", _("Attendance rate"),
                        _("{0} of marked attendance was present; {1} absences recorded.").format(_pct(rate), f"{int(v('absent')):,}"), _pct(rate)))
        if v("attrition") > 3:
            out.append(_ins("critical" if v("attrition") > 20 else "warning", _("Attrition"),
                            _("{0} of the workforce left in the period ({1} leavers).").format(_pct(v("attrition")), int(v("leavers"))), _pct(v("attrition"))))
        if v("joiners") or v("leavers"):
            net = int(v("joiners") - v("leavers"))
            out.append(_ins("warning" if net < 0 else "positive", _("Net headcount change"),
                            _("{0} joined and {1} left: a net {2} of {3}.").format(int(v("joiners")), int(v("leavers")),
                                                                                  _("loss") if net < 0 else _("gain"), abs(net)), f"{net:+d}"))
            peak = max(w["turnover"]["data"], key=lambda r: flt(r["left"]), default=None)
            if peak and flt(peak["left"]):
                out.append(_ins("info", _("Peak exits"), _("{0} saw the most leavers ({1}).").format(peak["month"], int(peak["left"])), peak["month"]))
        dept = w["dept"]["data"]
        if dept:
            out.append(_ins("info", _("Largest department"), _("{0} has {1} active employees.").format(dept[0]["label"], int(dept[0]["value"])), str(int(dept[0]["value"]))))
        if v("female") < 5:
            out.append(_ins("info", _("Gender balance"), _("Women are {0} of active staff.").format(_pct(v("female"))), _pct(v("female"))))

    elif module == "payroll":
        if d("gross") is not None:
            out.append(_ins("warning" if d("gross") > 10 else "info", _("Payroll cost trend"),
                            _("Last month's gross pay moved {0} against the month before.").format(_pct(d("gross"))), _pct(d("gross"))))
        out.append(_ins("warning" if v("ded_ratio") > 30 else "info", _("Deductions"),
                        _("Deductions take {0} of gross pay.").format(_pct(v("ded_ratio"))), _pct(v("ded_ratio"))))
        dept = w["dept"]["data"]
        if dept:
            out.append(_ins("info", _("Costliest department"), _("{0}: {1} gross pay.").format(dept[0]["label"], _money(dept[0]["value"])), _money(dept[0]["value"])))

    elif module == "production":
        ach = v("achievement")
        out.append(_ins("positive" if ach >= 97 else "warning" if ach >= 90 else "critical", _("Plan achievement"),
                        _("{0} of planned quantity was produced.").format(_pct(ach)), _pct(ach)))
        tgt = w["yield"]["data"]
        tgt_avg = [flt(r["target"]) for r in tgt if flt(r["target"])]
        tgt_avg = sum(tgt_avg) / len(tgt_avg) if tgt_avg else 0
        if tgt_avg:
            gap = v("yield") - tgt_avg
            out.append(_ins("positive" if gap >= 0 else "warning", _("Yield vs target"),
                            _("Actual yield {0} against a {1} target ({2:+.2f} pts).").format(_pct(v("yield"), 2), _pct(tgt_avg, 2), gap), _pct(v("yield"), 2)))
        ops = [r for r in w["ops"]["data"] if flt(r["target"])]
        if ops:
            t_ops = sum(flt(r["target"]) for r in ops) / len(ops)
            gap = (v("ops") - t_ops) / t_ops * 100 if t_ops else 0
            out.append(_ins("warning" if gap < -3 else "positive", _("Spindle productivity"),
                            _("Average OPS {0} vs target {1} ({2:+.1f}%).").format(flt(v("ops"), 2), flt(t_ops, 2), gap), flt(v("ops"), 2)))
        reasons = w["reasons"]["data"]
        if reasons:
            r0 = reasons[0]
            out.append(_ins("warning", _("Main cause of downtime"),
                            _("{0}: {1} hours, {2} of all stoppage time.").format(r0["label"], round(flt(r0["value"])), _pct(flt(r0["value"]) / sum(flt(x["value"]) for x in reasons) * 100)), f"{round(flt(r0['value']))} h"))
        if d("downtime") is not None and d("downtime") > 50:
            out.append(_ins("critical", _("Downtime spike"), _("Stoppage hours rose {0} last month.").format(_pct(d("downtime"))), _pct(d("downtime"))))

    elif module == "assets":
        left = v("nbv") / v("gross") * 100 if v("gross") else 0
        out.append(_ins("warning" if left < 40 else "info", _("Age of the asset base"),
                        _("{0} of the original cost is still on the books; the fleet is {1}.").format(_pct(left), _("ageing") if left < 40 else _("relatively new")), _pct(left)))
        if v("depreciation") and v("posted") < 98:
            out.append(_ins("warning", _("Unposted depreciation"),
                            _("Only {0} of scheduled depreciation has been booked to the ledger.").format(_pct(v("posted"))), _pct(v("posted"))))
        elif v("depreciation"):
            out.append(_ins("positive", _("Depreciation up to date"),
                            _("{0} of scheduled depreciation ({1}) is booked to the ledger.").format(_pct(v("posted")), _money(v("depreciation"))), _pct(v("posted"))))
        cats = w["cat"]["data"]
        if cats and v("nbv"):
            out.append(_ins("info", _("Largest asset class"), _("{0} holds {1} of book value.").format(cats[0]["label"], _pct(flt(cats[0]["value"]) / v("nbv") * 100)), _money(cats[0]["value"])))
        if v("drafts"):
            out.append(_ins("warning", _("Assets not capitalised"), _("{0} asset records are still in Draft, so they are not depreciating.").format(int(v("drafts"))), str(int(v("drafts")))))
        if v("down"):
            out.append(_ins("warning", _("Idle machinery"), (_("1 asset is out of order or under maintenance.") if int(v("down")) == 1 else _("{0} assets are out of order or under maintenance.").format(int(v("down")))), str(int(v("down")))))

    elif module == "financials":
        rev = v("revenue")
        gm = v("gross_profit") / rev * 100 if rev else 0
        out.append(_ins("warning" if gm < 10 else "positive", _("Gross margin"),
                        _("After cost of sales, {0} of revenue is left to cover overheads.").format(_pct(gm)), _pct(gm)))
        cr = v("current_ratio")
        if cr:
            out.append(_ins("critical" if cr < 1 else "warning" if cr < 1.2 else "positive", _("Liquidity (current ratio)"),
                            _("Current assets cover current liabilities {0}×.").format(flt(cr, 2)), f"{flt(cr, 2)}×"))
        if v("total_assets"):
            lev = v("total_liabilities") / v("total_assets") * 100
            out.append(_ins("warning" if lev > 70 else "info", _("Leverage"),
                            _("Liabilities fund {0} of total assets.").format(_pct(lev)), _pct(lev)))
        out.append(_ins("positive" if v("net_cash") >= 0 else "warning", _("Cash generation"),
                        _("Bank & cash {0} by {1} over the period.").format(_("grew") if v("net_cash") >= 0 else _("fell"), _money(abs(v("net_cash")))),
                        _money(v("net_cash"))))
        gap = v("total_assets") - v("total_liabilities") - v("equity")
        if abs(gap) > 1:
            out.append(_ins("warning", _("Balance sheet difference"), _("Assets differ from liabilities + equity by {0}.").format(_money(gap)), _money(gap)))

    elif module == "procurement":
        stages = {_("Request → order"): v("mr_po"), _("Order → receipt"): v("po_pr"), _("Receipt → invoice"): v("pr_pi")}
        slow = max(stages, key=stages.get)
        if v("cycle"):
            out.append(_ins("info", _("Slowest stage"), _("{0} takes {1} of the {2}-day average buying cycle.").format(
                slow, _pct(stages[slow] / v("cycle") * 100), flt(v("cycle"), 1)), f"{flt(stages[slow], 1)} d"))
        ot = v("ontime")
        out.append(_ins("positive" if ot >= 90 else "warning" if ot >= 75 else "critical", _("Supplier delivery"),
                        _("{0} of PO lines were first received by their required-by date.").format(_pct(ot)), _pct(ot)))
        rate = v("ppv_rate")
        out.append(_ins("warning" if rate > 1 else "positive" if rate < -1 else "info", _("Purchase price variance"),
                        _("Paid {0} {1} standard rates overall ({2}).").format(_pct(abs(rate)), _("above") if rate > 0 else _("below"), _money(v("ppv"))), _pct(rate)))
        if v("above_tol"):
            out.append(_ins("warning", _("Rates above tolerance"), _("{0} PO lines were priced more than the tolerance above standard.").format(int(v("above_tol"))),
                            str(int(v("above_tol")))))
        ab = w["abnormal"]["data"]
        if ab:
            out.append(_ins("warning" if flt(ab[0]["value"]) > 0 else "info", _("Largest vendor rate gap"),
                            _("{0}: {1} from standard on average.").format(ab[0]["label"], _pct(ab[0]["value"])), _pct(ab[0]["value"])))
        if v("act_days") or v("plan_days"):
            gap = v("act_days") - v("plan_days")
            out.append(_ins("positive" if gap <= 0 else "warning", _("Lead time vs schedule"),
                            _("Goods arrive in {0} days on average against {1} scheduled ({2:+.1f} days).").format(flt(v("act_days"), 1), flt(v("plan_days"), 1), gap),
                            f"{flt(v('act_days'), 1)} d"))
        if v("quick"):
            qs = w["quick_sup"]["data"]
            out.append(_ins("info", _("Quick purchases"),
                            _("{0} PO lines were received within a day of ordering{1}.").format(
                                int(v("quick")), _(" — {0} supplies the most").format(qs[0]["label"]) if qs else ""), str(int(v("quick")))))
        if v("odd"):
            reasons = Counter(o["reason"].split(" ")[0] for o in w["odd_list"]["data"])
            top_r = {"Not": _("never received"), "Required-by": _("back-dated required-by"), "Lead": _("very long lead time"),
                     "Received": _("very late receipt")}.get(reasons.most_common(1)[0][0], "") if reasons else ""
            out.append(_ins("warning", _("Odd purchase lines"), _("{0} PO lines fall outside the normal pattern; most common: {1}.").format(int(v("odd")), top_r),
                            str(int(v("odd")))))
        wl = w.get("wrong_users", {}).get("data") or []
        if wl:
            top_u = wl[0]
            out.append(_ins("warning", _("Out-of-order cycle dates"),
                            _("{0} created {1} documents dated before the step they follow — the most of any user.").format(top_u["user"], top_u["n"]),
                            str(top_u["n"])))
        if v("suspect"):
            out.append(_ins("warning", _("Suspect purchase rates"),
                            _("{0} PO lines are more than ±200% off the item's standard rate — usually a wrong UOM or price entry. They are excluded from PPV.").format(int(v("suspect"))),
                            str(int(v("suspect")))))
        if v("received_pct") < 95:
            out.append(_ins("warning", _("Open purchase orders"), _("Only {0} of ordered quantity has been received.").format(_pct(v("received_pct"))), _pct(v("received_pct"))))

    elif module == "so_analysis":
        o = v("otif")
        out.append(_ins("positive" if o >= 90 else "warning" if o >= 70 else "critical", _("On time in full"),
                        _("{0} of completed order lines were fully delivered by the promised date.").format(_pct(o)), _pct(o)))
        if v("overdue"):
            out.append(_ins("critical" if v("overdue") > 20 else "warning", _("Overdue orders"),
                            _("{0} open order lines are past their promised date.").format(int(v("overdue"))), str(int(v("overdue")))))
        if v("open_book"):
            top = sorted(w["customers"]["data"], key=lambda x: -flt(x["pending"]))
            out.append(_ins("info", _("Open order book"), _("{0} is still to be delivered{1}.").format(
                _money(v("open_book")), _(" — {0} has the most waiting").format(top[0]["c"]) if top and flt(top[0]["pending"]) else ""), _money(v("open_book"))))
        gap = v("lead") - v("promise")
        out.append(_ins("positive" if gap <= 0 else "warning", _("Delivery speed vs promise"),
                        _("First delivery comes {0} days after ordering against {1} promised ({2:+.1f}).").format(flt(v("lead"), 1), flt(v("promise"), 1), gap),
                        f"{flt(v('lead'), 1)} d"))
        if v("delivered_pct") < 95:
            out.append(_ins("warning", _("Fulfilment"), _("Only {0} of ordered quantity has been delivered.").format(_pct(v("delivered_pct"))), _pct(v("delivered_pct"))))

    elif module == "do_analysis":
        o = v("ontime")
        out.append(_ins("positive" if o >= 90 else "warning" if o >= 70 else "critical", _("Delivery timeliness"),
                        _("{0} of delivery lines left by the promised date; late ones averaged {1} days.").format(_pct(o), flt(v("late_days"), 1)), _pct(o)))
        if v("unbilled"):
            out.append(_ins("warning", _("Delivered but not invoiced"), _("{0} of goods have gone out without an invoice.").format(_money(v("unbilled"))),
                            _money(v("unbilled"))))
        out.append(_ins("positive" if v("to_invoice") <= 2 else "warning", _("Billing speed"),
                        _("Invoices follow deliveries after {0} days on average; {1} are invoiced the same day.").format(flt(v("to_invoice"), 1), _pct(v("same_day_inv"))),
                        f"{flt(v('to_invoice'), 1)} d"))
        wd = w["weekday"]["data"]
        wd_total = sum(x["v"] for x in wd)
        if wd_total:  # the list always has 7 days; with no deliveries every count is 0
            peak = max(wd, key=lambda x: x["v"])
            out.append(_ins("info", _("Busiest dispatch day"), _("{0} carries {1} of delivery lines.").format(peak["day"], _pct(peak["v"] / wd_total * 100)), peak["day"]))
        if v("against_so") < 98:
            out.append(_ins("warning", _("Deliveries without an order"), _("{0} of delivery lines are not linked to a sales order.").format(_pct(100 - v("against_so"))),
                            _pct(100 - v("against_so"))))

    elif module == "export_analysis":
        cs = w["countries"]["data"]
        tot = sum(flt(x["value"]) for x in cs)
        if cs and tot:
            share = flt(cs[0]["value"]) / tot * 100
            out.append(_ins("warning" if share > 40 else "info", _("Destination concentration"),
                            _("{0} takes {1} of export orders; {2} destinations in total.").format(cs[0]["label"], _pct(share), len(cs)), _pct(share)))
        out.append(_ins("info", _("Export share"), _("Exports are {0} of order value ({1}).").format(_pct(v("export_share")), _money(v("export_value"))),
                        _pct(v("export_share"))))
        if v("lc_expiring"):
            out.append(_ins("critical", _("LCs expired or about to expire"),
                            _("{0} open LCs have expired or expire within 30 days — ship, amend or close them.").format(int(v("lc_expiring"))),
                            str(int(v("lc_expiring")))))
        o = v("ontime_lc")
        out.append(_ins("positive" if o >= 90 else "warning", _("Shipping within LC terms"),
                        _("{0} of shipments left by the LC's latest shipment date.").format(_pct(o)), _pct(o)))
        cb = [x for x in w["country_bar"]["data"] if flt(x["transit"])]
        if cb:
            slow = max(cb, key=lambda x: flt(x["transit"]))
            out.append(_ins("info", _("Longest route"), _("{0}: {1} days' average transit.").format(slow["country"], slow["transit"]), f"{slow['transit']} d"))

    elif module == "import_analysis":
        out.append(_ins("warning" if v("uplift") > 25 else "info", _("Landing uplift"),
                        _("Imports land at {0} above purchase value; non-refundable duties are {1}.").format(_pct(v("uplift")), _money(v("duty_tax"))), _pct(v("uplift"))))
        if v("adjustable"):
            out.append(_ins("positive", _("Recoverable import taxes"),
                            _("{0} of sales tax (s.7 STA) and s.148 income tax was paid at import — claim it in the sales tax return and the annual income tax return; it is not stock cost.").format(_money(v("adjustable"))),
                            _money(v("adjustable"))))
        ch = sorted(w["charges"]["data"], key=lambda x: -flt(x["value"]))
        if ch:
            out.append(_ins("info", _("Biggest add-on"), _("{0} is the largest cost added to imported stock ({1}).").format(ch[0]["label"], _money(ch[0]["value"])), _money(ch[0]["value"])))
        dw = v("dwell")
        out.append(_ins("warning" if dw > 5 else "positive", _("Port dwell"), _("Containers wait {0} days on average between arrival and clearance.").format(flt(dw, 1)),
                        f"{flt(dw, 1)} d"))
        if v("late"):
            out.append(_ins("warning", _("Delayed arrivals"), _("{0} shipments arrived more than 2 days after ETA.").format(int(v("late"))), str(int(v("late")))))
        pt = [x for x in w["port_times"]["data"] if flt(x["transit"])]
        if pt:
            slow = max(pt, key=lambda x: flt(x["transit"]) + flt(x["dwell"]))
            out.append(_ins("info", _("Slowest origin"), _("{0}: {1} days at sea + {2} at port.").format(slow["port"], slow["transit"], slow["dwell"]), slow["port"]))

    elif module == "quality":
        a = v("acceptance")
        out.append(_ins("positive" if a >= 97 else "warning" if a >= 93 else "critical", _("Overall acceptance"),
                        _("{0} of {1} inspected lots passed every parameter.").format(_pct(a), int(v("inspections"))), _pct(a)))
        tr = sorted(w["tpl_rate"]["data"], key=lambda x: -flt(x["rate"]))
        if tr and flt(tr[0]["rate"]):
            out.append(_ins("warning" if flt(tr[0]["rate"]) > 3 else "info", _("Weakest test"), _("{0} rejects {1} of its lots.").format(tr[0]["tpl"], _pct(tr[0]["rate"])),
                            _pct(tr[0]["rate"])))
        pf = w["params"]["data"]
        if pf:
            out.append(_ins("info", _("Most frequent failure"), _("{0} was out of limit {1} times.").format(pf[0]["label"], int(pf[0]["value"])), str(int(pf[0]["value"]))))
        sp = w["suppliers"]["data"]
        if sp:
            out.append(_ins("warning", _("Supplier quality"), _("{0} has the highest fibre rejection rate ({1}).").format(sp[0]["label"], _pct(sp[0]["value"])),
                            _pct(sp[0]["value"])))
        if v("open_nc"):
            out.append(_ins("warning", _("Open non-conformances"), _("{0} non-conformances still need corrective action.").format(int(v("open_nc"))), str(int(v("open_nc")))))
        gs = [g for g in w["goals"]["data"] if g["failed"]]
        if gs:
            g = max(gs, key=lambda x: x["failed"])
            out.append(_ins("critical" if g["failed"] > 6 else "warning", _("Goal at risk"),
                            _("'{0}' missed its target in {1} of {2} months.").format(g["goal"], g["failed"], g["failed"] + g["passed"]), f"{g['failed']}/{g['failed'] + g['passed']}"))
        u = v("u_pct")
        if u:
            out.append(_ins("positive" if u <= 11 else "warning", _("Yarn evenness"), _("Average U% is {0} against a limit of 12.").format(flt(u, 2)), f"{flt(u, 2)}"))

    elif module == "wo_analysis":
        ot = v("ontime")
        out.append(_ins("positive" if ot >= 90 else "warning" if ot >= 75 else "critical", _("On-time completion"),
                        _("{0} of completed work orders finished by their due date; late ones ran {1} days over.").format(_pct(ot), flt(v("late_days"), 1)), _pct(ot)))
        if v("open"):
            ag = {r["band"]: r["n"] for r in w["ageing"]["data"]}
            old = ag.get("31-60 days", 0) + ag.get("60+ days", 0)
            out.append(_ins("warning" if old else "info", _("Open work orders"),
                            _("{0} orders are still open, {1} of them older than a month.").format(int(v("open")), old), str(int(v("open")))))
        st = w["streams"]["data"]
        if len(st) > 1:
            worst = min(st, key=lambda x: flt(x["ontime"]))
            best = max(st, key=lambda x: flt(x["ontime"]))
            out.append(_ins("info", _("Stream comparison"), _("{0} is on time {1} of the time vs {2} for {3}.").format(
                worst["stream"], _pct(worst["ontime"]), _pct(best["ontime"]), best["stream"]), worst["stream"]))
        wy = w["worst_yield"]["data"]
        if wy:
            out.append(_ins("warning", _("Yield problem item"), _("{0} runs {1} points below target yield on average.").format(wy[0]["label"], abs(flt(wy[0]["value"]))),
                            f"{flt(wy[0]['value'])} pts"))
        if v("ops_var") < -2:
            out.append(_ins("warning", _("Spindle productivity"), _("Work orders average {0} below target OPS.").format(_pct(abs(v("ops_var")))), _pct(v("ops_var"))))
        if v("downtime"):
            out.append(_ins("info", _("Downtime on orders"), _("{0} work orders lost production to stoppages.").format(int(v("downtime"))), str(int(v("downtime")))))

    elif module == "jc_analysis":
        eff = v("efficiency")
        out.append(_ins("positive" if eff >= 95 else "warning" if eff >= 85 else "critical", _("Time efficiency"),
                        _("Job cards ran at {0} of standard time, {1} hours over the routing standard.").format(_pct(eff), int(v("overrun"))), _pct(eff)))
        oe = [r for r in w["op_eff"]["data"] if r["eff"]]
        if oe:
            worst = min(oe, key=lambda x: x["eff"])
            out.append(_ins("warning" if worst["eff"] < 90 else "info", _("Bottleneck operation"),
                            _("{0} is the slowest step at {1} efficiency.").format(worst["op"], _pct(worst["eff"])), worst["op"]))
        qw = [r for r in w["op_wait"]["data"] if r["wait"]]
        if qw:
            q = max(qw, key=lambda x: x["wait"])
            out.append(_ins("warning" if q["wait"] > 6 else "info", _("Longest queue"),
                            _("Work waits {0} hours on average before {1} starts.").format(flt(q["wait"], 1), q["op"]), f"{flt(q['wait'], 1)} h"))
        sw = w["slow_ws"]["data"]
        if sw and sw[0]["value"] < 90:
            out.append(_ins("warning", _("Machine to check"), _("{0} runs at only {1} of standard time.").format(sw[0]["label"], _pct(sw[0]["value"])),
                            _pct(sw[0]["value"])))
        ol = [r for r in w["op_loss"]["data"] if r["loss"]]
        if ol:
            l = max(ol, key=lambda x: x["loss"])
            out.append(_ins("info", _("Highest process loss"), _("{0} loses {1} of its input.").format(l["op"], _pct(l["loss"])), _pct(l["loss"])))
        if v("rework"):
            out.append(_ins("warning", _("Rework"), _("{0} corrective job cards were raised.").format(int(v("rework"))), str(int(v("rework")))))
        if v("open"):
            out.append(_ins("info", _("Open job cards"), _("{0} job cards are open or in progress.").format(int(v("open"))), str(int(v("open")))))

    for i in out:
        i["module"] = module
    return sorted(out, key=lambda i: LEVEL_RANK[i["level"]])


# ----------------------------------------------------------------------------- KPI drill-down
# Clicking a KPI tile in the SPA lists the documents behind it for the same period / company.
# Each spec returns (title, columns, rows). Columns: key, label, optional align / format
# (money | number | percent | days) and `doctype` (static) or `doctype_key` (per row) to link the cell.
DRILL_LIMIT = 2000


def _c(key, label, fmt=None, doctype=None, doctype_key=None, align=None):
    col = {"key": key, "label": label}
    if fmt:
        col["format"] = fmt
        col["align"] = "right"
    if align:
        col["align"] = align
    if doctype:
        col["doctype"] = doctype
    if doctype_key:
        col["doctype_key"] = doctype_key
    return col


def _d_sales_invoices(ctx, outstanding=False):
    cond = "outstanding_amount > 0 and posting_date <= %(t)s" if outstanding else "posting_date between %(f)s and %(t)s"
    rows = ctx.sql(f"""select name, posting_date, customer, total_qty, base_net_total, base_grand_total, outstanding_amount, status, due_date
        from `tabSales Invoice` where docstatus = 1 and {cond} {ctx.co()} order by {'outstanding_amount' if outstanding else 'base_net_total'} desc limit {DRILL_LIMIT}""")
    return (_("Unpaid sales invoices") if outstanding else _("Sales invoices"),
            [_c("name", _("Invoice"), doctype="Sales Invoice"), _c("posting_date", _("Date")), _c("customer", _("Customer")),
             _c("total_qty", _("Qty"), "number"), _c("base_net_total", _("Net"), "money"), _c("base_grand_total", _("Grand total"), "money"),
             _c("outstanding_amount", _("Outstanding"), "money"), _c("due_date", _("Due")), _c("status", _("Status"))], rows)


def _d_purchase_invoices(ctx, outstanding=False):
    cond = "outstanding_amount > 0 and posting_date <= %(t)s" if outstanding else "posting_date between %(f)s and %(t)s"
    rows = ctx.sql(f"""select name, posting_date, supplier, bill_no, total_qty, base_net_total, base_total_taxes_and_charges tax,
            base_grand_total, outstanding_amount, status, due_date
        from `tabPurchase Invoice` where docstatus = 1 and {cond} {ctx.co()} order by {'outstanding_amount' if outstanding else 'base_net_total'} desc limit {DRILL_LIMIT}""")
    return (_("Unpaid purchase invoices") if outstanding else _("Purchase invoices"),
            [_c("name", _("Invoice"), doctype="Purchase Invoice"), _c("posting_date", _("Date")), _c("supplier", _("Supplier")), _c("bill_no", _("Bill no")),
             _c("base_net_total", _("Net"), "money"), _c("tax", _("Tax"), "money"), _c("base_grand_total", _("Grand total"), "money"),
             _c("outstanding_amount", _("Outstanding"), "money"), _c("due_date", _("Due")), _c("status", _("Status"))], rows)


def _d_payments(ctx, kind):
    party = "Customer" if kind == "Receive" else "Supplier"
    amt = "base_received_amount" if kind == "Receive" else "base_paid_amount"
    rows = ctx.sql(f"""select name, posting_date, party, mode_of_payment, reference_no, {amt} amount,
            ifnull(paid_to, '') paid_to, ifnull(paid_from, '') paid_from
        from `tabPayment Entry` where docstatus = 1 and payment_type = %(k)s and party_type = %(p)s
          and posting_date between %(f)s and %(t)s {ctx.co()} order by {amt} desc limit {DRILL_LIMIT}""", {"k": kind, "p": party})
    return (_("Receipts from customers") if kind == "Receive" else _("Payments to suppliers"),
            [_c("name", _("Payment"), doctype="Payment Entry"), _c("posting_date", _("Date")), _c("party", party), _c("mode_of_payment", _("Mode")),
             _c("reference_no", _("Reference")), _c("amount", _("Amount"), "money"), _c("paid_to" if kind == "Receive" else "paid_from", _("Account"))], rows)


def _d_gl_accounts(ctx, root_types, title, balance_sheet=False):
    cond = "g.posting_date <= %(t)s" if balance_sheet else "g.posting_date between %(f)s and %(t)s and g.voucher_type != 'Period Closing Voucher'"
    rows = ctx.sql(f"""select * from (
            select g.account, a.root_type, a.account_type, sum(g.debit) debit, sum(g.credit) credit,
                   sum(case when a.root_type in ('Asset', 'Expense') then g.debit - g.credit else g.credit - g.debit end) amount, count(*) postings
            from `tabGL Entry` g join `tabAccount` a on a.name = g.account
            where g.is_cancelled = 0 and {cond} {ctx.co('g')} and a.root_type in %(rt)s
            group by g.account, a.root_type, a.account_type) x
        where abs(x.amount) > 0.5 order by abs(x.amount) desc limit {DRILL_LIMIT}""", {"rt": tuple(root_types)})
    return (title, [_c("account", _("Account")), _c("root_type", _("Type")), _c("account_type", _("Account type")),
                    _c("debit", _("Debit"), "money"), _c("credit", _("Credit"), "money"), _c("amount", _("Balance"), "money"),
                    _c("postings", _("Postings"), "number")], rows)


def _d_cash_accounts(ctx):
    rows = ctx.sql(f"""select g.account, a.account_type,
            sum(case when g.posting_date < %(f)s then g.debit - g.credit else 0 end) opening,
            sum(case when g.posting_date >= %(f)s then g.debit else 0 end) cash_in,
            sum(case when g.posting_date >= %(f)s then g.credit else 0 end) cash_out,
            sum(g.debit - g.credit) closing
        from `tabGL Entry` g join `tabAccount` a on a.name = g.account
        where g.is_cancelled = 0 and g.posting_date <= %(t)s {ctx.co('g')} and a.account_type in ('Bank', 'Cash')
        group by g.account having abs(closing) > 0.5 or abs(cash_in) > 0.5 order by closing desc""")
    return (_("Bank & cash accounts"), [_c("account", _("Account")), _c("account_type", _("Type")), _c("opening", _("Opening"), "money"),
                                        _c("cash_in", _("In"), "money"), _c("cash_out", _("Out"), "money"), _c("closing", _("Closing"), "money")], rows)


def _d_orders(ctx, doctype):
    party = "customer" if doctype == "Sales Order" else "supplier"
    pct = ("per_delivered", _("Delivered %")) if doctype == "Sales Order" else ("per_received", _("Received %"))
    rows = ctx.sql(f"""select name, transaction_date, {party} party, total_qty, base_net_total, base_grand_total, {pct[0]} p1, per_billed, status
        from `tab{doctype}` where docstatus = 1 and transaction_date between %(f)s and %(t)s {ctx.co()}
        order by base_net_total desc limit {DRILL_LIMIT}""")
    return (_("{0}s").format(doctype), [_c("name", doctype, doctype=doctype), _c("transaction_date", _("Date")), _c("party", _(party.title())),
                                        _c("total_qty", _("Qty"), "number"), _c("base_net_total", _("Net"), "money"),
                                        _c("base_grand_total", _("Grand total"), "money"), _c("p1", pct[1], "percent"),
                                        _c("per_billed", _("Billed %"), "percent"), _c("status", _("Status"))], rows)


def _d_receipts(ctx):
    rows = ctx.sql(f"""select name, posting_date, supplier, total_qty, base_net_total, per_billed, status from `tabPurchase Receipt`
        where docstatus = 1 and posting_date between %(f)s and %(t)s {ctx.co()} order by total_qty desc limit {DRILL_LIMIT}""")
    return (_("Purchase receipts"), [_c("name", _("Receipt"), doctype="Purchase Receipt"), _c("posting_date", _("Date")), _c("supplier", _("Supplier")),
                                     _c("total_qty", _("Qty"), "number"), _c("base_net_total", _("Net"), "money"),
                                     _c("per_billed", _("Billed %"), "percent"), _c("status", _("Status"))], rows)


def _d_bins(ctx):
    wh = " and w.company = %(co)s" if ctx.company else ""
    rows = ctx.sql(f"""select b.item_code, i.item_name, i.item_group, b.warehouse, b.actual_qty, b.valuation_rate, b.stock_value
        from `tabBin` b join `tabItem` i on i.name = b.item_code join `tabWarehouse` w on w.name = b.warehouse
        where b.actual_qty != 0 {wh} order by b.stock_value desc limit {DRILL_LIMIT}""")
    return (_("Stock on hand"), [_c("item_code", _("Item")), _c("item_name", _("Name")), _c("item_group", _("Group")), _c("warehouse", _("Warehouse")),
                                 _c("actual_qty", _("Qty"), "number"), _c("valuation_rate", _("Rate"), "money"), _c("stock_value", _("Value"), "money")], rows)


def _d_stock_vouchers(ctx, direction):
    rows = ctx.sql(f"""select s.voucher_type, s.voucher_no, s.posting_date, ifnull(se.purpose, s.voucher_type) purpose,
            sum(case when s.stock_value_difference > 0 then s.stock_value_difference else 0 end) inward,
            sum(case when s.stock_value_difference < 0 then -s.stock_value_difference else 0 end) outward, count(*) n_lines
        from `tabStock Ledger Entry` s left join `tabStock Entry` se on s.voucher_type = 'Stock Entry' and se.name = s.voucher_no
        where s.is_cancelled = 0 and s.posting_date between %(f)s and %(t)s {ctx.co('s')}
          and ifnull(se.purpose, '') not in ('Material Transfer', 'Material Transfer for Manufacture')
        group by s.voucher_type, s.voucher_no having {direction} > 0 order by {direction} desc limit {DRILL_LIMIT}""")
    return (_("Stock received (by document)") if direction == "inward" else _("Stock issued (by document)"),
            [_c("voucher_no", _("Document"), doctype_key="voucher_type"), _c("voucher_type", _("Type")), _c("purpose", _("Purpose")),
             _c("posting_date", _("Date")), _c("inward", _("Inward value"), "money"), _c("outward", _("Outward value"), "money"),
             _c("n_lines", _("Lines"), "number")], rows)


def _d_stock_entries(ctx, manufacture=False):
    cond = " and purpose = 'Manufacture'" if manufacture else ""
    rows = ctx.sql(f"""select name, posting_date, stock_entry_type, work_order, fg_completed_qty, total_outgoing_value, total_incoming_value
        from `tabStock Entry` where docstatus = 1 and posting_date between %(f)s and %(t)s {ctx.co()} {cond}
        order by {'fg_completed_qty' if manufacture else 'total_outgoing_value'} desc limit {DRILL_LIMIT}""")
    return (_("Manufacture entries") if manufacture else _("Stock entries"),
            [_c("name", _("Entry"), doctype="Stock Entry"), _c("posting_date", _("Date")), _c("stock_entry_type", _("Type")),
             _c("work_order", _("Work order"), doctype="Work Order"), _c("fg_completed_qty", _("Finished qty"), "number"),
             _c("total_outgoing_value", _("Out value"), "money"), _c("total_incoming_value", _("In value"), "money")], rows)


def _d_items_moved(ctx):
    rows = ctx.sql(f"""select s.item_code, max(i.item_name) item_name, count(*) movements, sum(s.actual_qty) net_qty,
            sum(abs(s.stock_value_difference)) value_moved
        from `tabStock Ledger Entry` s join `tabItem` i on i.name = s.item_code
        where s.is_cancelled = 0 and s.posting_date between %(f)s and %(t)s {ctx.co('s')}
        group by s.item_code order by movements desc limit {DRILL_LIMIT}""")
    return (_("Items moved"), [_c("item_code", _("Item")), _c("item_name", _("Name")), _c("movements", _("Movements"), "number"),
                               _c("net_qty", _("Net qty"), "number"), _c("value_moved", _("Value moved"), "money")], rows)


def _d_employees(ctx, where, title):
    rows = ctx.sql(f"""select name, employee_name, department, designation, gender, employment_type, date_of_joining, relieving_date, status
        from `tabEmployee` where {where} {ctx.co()} order by department, employee_name limit {DRILL_LIMIT}""")
    return (title, [_c("name", _("Employee"), doctype="Employee"), _c("employee_name", _("Name")), _c("department", _("Department")),
                    _c("designation", _("Designation")), _c("gender", _("Gender")), _c("employment_type", _("Type")),
                    _c("date_of_joining", _("Joined")), _c("relieving_date", _("Left")), _c("status", _("Status"))], rows)


def _d_attendance(ctx, order):
    rows = ctx.sql(f"""select a.employee, max(a.employee_name) employee_name, max(a.department) department,
            sum(a.status = 'Present') present, sum(a.status = 'Absent') absent, sum(a.status = 'On Leave') on_leave,
            sum(a.status = 'Half Day') half_day, count(*) marked,
            round((sum(a.status = 'Present') + sum(a.status = 'Half Day') / 2) / count(*) * 100, 1) rate
        from `tabAttendance` a where a.docstatus = 1 and a.attendance_date between %(f)s and %(t)s {ctx.co('a')}
        group by a.employee order by {order} limit {DRILL_LIMIT}""")
    return (_("Attendance by employee"), [_c("employee", _("Employee"), doctype="Employee"), _c("employee_name", _("Name")), _c("department", _("Department")),
                                          _c("present", _("Present"), "number"), _c("absent", _("Absent"), "number"), _c("on_leave", _("On leave"), "number"),
                                          _c("half_day", _("Half day"), "number"), _c("rate", _("Attendance %"), "percent")], rows)


def _d_leaves(ctx):
    rows = ctx.sql(f"""select name, employee, employee_name, leave_type, from_date, to_date, total_leave_days, status
        from `tabLeave Application` where docstatus = 1 and from_date between %(f)s and %(t)s {ctx.co()} order by from_date desc limit {DRILL_LIMIT}""")
    return (_("Leave applications"), [_c("name", _("Application")), _c("employee", _("Employee"), doctype="Employee"), _c("employee_name", _("Name")),
                                      _c("leave_type", _("Type")), _c("from_date", _("From")), _c("to_date", _("To")),
                                      _c("total_leave_days", _("Days"), "number"), _c("status", _("Status"))], rows)


def _d_salary_slips(ctx, per_employee=False):
    if per_employee:
        rows = ctx.sql(f"""select employee, max(employee_name) employee_name, max(department) department, count(*) slips,
                sum(base_gross_pay) gross, sum(base_total_deduction) deductions, sum(base_net_pay) net
            from `tabSalary Slip` where docstatus = 1 and start_date between %(f)s and %(t)s {ctx.co()}
            group by employee order by gross desc limit {DRILL_LIMIT}""")
        return (_("Pay by employee"), [_c("employee", _("Employee"), doctype="Employee"), _c("employee_name", _("Name")), _c("department", _("Department")),
                                       _c("slips", _("Slips"), "number"), _c("gross", _("Gross"), "money"), _c("deductions", _("Deductions"), "money"),
                                       _c("net", _("Net"), "money")], rows)
    rows = ctx.sql(f"""select name, employee, employee_name, department, start_date, payment_days, base_gross_pay, base_total_deduction, base_net_pay
        from `tabSalary Slip` where docstatus = 1 and start_date between %(f)s and %(t)s {ctx.co()} order by base_gross_pay desc limit {DRILL_LIMIT}""")
    return (_("Salary slips"), [_c("name", _("Slip"), doctype="Salary Slip"), _c("employee_name", _("Employee")), _c("department", _("Department")),
                                _c("start_date", _("Month")), _c("payment_days", _("Paid days"), "number"), _c("base_gross_pay", _("Gross"), "money"),
                                _c("base_total_deduction", _("Deductions"), "money"), _c("base_net_pay", _("Net"), "money")], rows)


def _d_work_orders(ctx, order="qty desc"):
    rows = ctx.sql(f"""select name, work_order_date, production_item, item_name, qty, produced_qty,
            round(produced_qty / nullif(qty, 0) * 100, 1) achievement, target_yield, actual_yield, actual_waste, target_ops, actual_ops, status
        from `tabWork Order` where docstatus = 1 and work_order_date between %(f)s and %(t)s {ctx.co()} order by {order} limit {DRILL_LIMIT}""")
    return (_("Work orders"), [_c("name", _("Work order"), doctype="Work Order"), _c("work_order_date", _("Date")), _c("item_name", _("Item")),
                               _c("qty", _("Planned"), "number"), _c("produced_qty", _("Produced"), "number"), _c("achievement", _("Achieved %"), "percent"),
                               _c("target_yield", _("Target yield %"), "percent"), _c("actual_yield", _("Actual yield %"), "percent"),
                               _c("actual_waste", _("Waste"), "number"), _c("target_ops", _("Target OPS"), "number"),
                               _c("actual_ops", _("Actual OPS"), "number"), _c("status", _("Status"))], rows)


def _d_downtime(ctx):
    co = " and w.company = %(co)s" if ctx.company else ""
    rows = ctx.sql(f"""select d.name, d.from_time, d.to_time, d.workstation, d.stop_reason, round(d.downtime / 60, 2) hours, d.work_order, d.remarks
        from `tabDowntime Entry` d left join `tabWork Order` w on w.name = d.work_order
        where d.docstatus < 2 and date(d.from_time) between %(f)s and %(t)s {co} order by d.downtime desc limit {DRILL_LIMIT}""")
    return (_("Downtime entries"), [_c("name", _("Entry"), doctype="Downtime Entry"), _c("from_time", _("From")), _c("to_time", _("To")),
                                    _c("workstation", _("Workstation")), _c("stop_reason", _("Reason")), _c("hours", _("Hours"), "number"),
                                    _c("work_order", _("Work order"), doctype="Work Order"), _c("remarks", _("Remarks"))], rows)


def _d_assets(ctx, where="a.docstatus = 1 and a.purchase_date <= %(t)s", title=None):
    rows = ctx.sql(f"""select a.name, a.asset_name, a.asset_category, a.location, a.purchase_date, a.net_purchase_amount gross,
            sum(fb.value_after_depreciation) nbv, a.net_purchase_amount - ifnull(sum(fb.value_after_depreciation), 0) accumulated, a.status, a.custodian
        from `tabAsset` a left join `tabAsset Finance Book` fb on fb.parent = a.name and fb.parenttype = 'Asset'
        where {where} {ctx.co('a')} group by a.name order by gross desc limit {DRILL_LIMIT}""")
    return (title or _("Assets"), [_c("name", _("Asset"), doctype="Asset"), _c("asset_name", _("Name")), _c("asset_category", _("Category")),
                                   _c("location", _("Location")), _c("purchase_date", _("Bought")), _c("gross", _("Cost"), "money"),
                                   _c("nbv", _("Book value"), "money"), _c("accumulated", _("Depreciated"), "money"), _c("status", _("Status")),
                                   _c("custodian", _("Custodian"), doctype="Employee")], rows)


def _d_depreciation(ctx):
    rows = ctx.sql(f"""select s.asset, a.asset_name, a.asset_category, d.schedule_date, d.depreciation_amount, d.accumulated_depreciation_amount,
            d.journal_entry, if(d.journal_entry is null, 'Not posted', 'Posted') posted
        from `tabDepreciation Schedule` d join `tabAsset Depreciation Schedule` s on s.name = d.parent join `tabAsset` a on a.name = s.asset
        where s.docstatus = 1 and d.schedule_date between %(f)s and %(t)s {ctx.co('a')}
        order by (d.journal_entry is null) desc, d.depreciation_amount desc limit {DRILL_LIMIT}""")
    return (_("Depreciation schedule"), [_c("asset", _("Asset"), doctype="Asset"), _c("asset_name", _("Name")), _c("asset_category", _("Category")),
                                         _c("schedule_date", _("Date")), _c("depreciation_amount", _("Depreciation"), "money"),
                                         _c("accumulated_depreciation_amount", _("Accumulated"), "money"),
                                         _c("journal_entry", _("Journal"), doctype="Journal Entry"), _c("posted", _("Status"))], rows)


PROC_COLS = {
    "cycle": ["po", "po_date", "supplier", "item", "request", "receipt", "invoice", "mr_po", "po_pr", "pr_pi", "cycle"],
    "lead": ["po", "po_date", "required_by", "receipt", "supplier", "item", "planned", "po_pr", "late", "ontime", "value"],
    "cover": ["po", "po_date", "supplier", "item", "qty", "received_qty", "received_pct", "billed_pct", "value"],
    "price": ["po", "po_date", "supplier", "item", "std_rate", "rate", "dev", "ppv", "band", "value"],
}
PROC_LABELS = {
    "po": (_("PO"), None, "_po"), "po_date": (_("Ordered"), None, None), "supplier": (_("Supplier"), None, None), "item": (_("Item"), None, None),
    "request": (_("Request"), None, "_mr"), "receipt": (_("Receipt"), None, "_pr"), "invoice": (_("Invoice"), None, "_pi"),
    "mr_po": (_("Req→PO days"), "days", None), "po_pr": (_("PO→receipt days"), "days", None), "pr_pi": (_("Receipt→inv. days"), "days", None),
    "cycle": (_("Cycle days"), "days", None), "required_by": (_("Required by"), None, None), "planned": (_("Planned days"), "days", None),
    "late": (_("Days late"), "days", None), "ontime": (_("On time"), None, None), "qty": (_("Ordered qty"), "number", None),
    "received_qty": (_("Received qty"), "number", None), "received_pct": (_("Received %"), "percent", None), "billed_pct": (_("Billed %"), "percent", None),
    "value": (_("Value"), "money", None), "std_rate": (_("Standard rate"), "money", None), "rate": (_("Actual rate"), "money", None),
    "dev": (_("Deviation %"), "percent", None), "ppv": (_("PPV"), "money", None), "band": (_("Band"), None, None),
}


def _d_procurement(ctx, key):
    _procurement(ctx)
    rows = ctx.proc_rows
    cols = lambda keys: [_c(k, PROC_LABELS[k][0], PROC_LABELS[k][1], doctype_key=PROC_LABELS[k][2]) for k in keys]  # noqa: E731
    if key == "quick":
        return (_("Quick purchases"), cols(["po", "po_date", "receipt", "supplier", "item", "request", "po_pr", "value"]),
                [dict(q, po_pr=q["days"]) for q in sorted(ctx.proc_quick, key=lambda x: -x["value"])])
    if key == "odd":
        return (_("Odd cases"), [_c("reason", _("Why it is odd"))] + cols(["po", "po_date", "required_by", "supplier", "item", "value"]) +
                [_c("lead", _("Lead days"), "days"), _c("received", _("Received"))], sorted(ctx.proc_odd, key=lambda x: -x["value"]))
    if key == "bad_dates":
        return (_("Out-of-order cycle entries"),
                [_c("stage", _("Stage")), _c("from_doc", _("Earlier step"), doctype_key="from_doctype"), _c("from_date", _("Dated")),
                 _c("to_doc", _("Later step"), doctype_key="to_doctype"), _c("to_date", _("Dated")), _c("days", _("Days out"), "days"),
                 _c("user_name", _("Created by"))], ctx.proc_wrong)
    if key in ("cycle", "mr_po", "po_pr", "pr_pi", "lines"):
        sk = {"mr_po": "mr_po", "po_pr": "po_pr", "pr_pi": "pr_pi"}.get(key, "cycle")
        return (_("PO lines — buying cycle"), cols(PROC_COLS["cycle"]), sorted(rows, key=lambda x: -(x[sk] if isinstance(x[sk], (int, float)) else -1e9)))
    if key in ("ontime", "plan_days", "act_days", "late_days"):
        sel = [r for r in rows if r["late"] is not None and r["late"] > 0] if key == "late_days" else rows
        return (_("PO lines — lead time vs schedule"), cols(PROC_COLS["lead"]),
                sorted(sel, key=lambda x: -(x["late"] if isinstance(x["late"], (int, float)) else -1e9)))
    if key in ("received_pct", "billed_pct"):
        return (_("PO lines — coverage"), cols(PROC_COLS["cover"]), sorted(rows, key=lambda x: (x[key], -x["value"])))
    band = {"above_tol": "Above tolerance", "below_tol": "Below tolerance", "suspect": "Suspect (>±200%)"}.get(key)
    sel = [r for r in rows if r["band"] == band] if band else [r for r in rows if r["ppv"] is not None]
    return ((_("PO lines — {0}").format(band.lower()) if band else _("PO lines — purchase price variance")), cols(PROC_COLS["price"]),
            sorted(sel, key=lambda x: -abs(x["dev"] if band else (x["ppv"] or 0))))


def _d_so(ctx, key):
    _so_analysis(ctx)
    rows = ctx.so_rows
    base = [_c("so", _("Sales order"), doctype_key="_so"), _c("so_date", _("Ordered")), _c("customer", _("Customer")), _c("item", _("Item"))]
    if key in ("open_book", "overdue"):
        sel = [r for r in rows if r["pending_qty"] and (r["overdue"] if key == "overdue" else True)]
        return (_("Overdue open order lines") if key == "overdue" else _("Open order book"),
                base + [_c("promise", _("Promised")), _c("overdue", _("Days overdue"), "days"), _c("pending_qty", _("Pending qty"), "number"),
                        _c("pending_value", _("Pending value"), "money"), _c("delivered_pct", _("Delivered %"), "percent")],
                sorted(sel, key=lambda x: -x["pending_value"]))
    if key in ("otif", "lead", "promise", "late_lines"):
        sel = [r for r in rows if r["state"] == "Delivered late"] if key == "late_lines" else rows
        return (_("Order lines — delivery vs promise"),
                base + [_c("promise", _("Promised")), _c("first_dn", _("First delivery"), doctype_key="_dn"), _c("first_delivery", _("Delivered")),
                        _c("promised_days", _("Promised days"), "days"), _c("lead", _("Actual days"), "days"), _c("late_by", _("Days late"), "days"),
                        _c("state", _("Status"))],
                sorted(sel, key=lambda x: -(x["late_by"] or 0)))
    return (_("Sales order lines"),
            base + [_c("qty", _("Qty"), "number"), _c("delivered", _("Delivered"), "number"), _c("delivered_pct", _("Delivered %"), "percent"),
                    _c("value", _("Value"), "money"), _c("billed_pct", _("Billed %"), "percent"), _c("state", _("Status"))],
            sorted(rows, key=lambda x: -x["value"]))


def _d_do(ctx, key):
    _do_analysis(ctx)
    rows = ctx.do_rows
    base = [_c("dn", _("Delivery note"), doctype_key="_dn"), _c("date", _("Delivered")), _c("customer", _("Customer")), _c("item", _("Item")),
            _c("qty", _("Qty"), "number"), _c("value", _("Value"), "money")]
    if key in ("ontime", "late_days"):
        sel = [r for r in rows if r["vs_promise"] is not None and (r["vs_promise"] > 0 if key == "late_days" else True)]
        return (_("Delivery lines vs promise"), base + [_c("so", _("Sales order"), doctype_key="_so"), _c("promise", _("Promised")),
                                                        _c("vs_promise", _("Days late (+) / early (−)"), "days")],
                sorted(sel, key=lambda x: -x["vs_promise"]))
    if key in ("to_invoice", "same_day_inv", "unbilled"):
        sel = [r for r in rows if not r["invoice"]] if key == "unbilled" else rows
        return (_("Delivered, not invoiced") if key == "unbilled" else _("Delivery lines — invoicing"),
                base + [_c("invoice", _("Invoice"), doctype_key="_si"), _c("invoiced_on", _("Invoiced")), _c("to_invoice", _("Days to invoice"), "days"),
                        _c("created_by", _("Created by"))],
                sorted(sel, key=lambda x: -(x["to_invoice"] if x["to_invoice"] is not None else 1e9)))
    return (_("Delivery lines"), base + [_c("so", _("Sales order"), doctype_key="_so"), _c("warehouse", _("Warehouse")), _c("created_by", _("Created by"))],
            sorted(rows, key=lambda x: -x["value"]))


def _d_export(ctx, key):
    _export_analysis(ctx)
    R = ctx.exp_rows
    if key in ("open_lc", "lc_expiring"):
        sel = R["exp_soon"] if key == "lc_expiring" else [r for r in R["lc"] if r["status"] in LC_OPEN]
        return (_("Open LCs"), [_c("lc", _("LC proforma"), doctype_key="_lc"), _c("lc_no", _("LC no")), _c("customer", _("Buyer")), _c("country", _("Country")),
                                _c("status", _("Status")), _c("expiry", _("Expires")), _c("days_left", _("Days left"), "days"), _c("usd", _("USD"), "number"),
                                _c("pkr", _("PKR"), "money"), _c("so", _("Export order"), doctype_key="_so"), _c("bank", _("Issuing bank"))],
                sorted(sel, key=lambda x: (x["days_left"] if x["days_left"] is not None else 9999)))
    if key in ("ontime_lc", "transit", "in_transit", "lead"):
        sel = [r for r in R["ship"] if r["status"] in ("Shipped", "In Transit")] if key == "in_transit" else R["ship"]
        return (_("Export shipments"), [_c("shipment", _("Shipment"), doctype_key="_es"), _c("customer", _("Buyer")), _c("country", _("Country")),
                                        _c("so", _("Export order"), doctype_key="_so"), _c("lc_no", _("LC no")), _c("etd", _("ETD")), _c("eta", _("ETA")),
                                        _c("latest", _("LC latest ship")), _c("late_by", _("Days past LC date"), "days"), _c("transit", _("Transit days"), "days"),
                                        _c("status", _("Status")), _c("line", _("Line"))],
                sorted(sel, key=lambda x: -(x["late_by"] if x["late_by"] is not None else -999)))
    rows = [{"_so": "Sales Order", "so": r.name, "date": str(r.d), "customer": r.customer, "country": r.country or "", "status": r.st or "",
             "value": round(flt(r.v), 2), "qty": flt(r.q), "delivered": flt(r.pd), "incoterm": r.incoterm or "", "port": r.port or "",
             "lc": r.lc_proforma or "", "_lc": "LC Proforma"} for r in R["so"]]
    return (_("Export orders"), [_c("so", _("Export order"), doctype_key="_so"), _c("date", _("Date")), _c("customer", _("Buyer")), _c("country", _("Country")),
                                 _c("port", _("Port")), _c("incoterm", _("Incoterm")), _c("status", _("Export status")), _c("qty", _("Qty"), "number"),
                                 _c("value", _("Value"), "money"), _c("delivered", _("Delivered %"), "percent"), _c("lc", _("LC"), doctype_key="_lc")],
            sorted(rows, key=lambda x: -x["value"]))


def _d_import(ctx, key):
    _import_analysis(ctx)
    rows = ctx.imp_rows
    if key == "late":
        rows = [r for r in rows if r["delay"] is not None and r["delay"] > 2]
    elif key == "at_sea":
        rows = [r for r in rows if not r["arrived"]]
    order = {"dwell": "dwell", "transit": "transit", "late": "delay", "uplift": "uplift", "duty_tax": "duty"}.get(key, "landed")
    return (_("Import shipments"), [_c("shipment", _("Shipment"), doctype_key="_sh"), _c("supplier", _("Supplier")), _c("loading", _("From")),
                                    _c("etd", _("ETD")), _c("arrived", _("Arrived")), _c("cleared", _("Cleared")), _c("transit", _("Transit"), "days"),
                                    _c("dwell", _("Dwell"), "days"), _c("delay", _("Days late"), "days"), _c("purchase", _("Purchase"), "money"),
                                    _c("duty", _("Duty"), "money"), _c("tax", _("Tax"), "money"), _c("landed", _("Landed"), "money"),
                                    _c("uplift", _("Uplift %"), "percent"), _c("po", _("PO"), doctype_key="_po"), _c("receipt", _("Receipt"), doctype_key="_pr")],
            sorted(rows, key=lambda x: -(x[order] if isinstance(x[order], (int, float)) else -1e9)))


def _d_quality(ctx, key):
    _quality(ctx)
    Q = ctx.qa
    if key in ("open_nc",):
        rows = [{"_nc": "Non Conformance", "nc": c.name, "subject": c.subject, "procedure": c.procedure, "status": c.status, "raised": str(c.creation)[:10]}
                for c in Q["ncs"] if c.status == "Open"]
        return (_("Open non-conformances"), [_c("nc", _("Non conformance"), doctype_key="_nc"), _c("subject", _("Subject")), _c("procedure", _("Procedure")),
                                             _c("status", _("Status")), _c("raised", _("Raised"))], rows)
    if key == "goals":
        rows = [{"_qr": "Quality Review", "review": r.name, "goal": r.goal, "date": str(r.date), "status": r.status} for r in Q["reviews"]]
        return (_("Quality reviews"), [_c("review", _("Review"), doctype_key="_qr"), _c("goal", _("Goal")), _c("date", _("Date")), _c("status", _("Status"))],
                sorted(rows, key=lambda x: (x["status"] != "Failed", x["date"])))
    if key == "open_actions":
        rows = [{"_qa": "Quality Action", "action": a.name, "goal": a.goal or "", "type": a.cp, "date": str(a.date), "status": a.status} for a in Q["actions"]]
        return (_("CAPA actions"), [_c("action", _("Action"), doctype_key="_qa"), _c("type", _("Type")), _c("goal", _("Goal")), _c("date", _("Date")),
                                    _c("status", _("Status"))], sorted(rows, key=lambda x: (x["status"] != "Open", x["date"])))
    sel = Q["qis"]
    if key == "rejected":
        sel = [q for q in sel if q.status == "Rejected"]
    elif key == "incoming":
        sel = [q for q in sel if q.itype == "Incoming"]
    elif key in ("rft", "u_pct", "csp"):
        sel = [q for q in sel if (q.tpl or "").startswith("Ring Spun Yarn")]
    rows = [{"_qi": "Quality Inspection", "qi": q.name, "date": str(q.d), "stage": QI_STAGE.get(q.itype, q.itype), "template": q.tpl or "", "item": q.item_name or q.item_code,
             "ref": q.ref, "ref_type": q.rtype, "supplier": q.supplier or "", "status": q.status} for q in sel]
    return (_("Quality inspections"), [_c("qi", _("Inspection"), doctype_key="_qi"), _c("date", _("Date")), _c("stage", _("Stage")), _c("template", _("Test")),
                                       _c("item", _("Item")), _c("ref", _("Reference"), doctype_key="ref_type"), _c("supplier", _("Supplier")), _c("status", _("Result"))],
            sorted(rows, key=lambda x: (x["status"] != "Rejected", x["date"])))


def _d_wo(ctx, key):
    _wo_analysis(ctx)
    rows = ctx.wo_rows
    base = [_c("wo", _("Work order"), doctype_key="_wo"), _c("date", _("Date")), _c("item", _("Item")), _c("stream", _("Stream")), _c("status", _("Status"))]
    if key == "open":
        sel = [r for r in rows if r["status"] not in ("Completed", "Closed")]
        return (_("Open work orders"), base + [_c("age", _("Age (days)"), "days"), _c("qty", _("Planned"), "number"), _c("produced", _("Produced"), "number"),
                                               _c("due", _("Due"))], sorted(sel, key=lambda x: -(x["age"] or 0)))
    if key in ("ontime", "late_days", "start_delay"):
        sel = [r for r in rows if r["late"] is not None and (r["late"] > 0 if key == "late_days" else True)]
        return (_("Work orders — schedule"), base + [_c("planned_start", _("Planned start")), _c("started", _("Started")), _c("start_delay", _("Start delay"), "days"),
                                                     _c("finished", _("Finished")), _c("due", _("Due")), _c("late", _("Days late"), "days")],
                sorted(sel, key=lambda x: -(x["late"] or 0)))
    if key in ("yield_var", "ops_var"):
        sk = "yield_var" if key == "yield_var" else "ops_var"
        sel = [r for r in rows if r[sk] is not None]
        return (_("Work orders — performance"), base + [_c("target_yield", _("Target yield %"), "percent"), _c("actual_yield", _("Actual yield %"), "percent"),
                                                        _c("yield_var", _("Yield var (pts)"), "number"), _c("target_ops", _("Target OPS"), "number"),
                                                        _c("actual_ops", _("Actual OPS"), "number"), _c("ops_var", _("OPS var %"), "percent")],
                sorted(sel, key=lambda x: x[sk]))
    if key == "spindles":
        return (_("Work orders — spindles"), base + [_c("spindles_req", _("Required"), "number"), _c("spindles_alloc", _("Allocated"), "number")],
                sorted(rows, key=lambda x: (x["spindles_alloc"] - x["spindles_req"])))
    if key == "downtime":
        sel = [r for r in rows if r["downtime_h"]]
        return (_("Work orders with downtime"), base + [_c("downtime_h", _("Hours"), "number"), _c("stops", _("Stops"), "number"), _c("achieved", _("Achieved %"), "percent")],
                sorted(sel, key=lambda x: -x["downtime_h"]))
    sel = [r for r in rows if r["stream"] == "Conversion"] if key == "conversion" else rows
    return (_("Work orders"), base + [_c("qty", _("Planned"), "number"), _c("produced", _("Produced"), "number"), _c("achieved", _("Achieved %"), "percent"),
                                      _c("due", _("Due")), _c("late", _("Days late"), "days")], sorted(sel, key=lambda x: x["date"], reverse=True))


def _d_jc(ctx, key):
    _jc_analysis(ctx)
    rows = ctx.jc_rows
    base = [_c("jc", _("Job card"), doctype_key="_jc"), _c("wo", _("Work order"), doctype_key="_wo"), _c("date", _("Date")), _c("operation", _("Operation")),
            _c("workstation", _("Machine")), _c("status", _("Status"))]
    timing = [_c("std_h", _("Std h"), "number"), _c("act_h", _("Actual h"), "number"), _c("over_h", _("Over h"), "number"), _c("efficiency", _("Efficiency"), "percent")]
    if key == "open":
        return (_("Open job cards"), base + [_c("age", _("Age (days)"), "days"), _c("qty", _("Qty"), "number"), _c("act_h", _("Hours so far"), "number")],
                sorted([r for r in rows if r["status"] != "Completed"], key=lambda x: -(x["age"] or 0)))
    if key in ("efficiency", "overrun", "slow"):
        sel = [r for r in rows if r["efficiency"] is not None and (r["efficiency"] < 85 if key == "slow" else True)]
        return (_("Job cards — time"), base + timing, sorted(sel, key=lambda x: x["efficiency"]))
    if key in ("wait", "on_schedule"):
        sel = [r for r in rows if r["wait_h"] is not None]
        return (_("Job cards — schedule"), base + [_c("wait_h", _("Wait h"), "number"), _c("late_h", _("Finish vs expected h"), "number")],
                sorted(sel, key=lambda x: -(x["wait_h"] or 0)))
    if key == "loss":
        sel = [r for r in rows if r["loss_pct"] is not None]
        return (_("Job cards — process loss"), base + [_c("qty", _("Input"), "number"), _c("completed", _("Completed"), "number"), _c("loss", _("Loss"), "number"),
                                                       _c("loss_pct", _("Loss %"), "percent")], sorted(sel, key=lambda x: -x["loss_pct"]))
    if key == "cost":
        return (_("Job cards — cost"), base + [_c("act_h", _("Actual h"), "number"), _c("cost", _("Cost"), "money")], sorted(rows, key=lambda x: -x["cost"]))
    if key == "rework":
        return (_("Rework job cards"), base + timing, [r for r in rows if r["rework"] == "Yes"])
    if key == "operators":
        lg = ctx.sql(f"""select l.employee, max(e.employee_name) name, max(e.department) dept, round(sum(l.time_in_mins) / 60, 1) hours,
                sum(l.completed_qty) qty, count(distinct l.parent) cards
            from `tabJob Card Time Log` l join `tabJob Card` j on j.name = l.parent left join `tabEmployee` e on e.name = l.employee
            where j.docstatus < 2 and j.posting_date between %(f)s and %(t)s {ctx.co('j')} and l.employee is not null group by l.employee order by hours desc""")
        return (_("Operators"), [_c("employee", _("Employee"), doctype="Employee"), _c("name", _("Name")), _c("dept", _("Department")),
                                 _c("hours", _("Hours"), "number"), _c("cards", _("Job cards"), "number"), _c("qty", _("Completed qty"), "number")], lg)
    sel = [r for r in rows if r["status"] == "Completed"] if key == "completion" else rows
    return (_("Job cards"), base + timing + [_c("cost", _("Cost"), "money")], sorted(sel, key=lambda x: x["date"], reverse=True))


def _d_ageing(ctx, doctype):
    rows = [r for r in _ageing_invoices(ctx, doctype) if r.bucket in OVERDUE_90]
    party = _("Customer") if doctype == "Sales Invoice" else _("Supplier")
    return (_("{0} overdue more than 90 days").format(_("Receivables") if doctype == "Sales Invoice" else _("Payables")),
            [_c("name", _("Invoice"), doctype=doctype), _c("posting_date", _("Date")), _c("due", _("Due")), _c("party_name", party),
             _c("bucket", _("Bucket")), _c("days", _("Days overdue"), "days"), _c("grand_total", _("Invoice total"), "money"),
             _c("outstanding_amount", _("Outstanding"), "money")],
            sorted(rows, key=lambda r: -r.days))


DRILL = {
    "accounts": {
        "income": lambda c: _d_gl_accounts(c, ["Income"], _("Income by account")),
        "expense": lambda c: _d_gl_accounts(c, ["Expense"], _("Expenses by account")),
        "profit": lambda c: _d_gl_accounts(c, ["Income", "Expense"], _("Profit & loss by account")),
        "margin": lambda c: _d_gl_accounts(c, ["Income", "Expense"], _("Profit & loss by account")),
        "cash": _d_cash_accounts,
        "receivable": lambda c: _d_sales_invoices(c, True),
        "payable": lambda c: _d_purchase_invoices(c, True),
        "ar_over90": lambda c: _d_ageing(c, "Sales Invoice"),
        "ap_over90": lambda c: _d_ageing(c, "Purchase Invoice"),
    },
    "sales": {
        **{k: _d_sales_invoices for k in ("sales", "invoices", "avg_invoice", "qty", "rate")},
        "orders": lambda c: _d_orders(c, "Sales Order"),
        "collected": lambda c: _d_payments(c, "Receive"),
        "outstanding": lambda c: _d_sales_invoices(c, True),
    },
    "purchase": {
        **{k: _d_purchase_invoices for k in ("purchases", "tax")},
        **{k: (lambda c: _d_orders(c, "Purchase Order")) for k in ("po_value", "po_count", "avg_po")},
        "received": _d_receipts,
        "paid": lambda c: _d_payments(c, "Pay"),
        "payable": lambda c: _d_purchase_invoices(c, True),
    },
    "stock": {
        "value": _d_bins, "items": _d_bins,
        "inward": lambda c: _d_stock_vouchers(c, "inward"),
        "outward": lambda c: _d_stock_vouchers(c, "outward"),
        "turnover": lambda c: _d_stock_vouchers(c, "outward"),
        "entries": _d_stock_entries,
        "fg": lambda c: _d_stock_entries(c, True),
        "moved": _d_items_moved,
    },
    "hr": {
        "headcount": lambda c: _d_employees(c, "status = 'Active'", _("Active employees")),
        "joiners": lambda c: _d_employees(c, "date_of_joining between %(f)s and %(t)s", _("Joined in the period")),
        "leavers": lambda c: _d_employees(c, "relieving_date between %(f)s and %(t)s", _("Left in the period")),
        "attrition": lambda c: _d_employees(c, "relieving_date between %(f)s and %(t)s", _("Left in the period")),
        "female": lambda c: _d_employees(c, "status = 'Active' and gender = 'Female'", _("Active female staff")),
        "attendance": lambda c: _d_attendance(c, "rate asc"),
        "present_day": lambda c: _d_attendance(c, "present desc"),
        "absent": lambda c: _d_attendance(c, "absent desc"),
        "leave_apps": _d_leaves,
    },
    "payroll": {
        **{k: _d_salary_slips for k in ("gross", "net", "deductions", "ded_ratio", "avg_gross", "slips")},
        "employees": lambda c: _d_salary_slips(c, True),
    },
    "production": {
        **{k: _d_work_orders for k in ("planned", "produced", "orders")},
        "achievement": lambda c: _d_work_orders(c, "achievement asc"),
        "yield": lambda c: _d_work_orders(c, "actual_yield asc"),
        "waste": lambda c: _d_work_orders(c, "actual_waste desc"),
        "ops": lambda c: _d_work_orders(c, "actual_ops asc"),
        "downtime": _d_downtime,
    },
    "assets": {
        **{k: _d_assets for k in ("gross", "nbv", "accumulated")},
        **{k: _d_depreciation for k in ("depreciation", "posted")},
        "additions": lambda c: _d_assets(c, "a.docstatus = 1 and a.purchase_date between %(f)s and %(t)s", _("Assets bought in the period")),
        "drafts": lambda c: _d_assets(c, "a.docstatus = 0", _("Draft assets (not capitalised)")),
        "down": lambda c: _d_assets(c, "a.docstatus = 1 and a.status in ('Out of Order', 'In Maintenance')", _("Out of order / in maintenance")),
    },
    "financials": {
        **{k: (lambda c: _d_gl_accounts(c, ["Income", "Expense"], _("Income statement by account"))) for k in ("revenue", "gross_profit", "net_profit")},
        "net_cash": _d_cash_accounts,
        "total_assets": lambda c: _d_gl_accounts(c, ["Asset"], _("Assets by account"), True),
        "total_liabilities": lambda c: _d_gl_accounts(c, ["Liability"], _("Liabilities by account"), True),
        "equity": lambda c: _d_gl_accounts(c, ["Equity"], _("Equity by account"), True),
        "current_ratio": lambda c: _d_gl_accounts(c, ["Asset", "Liability"], _("Balance sheet by account"), True),
    },
    "export_analysis": {k: (lambda c, k=k: _d_export(c, k)) for k in (
        "export_value", "export_share", "destinations", "shipped_pct", "open_lc", "lc_expiring", "ontime_lc", "transit", "lead", "in_transit")},
    "import_analysis": {k: (lambda c, k=k: _d_import(c, k)) for k in (
        "import_value", "landed", "uplift", "duty_tax", "adjustable", "import_share", "transit", "dwell", "late", "at_sea")},
    "jc_analysis": {k: (lambda c, k=k: _d_jc(c, k)) for k in (
        "cards", "completion", "open", "efficiency", "overrun", "wait", "on_schedule", "loss", "cost", "slow", "rework", "operators")},
    "wo_analysis": {k: (lambda c, k=k: _d_wo(c, k)) for k in (
        "orders", "completion", "open", "ontime", "late_days", "start_delay", "achievement", "yield_var", "ops_var", "spindles", "downtime", "conversion")},
    "quality": {k: (lambda c, k=k: _d_quality(c, k)) for k in (
        "inspections", "acceptance", "rejected", "incoming", "rft", "u_pct", "csp", "open_nc", "goals", "open_actions")},
    "so_analysis": {k: (lambda c, k=k: _d_so(c, k)) for k in (
        "booked", "orders", "avg_order", "qty", "delivered_pct", "billed_pct", "open_book", "otif", "lead", "promise", "overdue", "late_lines")},
    "do_analysis": {k: (lambda c, k=k: _d_do(c, k)) for k in (
        "deliveries", "qty", "value", "avg_qty", "ontime", "late_days", "to_invoice", "same_day_inv", "unbilled", "against_so")},
    "procurement": {k: (lambda c, k=k: _d_procurement(c, k)) for k in (
        "cycle", "mr_po", "po_pr", "pr_pi", "ontime", "received_pct", "billed_pct", "lines", "ppv", "ppv_rate", "above_tol", "below_tol",
        "suspect", "bad_dates", "plan_days", "act_days", "late_days", "quick", "odd")},
}


@frappe.whitelist()
def get_drilldown(module: str, key: str, from_date: str, to_date: str, company: str | None = None, tolerance: float | None = None,
                  cost_center: str | None = None, customer: str | None = None, supplier: str | None = None, item: str | None = None,
                  item_group: str | None = None, department: str | None = None, asset_category: str | None = None,
                  account: str | None = None, account_group: str | None = None, account_type: str | None = None,
                  stream: str | None = None, wo_status: str | None = None) -> dict:
    """Rows behind one KPI tile (same period / company / tolerance as the dashboard)."""
    _check_module(module)
    spec = DRILL.get(module, {}).get(key)
    if not spec:
        return {"title": key, "columns": [], "rows": [], "total": 0, "available": False}
    f, t = getdate(from_date), getdate(to_date)
    tol = flt(tolerance) if tolerance not in (None, "") else 5.0
    dims = _dims(cost_center=cost_center, customer=customer, supplier=supplier, item=item, item_group=item_group,
                 department=department, asset_category=asset_category, account=account, account_group=account_group,
                 account_type=account_type, stream=stream, wo_status=wo_status)
    dims = {k: v for k, v in dims.items() if k in MODULE_FILTERS.get(module, ())}
    cache_key = f"micromax-drill:{module}:{key}:{f}:{t}:{company or ''}:{tol}:{_dims_key(dims)}"
    cached = frappe.cache.get_value(cache_key)
    if cached:
        return cached
    ctx = _Ctx(f, t, company or None, dims)
    ctx.tolerance = tol
    title, columns, rows = spec(ctx)
    rows = [dict(r) for r in rows]
    # SQL specs stop at DRILL_LIMIT rows (largest first); in-memory ones are cut here
    truncated = len(rows) >= DRILL_LIMIT
    out = {"title": title, "columns": columns, "rows": rows[:DRILL_LIMIT], "total": len(rows), "truncated": truncated, "available": True,
           "period": {"from": str(f), "to": str(t)}}
    frappe.cache.set_value(cache_key, out, expires_in_sec=CACHE_SECONDS)
    return out
