"""Linked documents for the React form's "Connections" tab (the desk's dashboard links, as data).

Each group is a SQL returning (name, status, date, detail) rows that reference the document; groups the user
cannot read are skipped."""

import frappe
from frappe import _

LIMIT = 50


def _wo_names(plan):
	return frappe.db.sql_list("select name from `tabWork Order` where production_plan = %s and docstatus < 2", plan)


def _plan_groups(name):
	wos = _wo_names(name) or [""]
	return [
		(_("Manufacturing"), "Work Order", """select name, status, work_order_date date, concat(item_name, ' · ', round(produced_qty), ' / ', round(qty)) detail
			from `tabWork Order` where production_plan = %(n)s and docstatus < 2 order by work_order_date desc""", {}),
		(_("Manufacturing"), "Job Card", """select name, status, posting_date date, concat(operation, ' · ', ifnull(workstation, '')) detail
			from `tabJob Card` where work_order in %(w)s and docstatus < 2 order by posting_date desc, sequence_id""", {"w": wos}),
		(_("Stock"), "Stock Entry", """select name, stock_entry_type status, posting_date date, work_order detail
			from `tabStock Entry` where work_order in %(w)s and docstatus < 2 order by posting_date desc""", {"w": wos}),
		(_("Stock"), "Material Request", """select distinct p.name, p.status, p.transaction_date date, p.material_request_type detail
			from `tabMaterial Request` p join `tabMaterial Request Item` i on i.parent = p.name
			where i.production_plan = %(n)s and p.docstatus < 2 order by p.transaction_date desc""", {}),
		(_("Sales"), "Sales Order", """select distinct so.name, so.status, so.transaction_date date, so.customer detail from `tabSales Order` so
			where so.name in (select sales_order from `tabProduction Plan Sales Order` where parent = %(n)s
			                  union select sales_order from `tabProduction Plan Item` where parent = %(n)s and ifnull(sales_order, '') != '')
			order by so.transaction_date desc""", {}),
	]


def _bom_groups(name):
	return [
		(_("Item"), "Item", """select i.name, if(i.disabled, 'Disabled', 'Enabled') status, null date, i.item_name detail
			from `tabItem` i where i.name = (select item from `tabBOM` where name = %(n)s)""", {}),
		(_("Item"), "BOM", """select distinct b.name, if(b.is_active, if(b.is_default, 'Default', 'Active'), 'Inactive') status, date(b.creation) date,
			concat('Uses this BOM · ', b.item_name) detail
			from `tabBOM` b join `tabBOM Item` i on i.parent = b.name where i.bom_no = %(n)s and b.name != %(n)s and b.docstatus < 2""", {}),
		(_("Manufacturing"), "Production Plan", """select distinct p.name, p.status, p.posting_date date, concat(round(i.planned_qty), ' planned') detail
			from `tabProduction Plan` p join `tabProduction Plan Item` i on i.parent = p.name
			where i.bom_no = %(n)s and p.docstatus < 2 order by p.posting_date desc""", {}),
		(_("Manufacturing"), "Work Order", """select name, status, work_order_date date, concat(round(produced_qty), ' / ', round(qty)) detail
			from `tabWork Order` where bom_no = %(n)s and docstatus < 2 order by work_order_date desc""", {}),
		(_("Manufacturing"), "Job Card", """select name, status, posting_date date, concat(operation, ' · ', ifnull(workstation, '')) detail
			from `tabJob Card` where bom_no = %(n)s and docstatus < 2 order by posting_date desc""", {}),
		(_("Stock"), "Stock Entry", """select name, stock_entry_type status, posting_date date, work_order detail
			from `tabStock Entry` where bom_no = %(n)s and docstatus < 2 order by posting_date desc""", {}),
	]


def _wo_groups(name):
	return [
		(_("Manufacturing"), "Job Card", """select name, status, posting_date date, concat(operation, ' · ', ifnull(workstation, '')) detail
			from `tabJob Card` where work_order = %(n)s and docstatus < 2 order by sequence_id, posting_date""", {}),
		(_("Manufacturing"), "Downtime Entry", """select name, ifnull(stop_reason, 'Not set') status, date(from_time) date,
			concat(ifnull(workstation, ''), ' · ', round(downtime), ' min') detail
			from `tabDowntime Entry` where work_order = %(n)s and docstatus < 2 order by from_time desc""", {}),
		(_("Stock"), "Stock Entry", """select name, stock_entry_type status, posting_date date, concat(round(fg_completed_qty), ' qty') detail
			from `tabStock Entry` where work_order = %(n)s and docstatus < 2 order by posting_date desc""", {}),
		(_("Quality"), "Quality Inspection", """select name, status, report_date date, concat(inspection_type, ' · ', reference_name) detail
			from `tabQuality Inspection` where docstatus < 2 and (
				(reference_type = 'Stock Entry' and reference_name in (select name from `tabStock Entry` where work_order = %(n)s))
				or (reference_type = 'Job Card' and reference_name in (select name from `tabJob Card` where work_order = %(n)s)))
			order by report_date desc""", {}),
		(_("Planning"), "Production Plan", """select name, status, posting_date date, null detail from `tabProduction Plan`
			where name = (select production_plan from `tabWork Order` where name = %(n)s)""", {}),
		(_("Planning"), "BOM", """select name, if(is_active, if(is_default, 'Default', 'Active'), 'Inactive') status, null date, item_name detail
			from `tabBOM` where name = (select bom_no from `tabWork Order` where name = %(n)s)""", {}),
		(_("Planning"), "Sales Order", """select name, status, transaction_date date, customer detail from `tabSales Order`
			where name = (select sales_order from `tabWork Order` where name = %(n)s)""", {}),
	]


def _jc_groups(name):
	return [
		(_("Manufacturing"), "Work Order", """select name, status, work_order_date date, item_name detail from `tabWork Order`
			where name = (select work_order from `tabJob Card` where name = %(n)s)""", {}),
		(_("Manufacturing"), "Job Card", """select name, status, posting_date date, concat('Corrective · ', operation) detail
			from `tabJob Card` where for_job_card = %(n)s and docstatus < 2 order by posting_date desc""", {}),
		(_("Manufacturing"), "Downtime Entry", """select d.name, ifnull(d.stop_reason, 'Not set') status, date(d.from_time) date,
			concat(round(d.downtime), ' min') detail
			from `tabDowntime Entry` d join `tabJob Card` j on j.name = %(n)s
			where d.work_order = j.work_order and d.workstation = j.workstation and d.docstatus < 2 order by d.from_time desc""", {}),
		(_("Stock"), "Stock Entry", """select name, stock_entry_type status, posting_date date, work_order detail
			from `tabStock Entry` where job_card = %(n)s and docstatus < 2 order by posting_date desc""", {}),
		(_("Quality"), "Quality Inspection", """select name, status, report_date date, inspection_type detail from `tabQuality Inspection`
			where docstatus < 2 and ((reference_type = 'Job Card' and reference_name = %(n)s)
			                         or name = (select quality_inspection from `tabJob Card` where name = %(n)s))""", {}),
		(_("People"), "Employee", """select distinct e.name, e.status, null date, concat(e.employee_name, ' · ', round(sum(l.time_in_mins) / 60, 1), ' h') detail
			from `tabJob Card Time Log` l join `tabEmployee` e on e.name = l.employee
			where l.parent = %(n)s group by e.name, e.status, e.employee_name order by sum(l.time_in_mins) desc""", {}),
	]


def _ws_groups(name):
	return [
		(_("Manufacturing"), "Job Card", """select name, status, posting_date date, concat(operation, ' · ', ifnull(item_name, '')) detail
			from `tabJob Card` where workstation = %(n)s and docstatus < 2 order by posting_date desc""", {}),
		(_("Manufacturing"), "Work Order", """select distinct w.name, w.status, w.work_order_date date, w.item_name detail
			from `tabWork Order` w join `tabJob Card` j on j.work_order = w.name where j.workstation = %(n)s and w.docstatus < 2
			order by w.work_order_date desc""", {}),
		(_("Manufacturing"), "Downtime Entry", """select name, ifnull(stop_reason, 'Not set') status, date(from_time) date, concat(round(downtime), ' min') detail
			from `tabDowntime Entry` where workstation = %(n)s and docstatus < 2 order by from_time desc""", {}),
		(_("Setup"), "Routing", """select distinct r.name, if(r.disabled, 'Disabled', 'Enabled') status, null date, o.operation detail
			from `tabRouting` r join `tabBOM Operation` o on o.parent = r.name and o.parenttype = 'Routing' where o.workstation = %(n)s""", {}),
		(_("Setup"), "Operation", """select name, 'Default workstation' status, null date, operation_type detail from `tabOperation` where workstation = %(n)s""", {}),
	]


def _routing_groups(name):
	return [
		(_("Setup"), "BOM", """select name, if(is_active, if(is_default, 'Default', 'Active'), 'Inactive') status, date(creation) date, item_name detail
			from `tabBOM` where routing = %(n)s and docstatus < 2 order by creation desc""", {}),
		(_("Setup"), "Operation", """select distinct op.name, op.operation_type status, null date, concat('Step ', o.idx) detail
			from `tabBOM Operation` o join `tabOperation` op on op.name = o.operation where o.parent = %(n)s and o.parenttype = 'Routing' order by o.idx""", {}),
		(_("Setup"), "Workstation", """select distinct w.name, ifnull(w.status, 'Not set') status, null date, w.workstation_type detail
			from `tabBOM Operation` o join `tabWorkstation` w on w.name = o.workstation where o.parent = %(n)s and o.parenttype = 'Routing'""", {}),
	]


def _operation_groups(name):
	return [
		(_("Setup"), "Routing", """select distinct r.name, if(r.disabled, 'Disabled', 'Enabled') status, null date, concat('Step ', o.idx) detail
			from `tabRouting` r join `tabBOM Operation` o on o.parent = r.name and o.parenttype = 'Routing' where o.operation = %(n)s""", {}),
		(_("Setup"), "BOM", """select distinct b.name, if(b.is_active, 'Active', 'Inactive') status, null date, b.item_name detail
			from `tabBOM` b join `tabBOM Operation` o on o.parent = b.name and o.parenttype = 'BOM' where o.operation = %(n)s and b.docstatus < 2""", {}),
		(_("Setup"), "Workstation", """select name, ifnull(status, 'Not set') status, null date, workstation_type detail from `tabWorkstation`
			where operation = %(n)s or name = (select workstation from `tabOperation` where name = %(n)s)""", {}),
		(_("Manufacturing"), "Job Card", """select name, status, posting_date date, concat(ifnull(workstation, ''), ' · ', ifnull(item_name, '')) detail
			from `tabJob Card` where operation = %(n)s and docstatus < 2 order by posting_date desc""", {}),
	]


def _ws_type_groups(name):
	return [
		(_("Setup"), "Workstation", """select name, ifnull(status, 'Not set') status, null date, concat(ifnull(spindles, 0), ' spindles') detail
			from `tabWorkstation` where workstation_type = %(n)s order by name""", {}),
		(_("Manufacturing"), "Job Card", """select name, status, posting_date date, concat(operation, ' · ', ifnull(workstation, '')) detail
			from `tabJob Card` where workstation_type = %(n)s and docstatus < 2 order by posting_date desc""", {}),
	]


def _downtime_groups(name):
	return [
		(_("Manufacturing"), "Work Order", """select name, status, work_order_date date, item_name detail from `tabWork Order`
			where name = (select work_order from `tabDowntime Entry` where name = %(n)s)""", {}),
		(_("Manufacturing"), "Workstation", """select name, ifnull(status, 'Not set') status, null date, workstation_type detail from `tabWorkstation`
			where name = (select workstation from `tabDowntime Entry` where name = %(n)s)""", {}),
		(_("Manufacturing"), "Downtime Entry", """select d.name, ifnull(d.stop_reason, 'Not set') status, date(d.from_time) date, concat(round(d.downtime), ' min') detail
			from `tabDowntime Entry` d join `tabDowntime Entry` me on me.name = %(n)s
			where d.workstation = me.workstation and d.name != me.name and d.docstatus < 2 order by d.from_time desc""", {}),
		(_("People"), "Employee", """select name, status, null date, employee_name detail from `tabEmployee`
			where name = (select operator from `tabDowntime Entry` where name = %(n)s)""", {}),
	]


def _lc_groups(name):
	so = "(select export_order from `tabLC Proforma` where name = %(n)s)"
	return [
		(_("Sales"), "Sales Order", f"""select name, status, transaction_date date, concat(currency, ' ', round(grand_total)) detail from `tabSales Order` where name = {so}""", {}),
		(_("Sales"), "Sales Invoice", f"""select distinct si.name, si.status, si.posting_date date, concat(si.currency, ' ', round(si.grand_total)) detail
			from `tabSales Invoice` si join `tabSales Invoice Item` i on i.parent = si.name where i.sales_order = {so} and si.docstatus < 2 order by si.posting_date desc""", {}),
		(_("Shipping"), "Export Shipment", """select name, ifnull(shipment_status, 'Draft') status, shipment_date date, concat(ifnull(vessel, ''), ' · ', ifnull(bill_of_lading_no, '')) detail
			from `tabExport Shipment` where lc_proforma = %(n)s order by shipment_date desc""", {}),
		(_("Shipping"), "Delivery Note", f"""select distinct dn.name, dn.status, dn.posting_date date, concat(round(dn.total_qty), ' qty') detail
			from `tabDelivery Note` dn join `tabDelivery Note Item` i on i.parent = dn.name where i.against_sales_order = {so} and dn.docstatus < 2 order by dn.posting_date desc""", {}),
	]


def _pe_groups(name):
	"""Payment Entry: the documents it settles (one group per reference type), and the party."""
	groups = []
	for dt in frappe.db.sql_list("select distinct reference_doctype from `tabPayment Entry Reference` where parent = %s", name):
		m = frappe.get_meta(dt)
		date = "posting_date" if m.has_field("posting_date") else "transaction_date" if m.has_field("transaction_date") else "creation"
		status = "p.status" if m.has_field("status") else "'Submitted'"
		due = "concat(' · now due ', format(p.outstanding_amount, 2))" if m.has_field("outstanding_amount") else "''"
		groups.append((_("Settled"), dt, f"""select p.name, {status} status, date(p.`{date}`) date, concat('Allocated ', format(r.allocated_amount, 2), {due}) detail
			from `tabPayment Entry Reference` r join `tab{dt}` p on p.name = r.reference_name
			where r.parent = %(n)s and r.reference_doctype = '{dt}' order by p.`{date}`""", {}))
	pt, party = frappe.db.get_value("Payment Entry", name, ["party_type", "party"]) or (None, None)
	if pt in ("Customer", "Supplier", "Employee") and party:
		title = {"Customer": "customer_name", "Supplier": "supplier_name", "Employee": "employee_name"}[pt]
		groups.append((_("Party"), pt, f"""select name, if(disabled, 'Disabled', 'Active') status, null date, `{title}` detail from `tab{pt}` where name = %(party)s"""
		                if pt != "Employee" else f"""select name, status, null date, `{title}` detail from `tab{pt}` where name = %(party)s""", {"party": party}))
		groups.append((_("Party"), "Payment Entry", """select name, status, posting_date date, concat(payment_type, ' · ', format(paid_amount, 2)) detail
			from `tabPayment Entry` where party_type = %(pt)s and party = %(party)s and name != %(n)s and docstatus < 2 order by posting_date desc""", {"pt": pt, "party": party}))
	return groups


def _crm_common(dt, name):
	"""Follow-ups, notes, calls and emails hang off a CRM record through reference_doctype / reference_docname."""
	ref = {"rdt": dt}
	return [
		(_("Activity"), "CRM Task", """select name, status, due_date date, concat(ifnull(title, ''), ' · ', ifnull(assigned_to, '')) detail
			from `tabCRM Task` where reference_doctype = %(rdt)s and reference_docname = %(n)s order by due_date desc""", ref),
		(_("Activity"), "FCRM Note", """select name, 'Note' status, date(modified) date, title detail from `tabFCRM Note`
			where reference_doctype = %(rdt)s and reference_docname = %(n)s order by modified desc""", ref),
		(_("Activity"), "CRM Call Log", """select name, status, date(start_time) date, concat(ifnull(type, ''), ' · ', ifnull(`from`, ''), ' → ', ifnull(`to`, '')) detail
			from `tabCRM Call Log` where reference_doctype = %(rdt)s and reference_docname = %(n)s order by start_time desc""", ref),
		(_("Activity"), "Communication", """select name, sent_or_received status, date(communication_date) date, subject detail from `tabCommunication`
			where reference_doctype = %(rdt)s and reference_name = %(n)s order by communication_date desc""", ref),
	]


def _crm_lead_groups(name):
	return [
		(_("Pipeline"), "CRM Deal", """select name, status, date(creation) date, concat(ifnull(organization, ''), ' · ', ifnull(deal_owner, '')) detail
			from `tabCRM Deal` where lead = %(n)s order by creation desc""", {}),
		(_("Pipeline"), "CRM Organization", """select name, ifnull(industry, '') status, null date, ifnull(website, '') detail from `tabCRM Organization`
			where name = (select organization from `tabCRM Lead` where name = %(n)s)""", {}),
		*_crm_common("CRM Lead", name),
	]


def _crm_deal_groups(name):
	return [
		(_("Pipeline"), "CRM Lead", """select name, status, date(creation) date, concat(ifnull(lead_name, ''), ' · ', ifnull(email, '')) detail
			from `tabCRM Lead` where name = (select lead from `tabCRM Deal` where name = %(n)s)""", {}),
		(_("Pipeline"), "CRM Organization", """select name, ifnull(industry, '') status, null date, ifnull(website, '') detail from `tabCRM Organization`
			where name = (select organization from `tabCRM Deal` where name = %(n)s)""", {}),
		(_("Pipeline"), "Contact", """select c.name, ifnull(c.status, '') status, null date, concat(ifnull(c.full_name, ''), ' · ', ifnull(c.email_id, '')) detail
			from `tabCRM Contacts` x join `tabContact` c on c.name = x.contact where x.parent = %(n)s and x.parenttype = 'CRM Deal'""", {}),
		*_crm_common("CRM Deal", name),
	]


SPECS = {"CRM Lead": _crm_lead_groups, "CRM Deal": _crm_deal_groups, "Payment Entry": _pe_groups, "LC Proforma": _lc_groups, "Production Plan": _plan_groups, "BOM": _bom_groups, "Work Order": _wo_groups, "Job Card": _jc_groups, "Workstation": _ws_groups,
         "Routing": _routing_groups, "Operation": _operation_groups, "Workstation Type": _ws_type_groups, "Downtime Entry": _downtime_groups}


DATE_FIELDS = ("posting_date", "transaction_date", "schedule_date", "attendance_date", "from_date", "start_date", "opening_date", "report_date", "date")
TITLE_FIELDS = ("title", "customer_name", "supplier_name", "employee_name", "item_name", "party_name", "subject", "lead_name", "project_name")


def _generic_groups(doctype, name):
	"""Any DocType: its desk "Connections" definition (`<doctype>_dashboard.py` → transactions), resolved to SQL.
	A linked DocType is matched on its own field (fieldname / non_standard_fieldnames), on a child-table row carrying
	that field, or — for internal links — on the names this document itself points to."""
	data = frappe.get_meta(doctype).get_dashboard_data() or {}
	base_field = data.get("fieldname") or frappe.scrub(doctype)
	nonstd = data.get("non_standard_fieldnames") or {}
	internal = data.get("internal_links") or {}
	doc = None
	groups = []
	for tr in data.get("transactions") or []:
		for dt in tr.get("items") or []:
			if not frappe.db.exists("DocType", dt):
				continue
			m = frappe.get_meta(dt)
			status = "p.status" if m.has_field("status") else "case p.docstatus when 0 then 'Draft' when 1 then 'Submitted' else 'Cancelled' end"
			date = next((f for f in DATE_FIELDS if m.has_field(f)), "creation")
			title = next((f for f in TITLE_FIELDS if m.has_field(f)), None)
			select = f"select distinct p.name, {status} status, date(p.`{date}`) date, {f'p.`{title}`' if title else 'null'} detail from `tab{dt}` p"
			ds = "and p.docstatus < 2" if m.is_submittable else ""
			order = f"order by date desc"
			if dt in internal:
				doc = doc or frappe.get_doc(doctype, name)
				link = internal[dt]
				names = ({r.get(link[1]) for r in doc.get(link[0]) or []} if isinstance(link, (list, tuple)) else {doc.get(link)})
				names = [x for x in names if x] or [""]
				groups.append((tr.get("label") or _("Related"), dt, f"{select} where p.name in %(names)s {ds} {order}", {"names": names}))
				continue
			field = nonstd.get(dt, base_field)
			if m.has_field(field):
				sql = f"{select} where p.`{field}` = %(n)s {ds} {order}"
			else:
				child = next((t.options for t in m.get_table_fields() if frappe.get_meta(t.options).has_field(field)), None)
				if not child:
					continue
				sql = f"{select} join `tab{child}` c on c.parent = p.name and c.parenttype = %(pdt)s where c.`{field}` = %(n)s {ds} {order}"
			groups.append((tr.get("label") or _("Related"), dt, sql, {"pdt": dt}))
	return groups


def _groups_for(doctype, name):
	return SPECS[doctype](name) if doctype in SPECS else _generic_groups(doctype, name)


@frappe.whitelist()
def get_connections(doctype, name):
	frappe.get_doc(doctype, name).check_permission("read")
	out = []
	for group, dt, sql, extra in _groups_for(doctype, name):
		if not frappe.has_permission(dt, "read"):
			continue
		rows = frappe.db.sql(f"select * from ({sql}) x", {"n": name, **extra}, as_dict=True)
		out.append({"group": group, "doctype": dt, "label": _(dt), "count": len(rows),
		            "rows": [{k: (str(v) if v is not None and k == "date" else v) for k, v in r.items()} for r in rows[:LIMIT]]})
	return out
