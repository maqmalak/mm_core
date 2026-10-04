"""Server side of the React form "Actions" menu: duplicate a document, or map it to the next document in its flow
(Sales Order → Delivery Note, Material Request → Purchase Order, …) with ERPNext's own mapper, saved as a draft."""

import frappe


@frappe.whitelist()
def duplicate(doctype, name):
	"""Copy of the document as a new draft (the desk's "Duplicate")."""
	src = frappe.get_doc(doctype, name)
	src.check_permission("read")
	new = frappe.copy_doc(src)
	new.insert()
	return {"doctype": new.doctype, "name": new.name}


@frappe.whitelist()
def make_mapped(method, source_name=None, args=None):
	"""Run a whitelisted ERPNext mapper (e.g. erpnext.selling.doctype.sales_order.sales_order.make_delivery_note)
	for `source_name` and save what it returns as a draft. Only whitelisted functions are accepted."""
	fn = frappe.get_attr(method)
	frappe.is_whitelisted(fn)  # throws PermissionError unless the function is @frappe.whitelist()-ed
	kwargs = frappe.parse_json(args) if args else {}
	if source_name and not kwargs:
		kwargs = {"source_name": source_name}
	doc = fn(**kwargs)
	if isinstance(doc, dict):
		doc = frappe.get_doc(doc)
	doc.check_permission("create")
	# The desk opens a mapped document unsaved for the user to finish; here it is kept as a draft instead, so fields
	# the user still has to fill (e.g. "Required By") must not block the save. Other validations still apply.
	doc.flags.ignore_mandatory = True
	_default_dates(doc)
	doc.insert()
	return {"doctype": doc.doctype, "name": doc.name}


DATE_DEFAULTS = ("schedule_date", "delivery_date")


def _default_dates(doc, days=7):
	"""Empty "Required By" / delivery dates → today + `days` (header and rows); the user adjusts them on the draft."""
	from frappe.utils import add_days, nowdate

	default = add_days(nowdate(), days)
	for d in [doc, *[r for t in doc.meta.get_table_fields() for r in doc.get(t.fieldname) or []]]:
		for f in DATE_DEFAULTS:
			if d.meta.has_field(f) and not d.get(f):
				d.set(f, doc.get(f) or default)
