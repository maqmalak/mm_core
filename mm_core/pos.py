"""Point of sale for the React POS terminal and POS dashboard — built on ERPNext's own POS documents.

POS Opening Entry (a cashier's shift with opening cash) → POS Invoices (sale, return, held as draft) → POS Closing
Entry (counted vs expected per mode of payment; ERPNext consolidates the shift's POS Invoices into Sales Invoices).
"""

import json

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, get_datetime, getdate, nowdate


# Everything taken off an invoice: the bill discount plus line discounts (list price − charged rate, × qty).
DISC_SQL = """(ifnull(`tabPOS Invoice`.discount_amount, 0) + ifnull((select sum(greatest(ifnull(it.price_list_rate, 0) - it.rate, 0) * it.qty)
    from `tabPOS Invoice Item` it where it.parent = `tabPOS Invoice`.name and ifnull(it.is_free_item, 0) = 0), 0))"""


def _discounts(names):
    """Total discount per invoice (bill + line discounts)."""
    names = [n for n in names if n] or [""]
    return {n: flt(d) for n, d in frappe.db.sql(f"select name, {DISC_SQL} from `tabPOS Invoice` where name in %s", (names,))}


def _profiles_for_user():
    user = frappe.session.user
    admin = user == "Administrator" or "System Manager" in frappe.get_roles()
    profiles = frappe.get_all("POS Profile", {"disabled": 0}, ["name", "company", "warehouse", "currency"], order_by="name")
    if admin:
        return profiles
    allowed = set(frappe.get_all("POS Profile User", {"user": user}, pluck="parent"))
    return [p for p in profiles if p.name in allowed]


def _open_shift(profile=None):
    f = {"user": frappe.session.user, "docstatus": 1, "pos_closing_entry": ["in", ["", None]]}
    if profile:
        f["pos_profile"] = profile
    return frappe.db.get_value("POS Opening Entry", f, ["name", "pos_profile", "company", "period_start_date"], as_dict=True,
                               order_by="period_start_date desc")


def _profile(name):
    p = frappe.get_doc("POS Profile", name)
    return {
        "name": p.name, "company": p.company, "warehouse": p.warehouse, "price_list": p.selling_price_list, "currency": p.currency,
        "customer": p.customer, "update_stock": p.update_stock, "allow_rate_change": p.get("allow_rate_change"),
        "allow_discount_change": p.get("allow_discount_change"), "print_format": p.get("print_format"), "letter_head": p.get("letter_head"),
        "payments": [{"mode_of_payment": m.mode_of_payment, "default": m.default} for m in p.payments],
        "item_groups": [g.item_group for g in p.get("item_groups") or []],
        "customer_groups": [g.customer_group for g in p.get("customer_groups") or []],
        "taxes_and_charges": p.get("taxes_and_charges"),
        "allow_partial_payment": p.get("allow_partial_payment"), "write_off_limit": flt(p.get("write_off_limit")),
        "hide_unavailable_items": p.get("hide_unavailable_items"), "hide_images": p.get("hide_images"),
        "auto_add_item_to_cart": p.get("auto_add_item_to_cart"), "ignore_pricing_rule": p.get("ignore_pricing_rule"),
        "print_receipt_on_order_complete": p.get("print_receipt_on_order_complete"),
        "set_grand_total_to_default_mop": p.get("set_grand_total_to_default_mop"),
    }


@frappe.whitelist()
def get_context(pos_profile: str | None = None) -> dict:
    """POS profiles the user may sell from, their open shift (if any) and the chosen profile's settings."""
    profiles = _profiles_for_user()
    shift = _open_shift()
    name = (shift and shift.pos_profile) or pos_profile or (profiles[0].name if profiles else None)
    return {"profiles": profiles, "shift": shift, "profile": _profile(name) if name else None,
            "cashier": frappe.utils.get_fullname(frappe.session.user)}


@frappe.whitelist(methods=["POST"])
def open_shift(pos_profile: str, balances: str) -> dict:
    from erpnext.selling.page.point_of_sale.point_of_sale import create_opening_voucher

    if _open_shift():
        frappe.throw(_("You already have an open shift — close it first."))
    company = frappe.db.get_value("POS Profile", pos_profile, "company")
    rows = [{"mode_of_payment": b["mode_of_payment"], "opening_amount": flt(b.get("opening_amount"))} for b in json.loads(balances)]
    doc = create_opening_voucher(pos_profile, company, json.dumps(rows))
    return {"name": doc["name"]}


def _build_invoice(data, profile):
    lines = data.get("items") or []
    if not lines:
        frappe.throw(_("Add at least one item."))
    inv = frappe.new_doc("POS Invoice")
    inv.update({"pos_profile": profile.name, "company": profile.company, "customer": data.get("customer") or profile.customer,
                "posting_date": nowdate(), "is_pos": 1, "update_stock": profile.update_stock, "currency": profile.currency,
                "selling_price_list": profile.selling_price_list, "set_warehouse": profile.warehouse,
                "ignore_pricing_rule": profile.get("ignore_pricing_rule"), "coupon_code": data.get("coupon_code") or None,
                "additional_discount_percentage": flt(data.get("discount_percentage")),
                "remarks": data.get("remarks")})
    from erpnext.stock.get_item_details import get_conversion_factor

    for l in lines:
        uom = l.get("uom") or frappe.get_cached_value("Item", l["item_code"], "stock_uom")
        cf = flt(get_conversion_factor(l["item_code"], uom).get("conversion_factor")) or 1
        row = {"item_code": l["item_code"], "qty": flt(l["qty"]), "uom": uom, "conversion_factor": cf,
               "stock_qty": flt(l["qty"]) * cf, "warehouse": profile.warehouse}   # stock_qty: pricing rules' min qty
        if l.get("rate") is not None:
            row.update({"rate": flt(l["rate"]), "price_list_rate": flt(l.get("price_list_rate") or l["rate"])})
        if l.get("batch_no") or l.get("serial_no"):
            row.update({"use_serial_batch_fields": 1, "batch_no": l.get("batch_no") or None, "serial_no": l.get("serial_no") or None})
        inv.append("items", row)
    inv.set_missing_values()                                  # price list, UOM conversion, pricing rules / coupon, free items
    if profile.taxes_and_charges and not inv.taxes:
        inv.taxes_and_charges = profile.taxes_and_charges
        inv.set_taxes()
    for l, row in zip(lines, [r for r in inv.items if not r.is_free_item]):   # the cashier's rate / discount win
        if l.get("rate") is not None:
            row.rate = flt(l["rate"])
        base = flt(l["rate"]) if l.get("rate") is not None else flt(row.price_list_rate)
        if l.get("discount_amount") is not None:                 # Rs off per unit (the terminal sends line total / qty)
            off = min(max(flt(l["discount_amount"]), 0), base)
            row.rate = flt(base - off, row.precision("rate"))
            row.discount_percentage = flt(off / base * 100, 6) if base else 0
            row.discount_amount = off
        elif l.get("discount_percentage") is not None:
            row.discount_percentage = flt(l["discount_percentage"])
            row.rate = flt(base * (1 - row.discount_percentage / 100), row.precision("rate"))
    if flt(data.get("discount_amount")) > 0:                     # bill discount as a fixed amount
        inv.additional_discount_percentage = 0
        inv.discount_amount = flt(data["discount_amount"])
    inv.calculate_taxes_and_totals()
    if not inv.ignore_pricing_rule:                           # bill-level offers / coupons (ERPNext applies them on validate)
        from erpnext.accounts.doctype.pricing_rule.utils import apply_pricing_rule_on_transaction

        apply_pricing_rule_on_transaction(inv)
    _apply_loyalty(inv, data)
    inv.calculate_taxes_and_totals()
    return inv


def _apply_loyalty(inv, data):
    """Redeem loyalty points (POS Awesome style): points × the program's conversion factor paid as 'loyalty amount'."""
    program = frappe.db.get_value("Customer", inv.customer, "loyalty_program")
    if not program:
        return
    inv.loyalty_program = program
    points = cint(data.get("redeem_points"))
    if points <= 0:
        return
    from erpnext.accounts.doctype.loyalty_program.loyalty_program import get_loyalty_program_details_with_points

    lp = get_loyalty_program_details_with_points(inv.customer, program, company=inv.company, silent=True)
    points = min(points, cint(lp.get("loyalty_points")))
    if points <= 0:
        return
    inv.redeem_loyalty_points = 1
    inv.loyalty_points = points
    inv.loyalty_amount = flt(points * flt(lp.get("conversion_factor")), 2)
    inv.loyalty_redemption_account = lp.get("expense_account")
    inv.loyalty_redemption_cost_center = lp.get("cost_center")


@frappe.whitelist(methods=["POST"])
def preview(data: str) -> dict:
    """Totals of the cart as ERPNext computes them (price list, discounts, taxes, rounding) — nothing is saved."""
    data = json.loads(data)
    profile = frappe.get_doc("POS Profile", data["pos_profile"])
    inv = _build_invoice(data, profile)
    return {"net_total": inv.net_total, "total": inv.total, "discount_amount": inv.discount_amount,
            "total_taxes_and_charges": inv.total_taxes_and_charges, "grand_total": inv.grand_total,
            "rounded_total": inv.rounded_total or inv.grand_total, "loyalty_amount": flt(inv.loyalty_amount),
            "loyalty_points": cint(inv.loyalty_points), "coupon_code": inv.coupon_code,
            "additional_discount_percentage": flt(inv.additional_discount_percentage),
            "items": [{"item_code": r.item_code, "item_name": r.item_name, "qty": r.qty, "uom": r.uom, "rate": r.rate, "amount": r.amount,
                       "price_list_rate": r.price_list_rate, "discount_percentage": r.discount_percentage,
                       "pricing_rules": r.pricing_rules, "is_free_item": r.is_free_item} for r in inv.items],
            "taxes": [{"description": t.description, "tax_amount": t.tax_amount} for t in inv.taxes]}


@frappe.whitelist(methods=["POST"])
def submit_invoice(data: str) -> dict:
    """Complete a sale (submit) or hold it (data.hold = 1 → draft). data.draft resumes a held invoice."""
    data = json.loads(data) if isinstance(data, str) else data
    if data.get("offline_id"):                                # a sale rung up offline may be synced twice — post it once
        done = frappe.db.get_value("POS Invoice", {"mm_offline_id": data["offline_id"], "docstatus": 1},
                                   ["name", "grand_total", "paid_amount", "change_amount", "outstanding_amount"], as_dict=True)
        if done:
            return {**done, "duplicate": 1}
    shift = _open_shift(data["pos_profile"])
    if not shift and not data.get("hold"):
        frappe.throw(_("Open a shift before selling."))
    profile = frappe.get_doc("POS Profile", data["pos_profile"])
    if data.get("draft") and frappe.db.exists("POS Invoice", {"name": data["draft"], "docstatus": 0}):
        frappe.delete_doc("POS Invoice", data["draft"], ignore_permissions=True)
    inv = _build_invoice(data, profile)
    if data.get("offline_id"):
        inv.mm_offline_id = data["offline_id"]
        inv.mm_offline_at = data.get("offline_at")
        rung = get_datetime(f"{data.get('posting_date')} {data.get('posting_time') or '00:00:00'}") if data.get("posting_date") else None
        if rung and get_datetime(shift.period_start_date) <= rung <= get_datetime():   # inside the shift: keep when it was rung up
            inv.set_posting_time = 1
            inv.posting_date = data["posting_date"]
            inv.posting_time = data.get("posting_time")
    total = flt(inv.rounded_total or inv.grand_total)
    if data.get("hold"):
        inv.remarks = data.get("remarks") or f"Held by {frappe.utils.get_fullname(frappe.session.user)}"
        inv.insert()
        return {"name": inv.name, "held": 1, "grand_total": total}
    paid = 0.0
    inv.set("payments", [])
    for p in data.get("payments") or []:
        if flt(p.get("amount")):
            inv.append("payments", {"mode_of_payment": p["mode_of_payment"], "amount": flt(p["amount"]),
                                    "reference_no": p.get("reference_no")})
            paid += flt(p["amount"])
    paid += flt(inv.loyalty_amount)
    short = flt(total - paid, 2)
    if short > 0.005:
        if data.get("write_off") and short <= flt(profile.get("write_off_limit")):
            inv.write_off_amount = short
            inv.write_off_outstanding_amount_automatically = 1
        elif data.get("credit") and profile.get("allow_partial_payment"):
            if inv.customer == profile.customer:
                frappe.throw(_("Pick the customer for a credit sale — not the walk-in customer."))
            if data.get("due_date"):
                inv.due_date = data["due_date"]
        else:
            frappe.throw(_("Paid {0} is less than the total {1}.").format(frappe.format_value(paid, "Currency"), frappe.format_value(total, "Currency")))
    inv.set_missing_values()                                  # payment accounts from the profile
    if inv.write_off_amount:
        inv.write_off_account = inv.write_off_account or profile.write_off_account
        inv.write_off_cost_center = inv.write_off_cost_center or profile.write_off_cost_center
    inv.calculate_taxes_and_totals()                          # change amount for cash over-tender, outstanding for credit
    inv.insert()
    inv.submit()
    return {"name": inv.name, "grand_total": total, "paid_amount": inv.paid_amount, "change_amount": inv.change_amount,
            "outstanding_amount": inv.outstanding_amount, "write_off_amount": inv.write_off_amount,
            "loyalty_amount": inv.loyalty_amount, "print_format": profile.get("print_format") or "POS Invoice"}


@frappe.whitelist()
def held_invoices(pos_profile: str) -> list:
    return frappe.get_all("POS Invoice", {"pos_profile": pos_profile, "docstatus": 0, "owner": frappe.session.user},
                          ["name", "customer_name", "grand_total", "modified", "remarks"], order_by="modified desc", limit=50)


@frappe.whitelist()
def load_invoice(name: str) -> dict:
    inv = frappe.get_doc("POS Invoice", name)
    inv.check_permission("read")
    return {"name": inv.name, "customer": inv.customer, "customer_name": inv.customer_name, "docstatus": inv.docstatus,
            "discount_percentage": inv.additional_discount_percentage, "coupon_code": inv.coupon_code, "remarks": inv.remarks,
            "is_return": inv.is_return, "grand_total": inv.grand_total, "posting_date": inv.posting_date,
            "items": [{"name": i.name, "item_code": i.item_code, "item_name": i.item_name, "qty": i.qty, "uom": i.uom, "rate": i.rate,
                       "price_list_rate": i.price_list_rate, "discount_percentage": i.discount_percentage, "amount": i.amount,
                       "batch_no": i.batch_no, "serial_no": i.serial_no, "is_free_item": i.is_free_item} for i in inv.items if not i.is_free_item]}


@frappe.whitelist()
def recent_orders(pos_profile: str, q: str | None = None, limit: int = 30) -> list:
    f = {"pos_profile": pos_profile, "docstatus": 1}
    or_f = None
    if q:
        or_f = {"name": ["like", f"%{q}%"], "customer_name": ["like", f"%{q}%"]}
    return frappe.get_all("POS Invoice", filters=f, or_filters=or_f, fields=["name", "customer_name", "posting_date", "posting_time",
                          "grand_total", "is_return", "return_against", "status", "owner"], order_by="creation desc", limit=cint(limit))


@frappe.whitelist(methods=["POST"])
def return_invoice(name: str, items: str | None = None, mode_of_payment: str | None = None) -> dict:
    """Return a POS sale in the current shift — whole invoice, or items = {invoice row: qty} for a partial return.
    Already-returned quantities are respected (ERPNext validates it); the refund goes through the chosen mode (default:
    the sale's first payment mode)."""
    from erpnext.accounts.doctype.pos_invoice.pos_invoice import make_sales_return

    src = frappe.get_doc("POS Invoice", name)
    if src.is_return:
        frappe.throw(_("{0} is already a return.").format(name))
    if not _open_shift(src.pos_profile):
        frappe.throw(_("Open a shift to process returns."))
    ret = make_sales_return(name)
    ret.posting_date = nowdate()
    if items:
        want = {k: flt(v) for k, v in json.loads(items).items()}
        keep = []
        for row in ret.items:
            src_row = row.get("pos_invoice_item")
            if src_row in want and want[src_row] > 0:
                row.qty = -min(abs(flt(row.qty)), want[src_row])
                row.stock_qty = row.qty * flt(row.conversion_factor or 1)
                keep.append(row)
        if not keep:
            frappe.throw(_("Pick at least one item to return."))
        ret.set("items", keep)
        ret.calculate_taxes_and_totals()
    ret.ignore_pricing_rule = 1                               # refund at the prices actually charged
    mode = mode_of_payment or (src.payments[0].mode_of_payment if src.payments else None)
    ret.set("payments", [])
    ret.set_missing_values()
    ret.calculate_taxes_and_totals()
    if mode:                                                  # refund what the return totals to (after rounding)
        ret.set("payments", [])
        ret.append("payments", {"mode_of_payment": mode, "amount": flt(ret.rounded_total or ret.grand_total)})
    # the return copies the sale's paid / change amounts (e.g. 500 tendered, 443 change); POS Invoice doesn't recompute them
    ret.paid_amount = ret.base_paid_amount = sum(flt(p.amount) for p in ret.payments)
    ret.change_amount = ret.base_change_amount = 0
    ret.write_off_amount = ret.base_write_off_amount = 0
    ret.calculate_taxes_and_totals()
    ret.insert()
    ret.submit()
    return {"name": ret.name, "grand_total": ret.grand_total}


@frappe.whitelist()
def returnable_items(name: str) -> dict:
    """Rows of a sale with the quantity still returnable (sold − already returned)."""
    inv = frappe.get_doc("POS Invoice", name)
    inv.check_permission("read")
    returned = dict(frappe.db.sql("""select it.pos_invoice_item, -sum(it.qty) from `tabPOS Invoice Item` it
        join `tabPOS Invoice` r on r.name = it.parent where r.return_against = %s and r.docstatus = 1 group by 1""", name))
    return {"name": inv.name, "customer_name": inv.customer_name, "posting_date": inv.posting_date,
            "payments": [p.mode_of_payment for p in inv.payments if flt(p.amount)],
            "items": [{"name": i.name, "item_code": i.item_code, "item_name": i.item_name, "qty": i.qty, "uom": i.uom, "rate": i.rate,
                       "returnable": flt(i.qty - flt(returned.get(i.name)))} for i in inv.items]}


# ------------------------------------------------------------------ customers, items, offers
@frappe.whitelist()
def customer_info(customer: str, company: str) -> dict:
    """What the cashier needs about the customer: contact, credit position, loyalty points."""
    from erpnext.selling.doctype.customer.customer import get_credit_limit, get_customer_outstanding

    c = frappe.db.get_value("Customer", customer, ["name", "customer_name", "customer_group", "territory", "mobile_no", "email_id",
                                                   "tax_id", "loyalty_program", "default_price_list"], as_dict=True)
    if not c:
        return {}
    c.outstanding = flt(get_customer_outstanding(customer, company, ignore_outstanding_sales_order=True))
    c.outstanding += flt(frappe.db.sql("""select sum(greatest(if(ifnull(rounded_total, 0) != 0, rounded_total, grand_total)
            - paid_amount - ifnull(write_off_amount, 0) - if(redeem_loyalty_points, ifnull(loyalty_amount, 0), 0), 0))
        from `tabPOS Invoice` where customer = %s and company = %s and docstatus = 1 and is_return = 0
        and ifnull(consolidated_invoice, '') = ''""", (customer, company))[0][0])
    c.credit_limit = flt(get_credit_limit(customer, company))
    c.loyalty_points = 0
    if c.loyalty_program:
        from erpnext.accounts.doctype.loyalty_program.loyalty_program import get_loyalty_program_details_with_points

        lp = get_loyalty_program_details_with_points(customer, c.loyalty_program, company=company, silent=True)
        c.update({"loyalty_points": cint(lp.get("loyalty_points")), "conversion_factor": flt(lp.get("conversion_factor")),
                  "tier_name": lp.get("tier_name")})
    c.last_purchase = frappe.db.get_value("POS Invoice", {"customer": customer, "docstatus": 1, "is_return": 0},
                                          ["name", "posting_date", "grand_total"], as_dict=True, order_by="creation desc")
    return c


@frappe.whitelist(methods=["POST"])
def create_customer(data: str) -> dict:
    """Quick customer from the counter: name, mobile, email, tax id (NTN / CNIC), group, territory."""
    d = json.loads(data)
    if not (d.get("customer_name") or "").strip():
        frappe.throw(_("Customer name is required."))
    if d.get("mobile_no") and frappe.db.exists("Customer", {"mobile_no": d["mobile_no"]}):
        frappe.throw(_("A customer with mobile {0} already exists.").format(d["mobile_no"]))
    doc = frappe.get_doc({
        "doctype": "Customer", "customer_name": d["customer_name"].strip(), "customer_type": d.get("customer_type") or "Individual",
        "customer_group": d.get("customer_group") or frappe.db.get_single_value("Selling Settings", "customer_group") or "All Customer Groups",
        "territory": d.get("territory") or frappe.db.get_single_value("Selling Settings", "territory") or "All Territories",
        "mobile_no": d.get("mobile_no"), "email_id": d.get("email_id"), "tax_id": d.get("tax_id"),
    }).insert()
    if d.get("mobile_no") or d.get("email_id"):               # the contact keeps mobile / email for receipts
        contact = frappe.get_doc({"doctype": "Contact", "first_name": doc.customer_name,
                                  "links": [{"link_doctype": "Customer", "link_name": doc.name}]})
        if d.get("mobile_no"):
            contact.append("phone_nos", {"phone": d["mobile_no"], "is_primary_mobile_no": 1})
        if d.get("email_id"):
            contact.append("email_ids", {"email_id": d["email_id"], "is_primary": 1})
        contact.insert(ignore_permissions=True)
        doc.db_set("customer_primary_contact", contact.name)
    return {"name": doc.name, "customer_name": doc.customer_name}


@frappe.whitelist()
def item_options(item_code: str, warehouse: str) -> dict:
    """UOMs (with conversion), batches with stock in the counter's warehouse (FEFO), and available serial numbers."""
    from erpnext.stock.doctype.batch.batch import get_batch_qty

    item = frappe.db.get_value("Item", item_code, ["item_code", "item_name", "stock_uom", "has_batch_no", "has_serial_no",
                                                   "description", "image"], as_dict=True)
    item.uoms = frappe.get_all("UOM Conversion Detail", {"parent": item_code}, ["uom", "conversion_factor"], order_by="idx")
    if not any(u.uom == item.stock_uom for u in item.uoms):
        item.uoms.insert(0, {"uom": item.stock_uom, "conversion_factor": 1})
    item.batches = []
    if item.has_batch_no:
        expiry = dict(frappe.get_all("Batch", {"item": item_code}, ["name", "expiry_date"], as_list=True))
        for b in get_batch_qty(warehouse=warehouse, item_code=item_code) or []:
            exp = expiry.get(b.get("batch_no"))
            if flt(b.get("qty")) > 0 and (not exp or getdate(exp) >= getdate(nowdate())):
                item.batches.append({"batch_no": b.get("batch_no"), "qty": flt(b.get("qty")), "expiry_date": exp})
        item.batches.sort(key=lambda b: (str(b["expiry_date"] or "9999"), b["batch_no"]))
    item.serials = frappe.get_all("Serial No", {"item_code": item_code, "warehouse": warehouse, "status": "Active"},
                                  pluck="name", limit=500) if item.has_serial_no else []
    item.actual_qty = flt(frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty"))
    return item


@frappe.whitelist()
def offers(pos_profile: str) -> list:
    """Selling pricing rules live today for the counter's company — ERPNext applies them to the cart automatically;
    coupon-based ones need the coupon code."""
    company = frappe.db.get_value("POS Profile", pos_profile, "company")
    today = nowdate()
    rules = frappe.db.sql("""select name, title, apply_on, price_or_product_discount, rate_or_discount, discount_percentage,
            discount_amount, rate, min_qty, min_amt, coupon_code_based, free_item, free_qty, valid_upto, priority
        from `tabPricing Rule` where selling = 1 and disable = 0 and ifnull(company, %(c)s) in (%(c)s, '')
          and ifnull(valid_from, '2000-01-01') <= %(d)s and ifnull(valid_upto, '2999-12-31') >= %(d)s
        order by priority desc, modified desc limit 100""", {"c": company, "d": today}, as_dict=True)
    for r in rules:
        field = {"Item Code": ("Pricing Rule Item Code", "item_code"), "Item Group": ("Pricing Rule Item Group", "item_group"),
                 "Brand": ("Pricing Rule Brand", "brand")}.get(r.apply_on)
        r.targets = frappe.get_all(field[0], {"parent": r.name}, pluck=field[1]) if field else []
        r.coupons = frappe.get_all("Coupon Code", {"pricing_rule": r.name, "valid_upto": [">=", today]}, pluck="coupon_code") \
            if r.coupon_code_based else []
    return rules


@frappe.whitelist()
def check_coupon(code: str) -> dict:
    from erpnext.accounts.doctype.pricing_rule.utils import validate_coupon_code

    name = frappe.db.get_value("Coupon Code", {"coupon_code": code}, "name") or (code if frappe.db.exists("Coupon Code", code) else None)
    if not name:
        frappe.throw(_("Coupon {0} not found.").format(code))
    validate_coupon_code(name)
    c = frappe.db.get_value("Coupon Code", name, ["name", "coupon_code", "pricing_rule", "valid_upto", "maximum_use", "used"], as_dict=True)
    c.title = frappe.db.get_value("Pricing Rule", c.pricing_rule, "title")
    return c


def _shift_invoices(shift):
    return frappe.get_all("POS Invoice", {"pos_profile": shift.pos_profile, "owner": frappe.session.user, "docstatus": 1,
                                          "consolidated_invoice": ["in", ["", None]],
                                          "creation": [">=", shift.period_start_date]},
                          ["name", "grand_total", "is_return", "net_total", "total_taxes_and_charges", "discount_amount",
                           "customer_name", "posting_date", "posting_time", "change_amount", "mm_offline_id"], order_by="creation")


@frappe.whitelist()
def shift_summary() -> dict:
    """Everything the close-shift screen shows: totals, every invoice, taxes, and per payment mode the opening float,
    sales, cash in / out and the expected amount (the cashier's counted amount starts from it)."""
    shift = _open_shift()
    if not shift:
        return {}
    opening = frappe.get_doc("POS Opening Entry", shift.name)
    invs = _shift_invoices(shift)
    names = [i.name for i in invs] or [""]
    by_mode = dict(frappe.db.sql("""select mode_of_payment, sum(amount) from `tabSales Invoice Payment`
        where parenttype = 'POS Invoice' and parent in %s group by mode_of_payment""", (names,)))
    pay_rows = frappe.db.sql("""select parent, group_concat(mode_of_payment separator ', ') from `tabSales Invoice Payment`
        where parenttype = 'POS Invoice' and parent in %s and amount != 0 group by parent""", (names,))
    modes_of = dict(pay_rows)
    change = flt(sum(flt(i.change_amount) for i in invs))
    moves = _dues_collected(shift)
    openings = {b.mode_of_payment: flt(b.opening_amount) for b in opening.balance_details}
    order = list(openings) + [m for m in list(by_mode) + list(moves) if m not in openings]   # modes used but not opened with
    modes, seen = [], set()
    for mode in order:
        if mode in seen:
            continue
        seen.add(mode)
        sales = flt(by_mode.get(mode))
        is_cash = "cash" in mode.lower()
        expected = openings.get(mode, 0) + sales - (change if is_cash else 0) + flt(moves.get(mode))
        modes.append({"mode_of_payment": mode, "opening": openings.get(mode, 0), "sales": sales, "change": change if is_cash else 0,
                      "dues": flt(moves.get(mode)), "expected": flt(expected, 2), "is_cash": is_cash})
    taxes = frappe.db.sql("""select description, max(rate), sum(tax_amount) from `tabSales Taxes and Charges`
        where parenttype = 'POS Invoice' and parent in %s group by description""", (names,))
    sales = [i for i in invs if not i.is_return]
    returns = [i for i in invs if i.is_return]
    disc = _discounts([i.name for i in invs])
    return {
        "shift": shift, "profile": shift.pos_profile, "cashier": frappe.utils.get_fullname(frappe.session.user),
        "invoices": len(sales), "returns": len(returns), "total": flt(sum(i.grand_total for i in invs), 2),
        "gross": flt(sum(i.grand_total for i in sales), 2), "refunds": flt(sum(i.grand_total for i in returns), 2),
        "net_total": flt(sum(flt(i.net_total) for i in invs), 2), "tax_total": flt(sum(flt(i.total_taxes_and_charges) for i in invs), 2),
        "discounts": flt(sum(disc.values()), 2), "change": change,
        "rows": [{"name": i.name, "type": "Return" if i.is_return else "Sale", "customer": i.customer_name, "time": "{:0>2}:{}".format(*str(i.posting_time).split(":")[:2]),
                  "date": str(i.posting_date), "amount": flt(i.grand_total), "discount": flt(disc.get(i.name)), "modes": modes_of.get(i.name, ""),
                  "offline": bool(i.mm_offline_id)}
                 for i in invs],
        "taxes": [{"description": d, "rate": flt(r), "amount": flt(a)} for d, r, a in taxes],
        "modes": modes,
    }


@frappe.whitelist(methods=["POST"])
def close_shift(counted: str) -> dict:
    """POS Closing Entry for the user's open shift with the counted amounts; ERPNext consolidates its invoices on submit."""
    from erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry import make_closing_entry_from_opening

    shift = _open_shift()
    if not shift:
        frappe.throw(_("No open shift."))
    counted = json.loads(counted)
    for mode, amt in _dues_collected(shift).items():          # collected customer dues are reported separately
        if mode in counted:
            counted[mode] = flt(counted[mode]) - amt
    closing = make_closing_entry_from_opening(frappe.get_doc("POS Opening Entry", shift.name))
    for row in closing.payment_reconciliation:
        if row.mode_of_payment in counted:
            row.closing_amount = flt(counted[row.mode_of_payment])
            row.difference = flt(row.closing_amount - row.expected_amount, 2)
    closing.insert()
    closing.submit()
    return {"name": closing.name, "status": frappe.db.get_value("POS Closing Entry", closing.name, "status")}


def _dues_collected(shift):
    """Cash the cashier took in (+) or paid out (−) at the counter during the shift, outside sales — dues collected,
    cash receipts and cash payments (Payment Entries / Journal Entries tagged "POS <shift>"), per mode of payment."""
    tag = f"%POS {shift.name}%"
    out = {}
    for m, a in frappe.db.sql("""select mode_of_payment, sum(if(payment_type = 'Receive', paid_amount, -paid_amount)) from `tabPayment Entry`
            where docstatus = 1 and remarks like %s group by 1""", tag):
        out[m] = out.get(m, 0) + flt(a)
    for m, a in frappe.db.sql("""select mode_of_payment, sum(if(title like 'Cash In%%', total_debit, -total_debit)) from `tabJournal Entry`
            where docstatus = 1 and user_remark like %s group by 1""", tag):
        out[m] = out.get(m, 0) + flt(a)
    return out


# ------------------------------------------------------------------ offline, receipts, invoice management, reports
@frappe.whitelist()
def offline_bundle(pos_profile: str) -> dict:
    """Everything the terminal needs to keep selling without the server: profile, tax rows, items with price / stock /
    barcodes, customers, company header for receipts. Cached in the browser (IndexedDB) and refreshed when online."""
    from erpnext.selling.page.point_of_sale.point_of_sale import get_items

    prof = _profile(pos_profile)
    doc = frappe.get_cached_doc("POS Profile", pos_profile)
    prof["disable_rounded_total"] = cint(doc.get("disable_rounded_total") or frappe.db.get_single_value("Global Defaults", "disable_rounded_total"))
    taxes = []
    if prof.get("taxes_and_charges"):
        taxes = frappe.get_all("Sales Taxes and Charges", {"parent": prof["taxes_and_charges"], "parenttype": "Sales Taxes and Charges Template"},
                               ["charge_type", "description", "rate", "included_in_print_rate", "account_head"], order_by="idx")
    items, start = [], 0
    while True:
        page = get_items(start, 500, prof["price_list"], "", pos_profile).get("items", [])
        items += page
        if len(page) < 500 or len(items) >= 5000:
            break
        start += 500
    codes = [i["item_code"] for i in items] or [""]
    barcodes = {}
    for code, bc in frappe.db.sql("select parent, barcode from `tabItem Barcode` where parent in %s", (codes,)):
        barcodes.setdefault(code, []).append(bc)
    groups = dict(frappe.db.sql("select name, item_group from tabItem where name in %s", (codes,)))
    for i in items:
        i["barcodes"] = barcodes.get(i["item_code"], [])
        i["item_group"] = groups.get(i["item_code"])
    customers = frappe.get_all("Customer", {"disabled": 0}, ["name", "customer_name", "mobile_no", "loyalty_program", "customer_group"],
                               order_by="modified desc", limit=5000)
    return {"profile": prof, "taxes": taxes, "items": items, "customers": customers, "company": _company_header(prof["company"]),
            "qty_presets": qty_presets(pos_profile),
            "cashier": frappe.utils.get_fullname(frappe.session.user), "user": frappe.session.user,
            "generated_at": frappe.utils.now(), "currency_symbol": frappe.db.get_value("Currency", prof["currency"], "symbol") or prof["currency"]}


def _company_header(company):
    c = frappe.db.get_value("Company", company, ["company_name", "tax_id", "phone_no", "email", "website", "company_logo"], as_dict=True) or {}
    addr = frappe.db.sql("""select a.address_line1, a.address_line2, a.city from tabAddress a join `tabDynamic Link` l on l.parent = a.name
        where l.link_doctype = 'Company' and l.link_name = %s order by a.is_primary_address desc limit 1""", company, as_dict=True)
    c["address"] = ", ".join(x for x in (addr[0].values() if addr else []) if x)
    return c


@frappe.whitelist(methods=["POST"])
def sync_invoices(invoices: str) -> list:
    """Post sales rung up offline, one by one (each in its own transaction). Returns per offline id: name or error."""
    out = []
    for data in json.loads(invoices):
        try:
            r = submit_invoice(json.dumps(data))
            frappe.db.commit()
            out.append({"offline_id": data.get("offline_id"), "ok": 1, **r})
        except Exception as e:
            frappe.db.rollback()
            frappe.clear_messages()
            out.append({"offline_id": data.get("offline_id"), "ok": 0, "error": frappe.utils.strip_html(str(e)) or e.__class__.__name__})
    return out


@frappe.whitelist()
def receipt(name: str) -> dict:
    """What the thermal receipt prints."""
    inv = frappe.get_doc("POS Invoice", name)
    inv.check_permission("read")
    return {
        "name": inv.name, "docstatus": inv.docstatus, "is_return": inv.is_return, "return_against": inv.return_against,
        "posting_date": str(inv.posting_date), "posting_time": str(inv.posting_time)[:8], "customer": inv.customer,
        "customer_name": inv.customer_name, "contact_mobile": inv.contact_mobile, "tax_id": inv.tax_id,
        "cashier": frappe.utils.get_fullname(inv.owner), "pos_profile": inv.pos_profile, "currency": inv.currency,
        "items": [{"item_code": i.item_code, "item_name": i.item_name, "qty": i.qty, "uom": i.uom, "rate": i.rate, "amount": i.amount,
                   "price_list_rate": i.price_list_rate, "discount_percentage": i.discount_percentage, "is_free_item": i.is_free_item,
                   "batch_no": i.batch_no, "serial_no": i.serial_no} for i in inv.items],
        "total": inv.total, "net_total": inv.net_total, "discount_amount": inv.discount_amount,
        "additional_discount_percentage": inv.additional_discount_percentage, "coupon_code": inv.coupon_code,
        "taxes": [{"description": t.description, "rate": t.rate, "tax_amount": t.tax_amount} for t in inv.taxes],
        "grand_total": inv.grand_total, "rounded_total": inv.rounded_total or inv.grand_total, "rounding_adjustment": inv.rounding_adjustment,
        "in_words": inv.in_words, "payments": [{"mode_of_payment": p.mode_of_payment, "amount": p.amount, "reference_no": p.reference_no}
                                               for p in inv.payments if flt(p.amount)],
        "paid_amount": inv.paid_amount, "change_amount": inv.change_amount, "outstanding_amount": inv.outstanding_amount,
        "write_off_amount": inv.write_off_amount, "loyalty_amount": inv.loyalty_amount if inv.redeem_loyalty_points else 0,
        "loyalty_points": inv.loyalty_points if inv.redeem_loyalty_points else 0, "remarks": inv.remarks, "offline_id": inv.get("mm_offline_id"),
        "total_qty": inv.total_qty, "company": _company_header(inv.company),
    }


@frappe.whitelist()
def invoices(pos_profile: str, status: str = "all", from_date: str | None = None, to_date: str | None = None, q: str | None = None,
             mine: int = 0, start: int = 0, page_length: int = 50) -> dict:
    """Invoice manager: sales of a counter by status (paid / credit / return / held / offline / consolidated), dates, search."""
    cond, vals = ["pos_profile = %(p)s"], {"p": pos_profile}
    if from_date:
        cond.append("posting_date >= %(f)s"); vals["f"] = from_date
    if to_date:
        cond.append("posting_date <= %(t)s"); vals["t"] = to_date
    if cint(mine):
        cond.append("owner = %(u)s"); vals["u"] = frappe.session.user
    if q:
        cond.append("(name like %(q)s or customer like %(q)s or customer_name like %(q)s or mm_offline_id like %(q)s)"); vals["q"] = f"%{q}%"
    due = """greatest(if(ifnull(rounded_total, 0) != 0, rounded_total, grand_total) - paid_amount - ifnull(write_off_amount, 0)
             - if(redeem_loyalty_points, ifnull(loyalty_amount, 0), 0), 0)"""
    status_cond = {
        "all": "docstatus < 2", "paid": f"docstatus = 1 and is_return = 0 and {due} < 0.01", "credit": f"docstatus = 1 and is_return = 0 and {due} >= 0.01",
        "return": "docstatus = 1 and is_return = 1", "held": "docstatus = 0", "offline": "docstatus = 1 and ifnull(mm_offline_id, '') != ''",
        "consolidated": "docstatus = 1 and ifnull(consolidated_invoice, '') != ''", "cancelled": "docstatus = 2",
    }
    base = " and ".join(cond)
    where = f"{base} and {status_cond.get(status, status_cond['all'])}"
    rows = frappe.db.sql(f"""select name, customer, customer_name, posting_date, posting_time, grand_total,
            if(ifnull(rounded_total, 0) != 0, rounded_total, grand_total) as total, paid_amount, change_amount, write_off_amount,
            {due} as due, {DISC_SQL} as discount, is_return, return_against, status, docstatus, consolidated_invoice, owner, mm_offline_id, coupon_code, remarks,
            total_qty, (select count(*) from `tabPOS Invoice Item` it where it.parent = `tabPOS Invoice`.name) as line_count,
            (select group_concat(distinct p.mode_of_payment separator ', ') from `tabSales Invoice Payment` p
             where p.parent = `tabPOS Invoice`.name and p.parenttype = 'POS Invoice' and p.amount != 0) as modes
        from `tabPOS Invoice` where {where} order by creation desc limit %(s)s, %(n)s""", {**vals, "s": cint(start), "n": cint(page_length)}, as_dict=True)
    tot = frappe.db.sql(f"""select count(*), sum(if(is_return, 0, grand_total)), sum({due}), sum(if(is_return, 0, paid_amount)),
            sum(ifnull(change_amount, 0)), sum(if(is_return, grand_total, 0)), sum({DISC_SQL}) from `tabPOS Invoice` where {where}""", vals)[0]
    # badge counts for the tabs, same dates / search
    tabs = {k: int(frappe.db.sql(f"select count(*) from `tabPOS Invoice` where {base} and {status_cond[k]}", vals)[0][0] or 0)
            for k in ("all", "credit", "held", "return")}
    for r in rows:
        r.cashier = frappe.utils.get_fullname(r.owner)
    return {"rows": rows, "count": int(tot[0] or 0), "amount": flt(tot[1]), "due": flt(tot[2]), "tendered": flt(tot[3]),
            "change": flt(tot[4]), "refunds": flt(tot[5]), "discount": flt(tot[6]), "tabs": tabs}


@frappe.whitelist(methods=["POST"])
def delete_held(name: str) -> dict:
    doc = frappe.get_doc("POS Invoice", name)
    if doc.docstatus != 0:
        frappe.throw(_("Only held (draft) sales can be deleted."))
    if doc.owner != frappe.session.user and "System Manager" not in frappe.get_roles():
        frappe.throw(_("Only the cashier who held {0} can delete it.").format(name))
    frappe.delete_doc("POS Invoice", name)
    return {"deleted": name}


@frappe.whitelist()
def shift_report(opening: str | None = None) -> dict:
    """X report (the open shift so far) or Z report (a closed shift): takings by mode, items, returns, discounts, taxes."""
    if opening:
        shift = frappe.db.get_value("POS Opening Entry", opening, ["name", "pos_profile", "company", "user", "period_start_date", "pos_closing_entry"], as_dict=True)
    else:
        shift = _open_shift()
        if shift:
            shift.user = frappe.session.user
    if not shift:
        return {}
    closing = frappe.get_doc("POS Closing Entry", shift.pos_closing_entry) if shift.get("pos_closing_entry") else None
    if closing:
        names = [r.pos_invoice for r in closing.pos_invoices]
    else:
        names = frappe.get_all("POS Invoice", {"pos_profile": shift.pos_profile, "owner": shift.user, "docstatus": 1,
                                               "creation": [">=", shift.period_start_date]}, pluck="name")
    names = names or [""]
    head = frappe.db.sql(f"""select count(*), sum(if(is_return, 0, 1)), sum(if(is_return, grand_total, 0)), sum(if(is_return, 0, grand_total)),
            sum(net_total), sum(total_taxes_and_charges), sum({DISC_SQL}), sum(change_amount), sum(ifnull(write_off_amount, 0)),
            sum(if(redeem_loyalty_points, loyalty_amount, 0)), sum(ifnull(coupon_code, '') != ''), min(name), max(name)
        from `tabPOS Invoice` where name in %s""", (names,))[0]
    modes = frappe.db.sql("""select mode_of_payment, sum(amount) from `tabSales Invoice Payment` where parenttype = 'POS Invoice'
        and parent in %s group by 1 order by 2 desc""", (names,))
    items = frappe.db.sql("""select item_name, sum(qty), sum(amount) from `tabPOS Invoice Item` where parent in %s group by item_code
        order by 3 desc limit 30""", (names,))
    taxes = frappe.db.sql("""select description, sum(tax_amount) from `tabSales Taxes and Charges` where parenttype = 'POS Invoice'
        and parent in %s group by 1""", (names,))
    summary = shift_summary() if not closing else {}
    return {
        "type": "Z" if closing else "X", "shift": shift.name, "closing": closing.name if closing else None, "pos_profile": shift.pos_profile,
        "cashier": frappe.utils.get_fullname(shift.user), "start": str(shift.period_start_date),
        "end": str(closing.period_end_date) if closing else frappe.utils.now(), "company": _company_header(shift.company),
        "invoices": int(head[1] or 0), "returns": int((head[0] or 0) - (head[1] or 0)), "refunds": flt(head[2]), "gross": flt(head[3]),
        "net_sales": flt(head[3]) + flt(head[2]), "net_total": flt(head[4]), "taxes_total": flt(head[5]), "discounts": flt(head[6]),
        "change": flt(head[7]), "write_off": flt(head[8]), "loyalty": flt(head[9]), "coupons": int(head[10] or 0),
        "first": head[11], "last": head[12],
        "modes": [{"mode": m, "amount": flt(a)} for m, a in modes],
        "items": [{"item": n, "qty": flt(q), "amount": flt(a)} for n, q, a in items],
        "taxes": [{"description": d, "amount": flt(a)} for d, a in taxes],
        "reconciliation": ([{"mode": r.mode_of_payment, "opening": r.opening_amount, "expected": r.expected_amount, "counted": r.closing_amount,
                             "difference": r.difference} for r in closing.payment_reconciliation] if closing else
                           [{"mode": m["mode_of_payment"], "opening": m["opening"], "expected": m["expected"], "dues": m.get("dues", 0)}
                            for m in summary.get("modes", [])]),
    }


@frappe.whitelist()
def last_shift_report() -> dict:
    """Z report of the cashier's most recently closed shift (printed right after closing)."""
    name = frappe.db.get_value("POS Opening Entry", {"user": frappe.session.user, "docstatus": 1, "pos_closing_entry": ["is", "set"]},
                               "name", order_by="period_start_date desc")
    return shift_report(name) if name else {}


@frappe.whitelist(methods=["POST"])
def receive_payment(customer: str, amount: float, mode_of_payment: str, reference_no: str | None = None) -> dict:
    """Collect a customer's dues at the counter: a Payment Entry allocated to their oldest open Sales Invoices
    (credit POS sales become Sales Invoices when the shift is consolidated)."""
    from erpnext.accounts.doctype.sales_invoice.sales_invoice import get_bank_cash_account
    from erpnext.accounts.party import get_party_account

    shift = _open_shift()
    if not shift:
        frappe.throw(_("Open a shift to collect payments."))
    amount = flt(amount)
    if amount <= 0:
        frappe.throw(_("Enter the amount received."))
    company = shift.company
    pe = frappe.new_doc("Payment Entry")
    pe.update({"payment_type": "Receive", "party_type": "Customer", "party": customer, "company": company, "posting_date": nowdate(),
               "mode_of_payment": mode_of_payment, "paid_from": get_party_account("Customer", customer, company),
               "paid_to": get_bank_cash_account(mode_of_payment, company).get("account"), "paid_amount": amount, "received_amount": amount,
               "reference_no": reference_no or f"POS {shift.name}", "reference_date": nowdate(),
               "custom_remarks": 1, "remarks": f"Dues collected at the counter — POS {shift.name} ({shift.pos_profile})"})
    left = amount
    for si in frappe.db.sql("""select name, outstanding_amount, grand_total, due_date from `tabSales Invoice` where customer = %s and company = %s
            and docstatus = 1 and outstanding_amount > 0 order by due_date, posting_date""", (customer, company), as_dict=True):
        if left <= 0:
            break
        alloc = min(left, flt(si.outstanding_amount))
        pe.append("references", {"reference_doctype": "Sales Invoice", "reference_name": si.name, "total_amount": si.grand_total,
                                 "outstanding_amount": si.outstanding_amount, "allocated_amount": alloc, "due_date": si.due_date})
        left -= alloc
    pe.setup_party_account_field()
    pe.set_missing_values()
    pe.set_amounts()
    pe.insert()
    pe.submit()
    return {"name": pe.name, "allocated": flt(amount - left), "unallocated": flt(left)}


@frappe.whitelist()
def qty_presets(pos_profile: str | None = None) -> dict:
    """Items with POS quantity presets: {item_code: {default: n, options: [..]}} (Item.mm_pos_default_qty / mm_pos_qty_options)."""
    if not frappe.get_meta("Item").has_field("mm_pos_default_qty"):
        return {}
    out = {}
    for code, d, opts in frappe.db.sql("""select name, mm_pos_default_qty, mm_pos_qty_options from tabItem
            where disabled = 0 and (ifnull(mm_pos_default_qty, 0) > 0 or ifnull(mm_pos_qty_options, '') != '')"""):
        options = sorted({flt(x) for x in (opts or "").replace(";", ",").split(",") if flt(x) > 0})
        out[code] = {"default": flt(d) or 1, "options": options}
    return out


# ------------------------------------------------------------------ receive stock into the counter
@frappe.whitelist()
def stock_setup(pos_profile: str) -> dict:
    """The counter's warehouse and the company's other warehouses (sources for a transfer)."""
    prof = frappe.get_cached_doc("POS Profile", pos_profile)
    others = frappe.get_all("Warehouse", {"company": prof.company, "is_group": 0, "disabled": 0, "name": ["!=", prof.warehouse]},
                            pluck="name", order_by="name")
    return {"warehouse": prof.warehouse, "company": prof.company, "warehouses": others, "currency": prof.currency,
            "can_receive": bool(frappe.has_permission("Stock Entry", "create")),
            "company_header": _company_header(prof.company), "user": frappe.utils.get_fullname(frappe.session.user)}


@frappe.whitelist()
def counter_stock(pos_profile: str, q: str | None = None) -> dict:
    """What's on the counter's shelves: qty, reorder level, value — low / out items first."""
    prof = frappe.get_cached_doc("POS Profile", pos_profile)
    cond, vals = "", {"w": prof.warehouse}
    if q:
        cond = " and (i.name like %(q)s or i.item_name like %(q)s)"
        vals["q"] = f"%{q}%"
    rows = frappe.db.sql(f"""select i.name as item_code, i.item_name, i.item_group, i.stock_uom, ifnull(b.actual_qty, 0) as qty,
            ifnull(b.stock_value, 0) as value, ifnull(r.warehouse_reorder_level, 0) as reorder_level, ifnull(r.warehouse_reorder_qty, 0) as reorder_qty
        from tabBin b join tabItem i on i.name = b.item_code
        left join `tabItem Reorder` r on r.parent = i.name and r.warehouse = b.warehouse
        where b.warehouse = %(w)s and i.disabled = 0 {cond}
        order by (ifnull(b.actual_qty, 0) <= greatest(ifnull(r.warehouse_reorder_level, 0), 5)) desc, i.item_name limit 300""", vals, as_dict=True)
    for r in rows:
        r.state = "out" if flt(r.qty) <= 0 else "low" if flt(r.qty) <= max(flt(r.reorder_level), 5) else "ok"
    return {"rows": rows, "value": flt(sum(flt(r.value) for r in rows)), "out": sum(r.state == "out" for r in rows),
            "low": sum(r.state == "low" for r in rows), "items": len(rows)}


@frappe.whitelist()
def source_stock(warehouse: str, item_codes: str) -> dict:
    """Available qty of the chosen items in a source warehouse (for transfers)."""
    codes = json.loads(item_codes) or [""]
    return {c: flt(q) for c, q in frappe.db.sql("select item_code, actual_qty from tabBin where warehouse = %s and item_code in %s", (warehouse, codes))}


@frappe.whitelist(methods=["POST"])
def receive_stock(data: str) -> dict:
    """Stock into the counter: mode "transfer" (Material Transfer from a source warehouse) or "receipt" (Material Receipt,
    new stock at a valuation rate). Submitted straight away; returns the Stock Entry."""
    d = json.loads(data)
    prof = frappe.get_cached_doc("POS Profile", d["pos_profile"])
    rows = [r for r in d.get("items") or [] if flt(r.get("qty")) > 0]
    if not rows:
        frappe.throw(_("Add at least one item with a quantity."))
    transfer = d.get("mode") == "transfer"
    if transfer and not d.get("source_warehouse"):
        frappe.throw(_("Choose the warehouse the stock comes from."))
    se = frappe.new_doc("Stock Entry")
    se.update({"stock_entry_type": "Material Transfer" if transfer else "Material Receipt", "purpose": "Material Transfer" if transfer else "Material Receipt",
               "company": prof.company, "posting_date": nowdate(), "to_warehouse": prof.warehouse,
               "from_warehouse": d.get("source_warehouse") if transfer else None,
               "remarks": (d.get("remarks") or "").strip() or f"Stock received at the counter ({prof.name})"})
    from erpnext.stock.get_item_details import get_conversion_factor

    for r in rows:
        stock_uom = frappe.get_cached_value("Item", r["item_code"], "stock_uom")
        uom = r.get("uom") or stock_uom
        cf = flt(get_conversion_factor(r["item_code"], uom).get("conversion_factor")) or 1
        row = {"item_code": r["item_code"], "qty": flt(r["qty"]), "t_warehouse": prof.warehouse, "uom": uom, "stock_uom": stock_uom,
               "conversion_factor": cf, "transfer_qty": flt(r["qty"]) * cf}
        if transfer:
            row["s_warehouse"] = d["source_warehouse"]
        elif flt(r.get("rate")):
            row.update({"basic_rate": flt(r["rate"]), "set_basic_rate_manually": 1})
        se.append("items", row)
    se.set_missing_values()
    se.insert()
    se.submit()
    return {"name": se.name, "purpose": se.purpose, "total": flt(se.total_incoming_value), "lines": len(se.items)}


@frappe.whitelist()
def stock_receipts(pos_profile: str, limit: int = 20) -> list:
    """Recent stock entries into the counter's warehouse."""
    wh = frappe.get_cached_value("POS Profile", pos_profile, "warehouse")
    rows = frappe.db.sql("""select se.name, se.purpose, se.posting_date, se.posting_time, se.docstatus, se.owner, se.remarks, se.total_incoming_value,
            count(d.name) as line_count, sum(d.qty) as qty, max(d.s_warehouse) as source
        from `tabStock Entry` se join `tabStock Entry Detail` d on d.parent = se.name
        where d.t_warehouse = %s and se.docstatus < 2 group by se.name order by se.creation desc limit %s""", (wh, cint(limit)), as_dict=True)
    for r in rows:
        r.user = frappe.utils.get_fullname(r.owner)
        r.posting_time = "{:0>2}:{}".format(*str(r.posting_time).split(":")[:2])
    return rows


@frappe.whitelist()
def stock_receipt_detail(name: str) -> dict:
    se = frappe.get_doc("Stock Entry", name)
    se.check_permission("read")
    return {"name": se.name, "purpose": se.purpose, "posting_date": str(se.posting_date), "posting_time": str(se.posting_time)[:5],
            "remarks": se.remarks, "user": frappe.utils.get_fullname(se.owner), "total": flt(se.total_incoming_value),
            "items": [{"item_code": i.item_code, "item_name": i.item_name, "qty": i.qty, "uom": i.uom, "rate": i.basic_rate, "amount": i.basic_amount,
                       "from": i.s_warehouse, "to": i.t_warehouse} for i in se.items]}


# ------------------------------------------------------------------ a notification for every POS invoice
def _invoice_recipients(doc):
    """The counter's cashiers (POS Profile users) and Administrator — not every manager on the site."""
    users = {u.user for u in frappe.get_cached_doc("POS Profile", doc.pos_profile).get("applicable_for_users") or []}
    users.add("Administrator")
    return sorted(u for u in users if u and frappe.db.get_value("User", u, "enabled"))


def notify_invoice(doc, method=None):
    """POS Invoice on_submit: an Alert in every recipient's bell (Alert-type logs never send email)."""
    try:
        total = flt(doc.rounded_total or doc.grand_total)
        due = max(total - flt(doc.paid_amount) - flt(doc.write_off_amount) - (flt(doc.loyalty_amount) if doc.redeem_loyalty_points else 0), 0)
        money = frappe.format_value(abs(total), {"fieldtype": "Currency", "options": doc.currency})
        who = frappe.utils.get_fullname(doc.owner)
        if doc.is_return:
            head = f"↩️ <b>POS return</b> {doc.name} · {money} refunded"
        elif due > 0.01:
            head = f"🧾 <b>POS credit sale</b> {doc.name} · {money} · {frappe.format_value(due, {'fieldtype': 'Currency', 'options': doc.currency})} due"
        else:
            head = f"🧾 <b>POS sale</b> {doc.name} · {money}"
        subject = f"{head} — {frappe.utils.escape_html(doc.customer_name or doc.customer)} · {frappe.utils.escape_html(doc.pos_profile)} · by {frappe.utils.escape_html(who)}"
        for user in _invoice_recipients(doc):
            frappe.get_doc({"doctype": "Notification Log", "for_user": user, "type": "Alert", "document_type": "POS Invoice",
                            "document_name": doc.name, "subject": subject, "from_user": doc.owner}).insert(ignore_permissions=True)
    except Exception:
        frappe.log_error(title=f"POS invoice notification failed: {doc.name}")   # never block the sale


# ------------------------------------------------------------------ bell: things the counter should know about
@frappe.whitelist()
def alerts(pos_profile: str) -> list:
    """POS alerts for the bell: stock running out at the counter, held drafts, unpaid credit sales, a long-open shift,
    coupons about to expire. Each: key, level (critical / warning / info), title, detail, action."""
    out = []
    prof = frappe.get_cached_doc("POS Profile", pos_profile)
    groups = [g.item_group for g in prof.get("item_groups") or []]
    gcond = " and i.item_group in %(g)s" if groups else ""
    rows = frappe.db.sql(f"""select i.item_code, i.item_name, b.actual_qty, ifnull(r.warehouse_reorder_level, 0)
        from tabBin b join tabItem i on i.name = b.item_code
        left join `tabItem Reorder` r on r.parent = i.name and r.warehouse = b.warehouse
        where b.warehouse = %(w)s and i.disabled = 0 and i.is_sales_item = 1 and i.is_stock_item = 1 {gcond}
          and b.actual_qty <= greatest(ifnull(r.warehouse_reorder_level, 0), 5)
        order by b.actual_qty limit 50""", {"w": prof.warehouse, "g": groups or [""]})
    out_items = [r for r in rows if flt(r[2]) <= 0]
    low_items = [r for r in rows if flt(r[2]) > 0]
    if out_items:
        out.append({"key": "out_of_stock", "level": "critical", "title": f"{len(out_items)} item{'s' if len(out_items) > 1 else ''} out of stock",
                    "detail": ", ".join(r[1] for r in out_items[:4]) + ("…" if len(out_items) > 4 else ""), "action": "stock"})
    if low_items:
        out.append({"key": "low_stock", "level": "warning", "title": f"{len(low_items)} item{'s' if len(low_items) > 1 else ''} running low",
                    "detail": ", ".join(f"{r[1]} ({flt(r[2]):g})" for r in low_items[:4]) + ("…" if len(low_items) > 4 else ""), "action": "stock"})
    held = frappe.db.count("POS Invoice", {"pos_profile": pos_profile, "docstatus": 0, "owner": frappe.session.user})
    if held:
        out.append({"key": "held", "level": "info", "title": f"{held} held sale{'s' if held > 1 else ''} waiting",
                    "detail": "Resume or delete drafts you parked", "action": "held"})
    credit = frappe.db.sql("""select count(*), sum(greatest(if(ifnull(rounded_total, 0) != 0, rounded_total, grand_total) - paid_amount
            - ifnull(write_off_amount, 0) - if(redeem_loyalty_points, ifnull(loyalty_amount, 0), 0), 0))
        from `tabPOS Invoice` where pos_profile = %s and docstatus = 1 and is_return = 0 and posting_date >= %s
          and if(ifnull(rounded_total, 0) != 0, rounded_total, grand_total) - paid_amount - ifnull(write_off_amount, 0)
              - if(redeem_loyalty_points, ifnull(loyalty_amount, 0), 0) > 0.01""", (pos_profile, add_days(nowdate(), -30)))[0]
    if cint(credit[0]):
        out.append({"key": "credit", "level": "warning", "title": f"{cint(credit[0])} credit sale{'s' if cint(credit[0]) > 1 else ''} unpaid",
                    "detail": f"{frappe.format_value(flt(credit[1]), 'Currency')} still to collect (last 30 days)", "action": "credit"})
    shift = _open_shift(pos_profile)
    if shift:
        hours = (get_datetime() - get_datetime(shift.period_start_date)).total_seconds() / 3600
        if hours >= 12:
            out.append({"key": "long_shift", "level": "critical" if getdate(shift.period_start_date) < getdate(nowdate()) else "warning",
                        "title": f"Shift open {int(hours // 24)}d {int(hours % 24)}h" if hours >= 24 else f"Shift open {int(hours)} hours",
                        "detail": "Close it and open a new one — ERPNext won't post sales on a past day's shift", "action": "close"})
    soon = frappe.db.sql("""select coupon_code, valid_upto from `tabCoupon Code` where valid_upto between %s and %s
        and (ifnull(maximum_use, 0) = 0 or used < maximum_use)""", (nowdate(), add_days(nowdate(), 3)))
    if soon:
        out.append({"key": "coupons", "level": "info", "title": f"{len(soon)} coupon{'s' if len(soon) > 1 else ''} expiring soon",
                    "detail": ", ".join(f"{c} ({frappe.format_value(d, 'Date')})" for c, d in soon[:3]), "action": "offers"})
    order = {"critical": 0, "warning": 1, "info": 2}
    return sorted(out, key=lambda a: order[a["level"]])


# ------------------------------------------------------------------ cash receipts / payments at the counter
@frappe.whitelist()
def cash_setup() -> dict:
    """The open shift, its payment modes and the accounts a cash receipt / payment can go against."""
    shift = _open_shift()
    if not shift:
        return {"shift": None}
    prof = frappe.get_cached_doc("POS Profile", shift.pos_profile)
    leaf = {"company": shift.company, "is_group": 0, "disabled": 0}
    return {
        "shift": shift, "company": shift.company, "currency": prof.currency, "pos_profile": prof.name,
        "modes": [p.mode_of_payment for p in prof.payments],
        "expense_accounts": frappe.get_all("Account", {**leaf, "root_type": "Expense"}, pluck="name", order_by="name"),
        "income_accounts": frappe.get_all("Account", {**leaf, "root_type": "Income"}, pluck="name", order_by="name"),
        "cashier": frappe.utils.get_fullname(frappe.session.user), "company_header": _company_header(shift.company),
    }


@frappe.whitelist(methods=["POST"])
def record_cash(data: str) -> dict:
    """A cash receipt (direction "in") or cash payment ("out") at the counter, inside the open shift.
    With a party (Customer / Supplier / Employee): a Payment Entry, allocated to the party's oldest open invoices.
    With an account (income for receipts, expense for payments): a Journal Entry against the mode's cash / bank account."""
    from erpnext.accounts.doctype.sales_invoice.sales_invoice import get_bank_cash_account
    from erpnext.accounts.party import get_party_account

    d = json.loads(data)
    shift = _open_shift()
    if not shift:
        frappe.throw(_("Open a shift first — cash in / out is recorded against the shift."))
    amount, direction, mode = flt(d.get("amount")), d.get("direction"), d.get("mode_of_payment")
    if amount <= 0 or direction not in ("in", "out") or not mode:
        frappe.throw(_("Choose cash in or out, the mode and an amount."))
    company = shift.company
    cash_account = get_bank_cash_account(mode, company).get("account")
    tag = f"POS {shift.name} ({shift.pos_profile})"
    note = (d.get("remarks") or "").strip()
    ref = d.get("reference_no") or tag

    if d.get("party_type") and d.get("party"):
        party_type, party = d["party_type"], d["party"]
        party_account = get_party_account(party_type, party, company)
        pe = frappe.new_doc("Payment Entry")
        pe.update({"payment_type": "Receive" if direction == "in" else "Pay", "party_type": party_type, "party": party, "company": company,
                   "posting_date": nowdate(), "mode_of_payment": mode, "paid_amount": amount, "received_amount": amount,
                   "paid_from": party_account if direction == "in" else cash_account,
                   "paid_to": cash_account if direction == "in" else party_account,
                   "reference_no": ref, "reference_date": nowdate(),
                   "custom_remarks": 1, "remarks": f"{note or ('Cash received' if direction == 'in' else 'Cash paid')} — {tag}"})
        inv_dt = {("Customer", "in"): "Sales Invoice", ("Supplier", "out"): "Purchase Invoice"}.get((party_type, direction))
        left = amount
        if inv_dt:
            party_field = "customer" if inv_dt == "Sales Invoice" else "supplier"
            for inv in frappe.db.sql(f"""select name, outstanding_amount, grand_total, due_date from `tab{inv_dt}` where {party_field} = %s
                    and company = %s and docstatus = 1 and outstanding_amount > 0 order by due_date, posting_date""", (party, company), as_dict=True):
                if left <= 0:
                    break
                alloc = min(left, flt(inv.outstanding_amount))
                pe.append("references", {"reference_doctype": inv_dt, "reference_name": inv.name, "total_amount": inv.grand_total,
                                         "outstanding_amount": inv.outstanding_amount, "allocated_amount": alloc, "due_date": inv.due_date})
                left -= alloc
        pe.setup_party_account_field()
        pe.set_missing_values()
        pe.set_amounts()
        pe.insert()
        pe.submit()
        return {"doctype": "Payment Entry", "name": pe.name, "allocated": flt(amount - left), "direction": direction, "amount": amount}

    account = d.get("account")
    if not account:
        frappe.throw(_("Pick a party or an account."))
    cc = frappe.get_cached_value("POS Profile", shift.pos_profile, "cost_center") or frappe.get_cached_value("Company", company, "cost_center")
    label = (note or account.rsplit(" - ", 1)[0])[:100]
    je = frappe.new_doc("Journal Entry")
    je.update({"voucher_type": "Cash Entry" if "cash" in mode.lower() else "Bank Entry", "company": company, "posting_date": nowdate(),
               "mode_of_payment": mode, "title": f"Cash {'In' if direction == 'in' else 'Out'} · {label}",
               "cheque_no": d.get("reference_no") or None, "cheque_date": nowdate() if d.get("reference_no") else None,
               "user_remark": f"{note or ('Cash receipt' if direction == 'in' else 'Cash payment')} — {tag}"})
    debit, credit = (cash_account, account) if direction == "in" else (account, cash_account)
    je.append("accounts", {"account": debit, "debit_in_account_currency": amount, "cost_center": cc})
    je.append("accounts", {"account": credit, "credit_in_account_currency": amount, "cost_center": cc})
    je.insert()
    je.submit()
    return {"doctype": "Journal Entry", "name": je.name, "direction": direction, "amount": amount}


@frappe.whitelist()
def cash_movements(pos_profile: str, scope: str = "shift", from_date: str | None = None, to_date: str | None = None) -> dict:
    """Cash in / out recorded at a counter: the open shift's (scope "shift") or a date range's, with totals."""
    if scope == "shift":
        shift = _open_shift(pos_profile)
        if not shift:
            return {"rows": [], "in": 0, "out": 0}
        like = f"%POS {shift.name}%"
    else:
        like = f"%({pos_profile})%"
    dates, vals = "", {"like": like}
    if scope != "shift":
        dates = " and posting_date between %(f)s and %(t)s"
        vals.update({"f": from_date or nowdate(), "t": to_date or nowdate()})
    rows = frappe.db.sql(f"""
        select 'Payment Entry' as doctype, name, posting_date, creation, owner, docstatus, mode_of_payment, paid_amount as amount,
            if(payment_type = 'Receive', 'in', 'out') as direction, party_type, party, party_name, null as account, reference_no, remarks
        from `tabPayment Entry` where docstatus < 2 and remarks like %(like)s {dates}
        union all
        select 'Journal Entry', je.name, je.posting_date, je.creation, je.owner, je.docstatus, je.mode_of_payment, je.total_debit,
            if(je.title like 'Cash In%%', 'in', 'out'), null, null, null,
            (select a.account from `tabJournal Entry Account` a where a.parent = je.name
             and a.account not in (select default_account from `tabMode of Payment Account` where parent = je.mode_of_payment) limit 1),
            je.cheque_no, je.user_remark
        from `tabJournal Entry` je where je.docstatus < 2 and je.user_remark like %(like)s {dates.replace('posting_date', 'je.posting_date')}
        order by creation desc limit 300""", vals, as_dict=True)
    for r in rows:
        r.cashier = frappe.utils.get_fullname(r.owner)
        r.remarks = (r.remarks or "").rsplit(" — POS ", 1)[0]
    live = [r for r in rows if r.docstatus == 1]
    return {"rows": rows, "in": flt(sum(r.amount for r in live if r.direction == "in")),
            "out": flt(sum(r.amount for r in live if r.direction == "out"))}


@frappe.whitelist(methods=["POST"])
def cancel_cash(doctype: str, name: str) -> dict:
    """Undo a cash in / out recorded today by this cashier (or a System Manager)."""
    if doctype not in ("Payment Entry", "Journal Entry"):
        frappe.throw(_("Not a cash entry."))
    doc = frappe.get_doc(doctype, name)
    remark = doc.get("remarks") if doctype == "Payment Entry" else doc.get("user_remark")
    if " — POS " not in (remark or ""):
        frappe.throw(_("{0} was not recorded at the counter.").format(name))
    if doc.owner != frappe.session.user and "System Manager" not in frappe.get_roles():
        frappe.throw(_("Only the cashier who recorded {0} can cancel it.").format(name))
    doc.cancel()
    return {"cancelled": name}


# ------------------------------------------------------------------ dashboard
@frappe.whitelist()
def dashboard(company: str, from_date: str | None = None, to_date: str | None = None, pos_profile: str | None = None) -> dict:
    """POS insights for a date range (default: this month) and optional counter, plus fixed period cards
    (today / yesterday / this & last week / this & last month), each compared like-for-like."""
    from datetime import timedelta

    today = getdate(nowdate())
    f = getdate(from_date) if from_date else today.replace(day=1)
    t = getdate(to_date) if to_date else today
    if f > t:
        f, t = t, f
    base = "company = %(c)s and docstatus = 1" + (" and pos_profile = %(p)s" if pos_profile else "")
    args = {"c": company, "p": pos_profile}

    def totals(a, b):
        r = frappe.db.sql(f"""select sum(if(is_return, 0, 1)), sum(is_return), sum(grand_total), sum(if(is_return, 0, grand_total)),
                sum(if(is_return, grand_total, 0)), sum(total_taxes_and_charges), sum({DISC_SQL}), count(distinct customer),
                sum(if(is_return, 0, total_qty)), sum(ifnull(coupon_code, '') != ''), sum(if(redeem_loyalty_points, loyalty_amount, 0)),
                sum(greatest(if(ifnull(rounded_total, 0) != 0, rounded_total, grand_total) - paid_amount - ifnull(write_off_amount, 0)
                    - if(redeem_loyalty_points, ifnull(loyalty_amount, 0), 0), 0))
            from `tabPOS Invoice` where {base} and posting_date between %(a)s and %(b)s""", {**args, "a": a, "b": b})[0]
        sales = int(r[0] or 0)
        return {"count": sales, "returns": int(r[1] or 0), "net": flt(r[2]), "gross": flt(r[3]), "refunds": flt(r[4]), "tax": flt(r[5]),
                "discounts": flt(r[6]), "customers": int(r[7] or 0), "qty": flt(r[8]), "coupons": int(r[9] or 0), "loyalty": flt(r[10]),
                "credit": flt(r[11]), "avg": flt(r[3]) / sales if sales else 0}

    def card(label, a, b, pa, pb, vs):
        cur, prev = totals(a, b), totals(pa, pb)
        return {"key": label.lower().replace(" ", "_"), "label": label, "from": str(a), "to": str(b), "net": cur["net"], "count": cur["count"],
                "avg": cur["avg"], "prev_net": prev["net"], "prev_count": prev["count"], "vs": vs}

    week_start = today - timedelta(days=today.weekday())
    lw_start, lw_end = week_start - timedelta(days=7), week_start - timedelta(days=1)
    month_start = today.replace(day=1)
    lm_end = month_start - timedelta(days=1)
    lm_start = lm_end.replace(day=1)
    pm_end = lm_start - timedelta(days=1)
    same_day_lm = min(lm_start + timedelta(days=(today - month_start).days), lm_end)
    periods = [
        card("Today", today, today, today - timedelta(days=1), today - timedelta(days=1), "vs yesterday"),
        card("Yesterday", today - timedelta(days=1), today - timedelta(days=1), today - timedelta(days=2), today - timedelta(days=2), "vs day before"),
        card("This week", week_start, today, lw_start, lw_start + (today - week_start), "vs same days last week"),
        card("Last week", lw_start, lw_end, lw_start - timedelta(days=7), lw_end - timedelta(days=7), "vs week before"),
        card("This month", month_start, today, lm_start, same_day_lm, "vs same days last month"),
        card("Last month", lm_start, lm_end, pm_end.replace(day=1), pm_end, "vs month before"),
    ]

    span = (t - f).days + 1
    pf, pt = f - timedelta(days=span), f - timedelta(days=1)
    cur, prev = totals(f, t), totals(pf, pt)
    rng = {**args, "a": f, "b": t}
    daily = dict(frappe.db.sql(f"select posting_date, sum(grand_total) from `tabPOS Invoice` where {base} and posting_date between %(a)s and %(b)s group by 1", rng))
    prev_daily = dict(frappe.db.sql(f"select posting_date, sum(grand_total) from `tabPOS Invoice` where {base} and posting_date between %(a)s and %(b)s group by 1",
                                    {**args, "a": pf, "b": pt}))
    series = [{"date": str(f + timedelta(days=i)), "amount": flt(daily.get(f + timedelta(days=i))),
               "prev": flt(prev_daily.get(pf + timedelta(days=i)))} for i in range(min(span, 370))]
    hourly = frappe.db.sql(f"""select hour(posting_time), count(*), sum(grand_total) from `tabPOS Invoice` where {base} and is_return = 0
        and posting_date between %(a)s and %(b)s group by 1""", rng)
    heat = frappe.db.sql(f"""select weekday(posting_date), hour(posting_time), count(*), sum(grand_total) from `tabPOS Invoice`
        where {base} and is_return = 0 and posting_date between %(a)s and %(b)s group by 1, 2""", rng)
    by_mode = frappe.db.sql(f"""select p.mode_of_payment, sum(p.amount), count(distinct p.parent) from `tabSales Invoice Payment` p
        join `tabPOS Invoice` i on i.name = p.parent where p.parenttype = 'POS Invoice' and p.amount != 0
        and i.{base.replace(' and ', ' and i.')} and i.posting_date between %(a)s and %(b)s group by 1 order by 2 desc""", rng)
    by_cashier = frappe.db.sql(f"""select owner, count(*), sum(grand_total), sum(is_return) from `tabPOS Invoice` where {base}
        and posting_date between %(a)s and %(b)s group by 1 order by 3 desc limit 10""", rng)
    by_profile = frappe.db.sql(f"""select pos_profile, count(*), sum(grand_total) from `tabPOS Invoice` where {base}
        and posting_date between %(a)s and %(b)s group by 1 order by 3 desc""", rng)
    top_items = frappe.db.sql(f"""select it.item_code, it.item_name, sum(it.qty), sum(it.amount), count(distinct it.parent) from `tabPOS Invoice Item` it
        join `tabPOS Invoice` i on i.name = it.parent where i.{base.replace(' and ', ' and i.')} and i.posting_date between %(a)s and %(b)s
        group by it.item_code order by 4 desc limit 10""", rng)
    groups = frappe.db.sql(f"""select it.item_group, sum(it.amount) from `tabPOS Invoice Item` it join `tabPOS Invoice` i on i.name = it.parent
        where i.{base.replace(' and ', ' and i.')} and i.posting_date between %(a)s and %(b)s group by 1 order by 2 desc""", rng)
    top_customers = frappe.db.sql(f"""select customer, customer_name, count(*), sum(grand_total), max(posting_date) from `tabPOS Invoice`
        where {base} and posting_date between %(a)s and %(b)s group by customer order by 4 desc limit 8""", rng)
    open_shifts = frappe.get_all("POS Opening Entry", {"company": company, "docstatus": 1, "pos_closing_entry": ["in", ["", None]],
                                                       **({"pos_profile": pos_profile} if pos_profile else {})},
                                 ["name", "pos_profile", "user", "period_start_date"], order_by="period_start_date desc")
    closings = frappe.get_all("POS Closing Entry", {"company": company, "docstatus": 1, "posting_date": ["between", [f, t]],
                                                    **({"pos_profile": pos_profile} if pos_profile else {})},
                              ["name", "pos_profile", "user", "period_end_date", "grand_total", "status"], order_by="period_end_date desc", limit=8)
    return {
        "range": {"from": str(f), "to": str(t), "days": span, "prev_from": str(pf), "prev_to": str(pt)},
        "periods": periods, "summary": cur, "previous": prev, "series": series,
        "hourly": [{"hour": int(h), "count": int(c), "amount": flt(a)} for h, c, a in hourly],
        "heat": [{"weekday": int(w), "hour": int(h), "count": int(c), "amount": flt(a)} for w, h, c, a in heat],
        "by_mode": [{"mode": m, "amount": flt(a), "count": int(n)} for m, a, n in by_mode],
        "by_cashier": [{"user": frappe.utils.get_fullname(u), "count": int(c), "amount": flt(a), "returns": int(r or 0)} for u, c, a, r in by_cashier],
        "by_profile": [{"profile": pr, "count": int(c), "amount": flt(a)} for pr, c, a in by_profile],
        "top_items": [{"code": c, "item": n, "qty": flt(q), "amount": flt(a), "bills": int(b)} for c, n, q, a, b in top_items],
        "groups": [{"group": g or "Other", "amount": flt(a)} for g, a in groups],
        "top_customers": [{"customer": c, "name": n, "count": int(k), "amount": flt(a), "last": str(l)} for c, n, k, a, l in top_customers],
        "profiles": frappe.get_all("POS Profile", {"company": company, "disabled": 0}, pluck="name"),
        "open_shifts": [{**x, "user": frappe.utils.get_fullname(x.user)} for x in open_shifts],
        "closings": closings,
        # kept for older clients
        "today": {"count": periods[0]["count"], "amount": periods[0]["net"]}, "month": {"count": periods[4]["count"], "amount": periods[4]["net"]},
    }
