"""Point of sale for the React POS terminal and POS dashboard — built on ERPNext's own POS documents.

POS Opening Entry (a cashier's shift with opening cash) → POS Invoices (sale, return, held as draft) → POS Closing
Entry (counted vs expected per mode of payment; ERPNext consolidates the shift's POS Invoices into Sales Invoices).
"""

import json

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, get_datetime, getdate, nowdate


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
        if l.get("discount_percentage") is not None:
            row.discount_percentage = flt(l["discount_percentage"])
            base = flt(l["rate"]) if l.get("rate") is not None else flt(row.price_list_rate)
            row.rate = flt(base * (1 - row.discount_percentage / 100), row.precision("rate"))
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
                          ["name", "grand_total", "is_return"])


@frappe.whitelist()
def shift_summary() -> dict:
    shift = _open_shift()
    if not shift:
        return {}
    opening = frappe.get_doc("POS Opening Entry", shift.name)
    invs = _shift_invoices(shift)
    names = [i.name for i in invs] or [""]
    by_mode = dict(frappe.db.sql("""select mode_of_payment, sum(amount) from `tabSales Invoice Payment`
        where parenttype = 'POS Invoice' and parent in %s group by mode_of_payment""", (names,)))
    change = flt(frappe.db.sql("select sum(change_amount) from `tabPOS Invoice` where name in %s", (names,))[0][0])
    modes = []
    for b in opening.balance_details:
        expected = flt(b.opening_amount) + flt(by_mode.get(b.mode_of_payment))
        if b.mode_of_payment.lower() == "cash":
            expected -= change
        dues = flt(_dues_collected(shift).get(b.mode_of_payment))
        modes.append({"mode_of_payment": b.mode_of_payment, "opening": flt(b.opening_amount), "sales": flt(by_mode.get(b.mode_of_payment)),
                      "dues": dues, "expected": flt(expected + dues, 2)})
    return {"shift": shift, "invoices": len([i for i in invs if not i.is_return]), "returns": len([i for i in invs if i.is_return]),
            "total": flt(sum(i.grand_total for i in invs), 2), "modes": modes}


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
    """Customer dues the cashier collected at the counter during the shift (Payment Entries), per mode of payment."""
    return {m: flt(a) for m, a in frappe.db.sql("""select mode_of_payment, sum(paid_amount) from `tabPayment Entry`
        where docstatus = 1 and payment_type = 'Receive' and owner = %s and creation >= %s and remarks like %s group by 1""",
                                                 (frappe.session.user, shift.period_start_date, f"%POS {shift.name}%"))}


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
            "cashier": frappe.utils.get_fullname(frappe.session.user), "user": frappe.session.user,
            "generated_at": frappe.utils.now(), "currency_symbol": frappe.db.get_value("Currency", prof["currency"], "symbol") or prof["currency"]}


def _company_header(company):
    c = frappe.db.get_value("Company", company, ["company_name", "tax_id", "phone_no", "email", "website"], as_dict=True) or {}
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
    cond.append({
        "all": "docstatus < 2", "paid": f"docstatus = 1 and is_return = 0 and {due} < 0.01", "credit": f"docstatus = 1 and is_return = 0 and {due} >= 0.01",
        "return": "docstatus = 1 and is_return = 1", "held": "docstatus = 0", "offline": "docstatus = 1 and ifnull(mm_offline_id, '') != ''",
        "consolidated": "docstatus = 1 and ifnull(consolidated_invoice, '') != ''", "cancelled": "docstatus = 2",
    }.get(status, "docstatus < 2"))
    where = " and ".join(cond)
    rows = frappe.db.sql(f"""select name, customer, customer_name, posting_date, posting_time, grand_total, paid_amount, {due} as due,
            is_return, return_against, status, docstatus, consolidated_invoice, owner, mm_offline_id, coupon_code, remarks
        from `tabPOS Invoice` where {where} order by creation desc limit %(s)s, %(n)s""", {**vals, "s": cint(start), "n": cint(page_length)}, as_dict=True)
    tot = frappe.db.sql(f"select count(*), sum(grand_total), sum({due}) from `tabPOS Invoice` where {where}", vals)[0]
    for r in rows:
        r.cashier = frappe.utils.get_fullname(r.owner)
    return {"rows": rows, "count": int(tot[0] or 0), "amount": flt(tot[1]), "due": flt(tot[2])}


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
    head = frappe.db.sql("""select count(*), sum(if(is_return, 0, 1)), sum(if(is_return, grand_total, 0)), sum(if(is_return, 0, grand_total)),
            sum(net_total), sum(total_taxes_and_charges), sum(ifnull(discount_amount, 0)), sum(change_amount), sum(ifnull(write_off_amount, 0)),
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
               "remarks": f"Dues collected at the counter — POS {shift.name} ({shift.pos_profile})"})
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


# ------------------------------------------------------------------ dashboard
@frappe.whitelist()
def dashboard(company: str) -> dict:
    today = getdate(nowdate())
    month_start = today.replace(day=1)

    def total(since):
        r = frappe.db.sql("""select count(*), sum(grand_total), sum(is_return) from `tabPOS Invoice`
            where company = %s and docstatus = 1 and posting_date >= %s""", (company, since))[0]
        return {"count": int(r[0] or 0), "amount": flt(r[1]), "returns": int(r[2] or 0)}

    by_mode = frappe.db.sql("""select p.mode_of_payment, sum(p.amount) from `tabSales Invoice Payment` p join `tabPOS Invoice` i on i.name = p.parent
        where p.parenttype = 'POS Invoice' and i.company = %s and i.docstatus = 1 and i.posting_date = %s group by 1 order by 2 desc""",
                            (company, today))
    by_cashier = frappe.db.sql("""select i.owner, i.pos_profile, count(*), sum(i.grand_total) from `tabPOS Invoice` i
        where i.company = %s and i.docstatus = 1 and i.posting_date >= %s group by 1, 2 order by 4 desc""", (company, month_start))
    top_items = frappe.db.sql("""select it.item_name, sum(it.qty), sum(it.amount) from `tabPOS Invoice Item` it
        join `tabPOS Invoice` i on i.name = it.parent where i.company = %s and i.docstatus = 1 and i.posting_date >= %s
        group by it.item_code order by 3 desc limit 8""", (company, month_start))
    hourly = frappe.db.sql("""select hour(posting_time), sum(grand_total) from `tabPOS Invoice` where company = %s and docstatus = 1
        and posting_date = %s group by 1""", (company, today))
    daily = frappe.db.sql("""select posting_date, sum(grand_total) from `tabPOS Invoice` where company = %s and docstatus = 1
        and posting_date >= %s group by 1 order by 1""", (company, add_days(today, -13)))
    open_shifts = frappe.get_all("POS Opening Entry", {"company": company, "docstatus": 1, "pos_closing_entry": ["in", ["", None]]},
                                 ["name", "pos_profile", "user", "period_start_date"], order_by="period_start_date desc")
    closings = frappe.get_all("POS Closing Entry", {"company": company, "docstatus": 1}, ["name", "pos_profile", "user", "period_end_date",
                              "grand_total", "status"], order_by="period_end_date desc", limit=5)
    extras = frappe.db.sql("""select
            sum(greatest(if(ifnull(rounded_total, 0) != 0, rounded_total, grand_total) - paid_amount - ifnull(write_off_amount, 0)
                - if(redeem_loyalty_points, ifnull(loyalty_amount, 0), 0), 0)),
            sum(ifnull(coupon_code, '') != ''), sum(if(redeem_loyalty_points, loyalty_amount, 0)), sum(if(is_return, -grand_total, 0)),
            sum(discount_amount), count(distinct customer)
        from `tabPOS Invoice` where company = %s and docstatus = 1 and posting_date >= %s""", (company, month_start))[0]
    return {
        "today": total(today), "month": total(month_start),
        "month_extras": {"credit": flt(extras[0]), "coupons": int(extras[1] or 0), "loyalty": flt(extras[2]), "refunds": flt(extras[3]),
                         "bill_discounts": flt(extras[4]), "customers": int(extras[5] or 0)},
        "by_mode": [{"mode": m, "amount": flt(a)} for m, a in by_mode],
        "by_cashier": [{"user": frappe.utils.get_fullname(u), "profile": p, "count": int(c), "amount": flt(a)} for u, p, c, a in by_cashier],
        "top_items": [{"item": n, "qty": flt(q), "amount": flt(a)} for n, q, a in top_items],
        "hourly": [{"hour": int(h), "amount": flt(a)} for h, a in hourly],
        "daily": [{"date": str(d), "amount": flt(a)} for d, a in daily],
        "open_shifts": [{**s, "user": frappe.utils.get_fullname(s.user)} for s in open_shifts],
        "closings": closings,
    }
