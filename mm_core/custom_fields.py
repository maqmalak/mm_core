"""Custom Fields shared by every site on this bench (micromaxerp/demo, wise, ...).

One definition, applied on install and on every migrate, so the same fields exist on each site that has
mm_core installed. A field is skipped on a site that can't hold it: its doctype isn't installed there
(CRM Lead without the crm app) or its Link target doesn't exist. Site-specific fields stay in that site's
own app (micromax for the spinning/export ERP).
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def _build_custom_fields():
    """Returns a dict keyed by doctype with a list of field dicts."""
    data = {}

    def add(dt, fieldname, label, fieldtype, options, insert_after, section=None, description=None, **props):
        field = {
            "fieldname": fieldname,
            "label": label,
            "fieldtype": fieldtype,
            "options": options,
            "insert_after": insert_after,
        }
        if description:
            field["description"] = description
        field.update(props)
        data.setdefault(dt, []).append(field)

    # ---------------------------------------------------------- Cost Center / cost-center abbreviation
    # "Abr" on transactions is fetched from the cost center's abbreviation, so Cost Center comes first.
    add("Cost Center", "abbreviation", "Abbreviation", "Data", None, "cost_center_number")
    add("Journal Entry", "cost_center", "Cost Center", "Link", "Cost Center", "tax_withholding_category", reqd=1)
    add("Material Request", "cost_center", "Cost Center", "Link", "Cost Center", "company", reqd=1)
    for dt in (
        "Journal Entry", "Payment Entry", "Material Request", "Purchase Order", "Purchase Receipt",
        "Purchase Invoice", "Delivery Note", "Sales Invoice",
    ):
        add(dt, "abr", "Abr", "Data", None, "cost_center", fetch_from="cost_center.abbreviation")

    # ---------------------------------------------------------- Buying: purchase location
    add("Material Request", "purchase_location", "Purchase Location", "Select", "Local\nHead Office", "schedule_date", reqd=1)
    add("Purchase Order", "purchase_location", "Purchase Location", "Select", "Local\nHead Office", "schedule_date", reqd=1)
    add("Purchase Receipt", "purchase_location", "Purchase Location", "Select", "Local\nHead Office", "supplier_delivery_note", reqd=1)
    add("Purchase Invoice", "purchase_location", "Purchase Location", "Select", "Local\nHead Office", "tax_withholding_category", reqd=1)
    add("Purchase Order Item", "remarks", "Remarks", "Data", None, "product_bundle")

    # ---------------------------------------------------------- Email Template: CRM scope
    # Email Template is desk-wide (HR/Payroll notifications share it); the React CRM's Email Templates page and
    # the Lead/Deal compose picker list only templates with this flag set.
    add("Email Template", "crm_template", "CRM Template", "Check", None, "subject", default="0",
        description="Shown in the CRM's Email Templates list and the Lead/Deal compose box template picker.")

    # ---------------------------------------------------------- CRM Task: Tasks vs Follow-ups
    add("CRM Task", "task_category", "Category", "Select", "\nTask\nFollow-up", "title",
        description="Distinguishes the generic Tasks list from the Lead/Deal Follow-ups mechanism (same doctype, different UI lists).")

    # ---------------------------------------------------------- Payment Entry sign-off
    add("Payment Entry", "prepared_by", "Prepared By", "Data", None, "remarks")
    add("Payment Entry", "checked_by", "Checked By", "Data", None, "prepared_by")
    add("Payment Entry", "approved_by", "Approved By", "Data", None, "checked_by")

    # ---------------------------------------------------------- Sales Order notes / packing / commission
    add("Sales Order", "commission_based_on", "Commission Based On", "Select", "Grand Total\nNet Total", "sales_partner")
    add("Sales Order", "notes_section", "Notes", "Section Break", None, "sales_team")
    add("Sales Order", "notes", "", "Text", None, "notes_section")
    add("Sales Order", "packing_instructions_section", "Packing Instructions", "Section Break", None, "payment_schedule")
    add("Sales Order", "packing_instructions", "", "Text", None, "packing_instructions_section")

    # ---------------------------------------------------------- Item (customs / duty)
    add("Item", "hs_code", "HS Code", "Data", None, "hs_section_break", "Customs Information")
    add("Item", "customs_tariff_description", "Customs Tariff Description", "Small Text", None, "hs_code", "Customs Information")
    add("Item", "country_of_origin", "Country of Origin", "Link", "Country", "customs_tariff_description", "Customs Information")
    add("Item", "export_control_code", "Export Control Code", "Data", None, "country_of_origin", "Customs Information")
    add("Item", "import_license_required", "Import License Required", "Check", None, "export_control_code", "Customs Information")
    add("Item", "export_license_required", "Export License Required", "Check", None, "import_license_required", "Customs Information")
    # Duty Information
    add("Item", "import_duty_percent", "Import Duty %", "Percent", None, "duty_section_break", None)
    add("Item", "additional_duty_percent", "Additional Duty %", "Percent", None, "import_duty_percent", None)
    add("Item", "regulatory_remarks", "Regulatory Remarks", "Small Text", None, "additional_duty_percent", None)

    # ---------------------------------------------------------- Supplier
    add("Supplier", "supplier_import_code", "Supplier Import Code", "Data", None, "import_info_section", None)
    add("Supplier", "supplier_country", "Supplier Country", "Link", "Country", "supplier_import_code", None)
    add("Supplier", "import_license_no", "Import License No.", "Data", None, "supplier_country", None)
    add("Supplier", "default_port_of_loading", "Default Port of Loading", "Data", None, "import_license_no", None)
    add("Supplier", "default_port_of_discharge", "Default Port of Discharge", "Data", None, "default_port_of_loading", None)
    add("Supplier", "default_incoterm", "Default Incoterm", "Select", "EXW\nFCA\nFAS\nFOB\nCFR\nCIF\nCPT\nCIP\nDAP\nDPU\nDDP", "default_port_of_discharge", None)
    add("Supplier", "import_payment_terms", "Import Payment Terms", "Link", "Payment Terms Template", "default_incoterm", None)
    add("Supplier", "clearing_agent", "Clearing Agent", "Link", "Supplier", "import_payment_terms", None)
    add("Supplier", "bank_lc_reference", "Bank/LC Reference", "Data", None, "clearing_agent", None)
    add("Supplier", "suppliers_item_code", "Supplier's Item Code", "Data", None, "bank_lc_reference", None)
    add("Supplier", "suppliers_item_desc", "Supplier's Item Description", "Small Text", None, "suppliers_item_code", None)

    # ---------------------------------------------------------- Customer (Buyer)
    add("Customer", "buyer_code", "Buyer Code", "Data", None, "export_info_section", None)
    add("Customer", "buyer_country", "Buyer Country", "Link", "Country", "buyer_code", None)
    add("Customer", "export_license_requirement", "Export License Requirement", "Check", None, "buyer_country", None)
    add("Customer", "buyer_registration_no", "Buyer Registration No.", "Data", None, "export_license_requirement", None)
    add("Customer", "default_port_of_loading", "Default Port of Loading", "Data", None, "buyer_registration_no", None)
    add("Customer", "default_port_of_discharge", "Default Port of Discharge", "Data", None, "default_port_of_loading", None)
    add("Customer", "default_incoterm", "Default Incoterm", "Select", "EXW\nFCA\nFAS\nFOB\nCFR\nCIF\nCPT\nCIP\nDAP\nDPU\nDDP", "default_port_of_discharge", None)
    add("Customer", "export_payment_terms", "Export Payment Terms", "Link", "Payment Terms Template", "default_incoterm", None)
    add("Customer", "export_bank", "Export Bank", "Data", None, "export_payment_terms", None)
    add("Customer", "beneficiary_ref", "Beneficiary Reference", "Data", None, "export_bank", None)
    add("Customer", "shipping_doc_requirement", "Shipping Document Requirement", "Small Text", None, "beneficiary_ref", None)

    # ---------------------------------------------------------- Sales Order (Export Section)
    add("Sales Order", "export_order_flag", "Export Order", "Data", None, "export_section_break", None)
    add("Sales Order", "buyer_po_no", "Buyer PO No.", "Data", None, "export_order_flag", None)
    add("Sales Order", "lc_no", "LC No.", "Data", None, "lc_proforma", None)
    add("Sales Order", "lc_date", "LC Date", "Date", None, "lc_no", None)
    add("Sales Order", "lc_issuing_bank", "LC Issuing Bank", "Data", None, "lc_date", None)
    add("Sales Order", "lc_advising_bank", "LC Advising Bank", "Data", None, "lc_issuing_bank", None)
    add("Sales Order", "lc_amount", "LC Amount", "Currency", None, "lc_advising_bank", None)
    add("Sales Order", "lc_currency", "LC Currency", "Link", "Currency", "lc_amount", None)
    add("Sales Order", "lc_expiry_date", "LC Expiry Date", "Date", None, "lc_currency", None)
    add("Sales Order", "latest_shipment_date", "Latest Shipment Date", "Date", None, "lc_expiry_date", None)
    add("Sales Order", "port_of_loading", "Port of Loading", "Data", None, "latest_shipment_date", None)
    add("Sales Order", "port_of_discharge", "Port of Discharge", "Data", None, "port_of_loading", None)
    add("Sales Order", "final_destination", "Final Destination", "Data", None, "port_of_discharge", None)
    add("Sales Order", "incoterm", "Incoterm", "Select", "EXW\nFCA\nFAS\nFOB\nCFR\nCIF\nCPT\nCIP\nDAP\nDPU\nDDP", "final_destination", None)
    add("Sales Order", "shipment_mode", "Shipment Mode", "Select", "\nSea\nAir\nRoad\nRail\nMultimodal", "incoterm", None)
    add("Sales Order", "country_of_destination", "Country of Destination", "Link", "Country", "shipment_mode", None)
    add("Sales Order", "export_status", "Export Status", "Select", "\nPlanned\nIn Production\nReady to Ship\nShipped\nClosed", "country_of_destination", None)

    # ---------------------------------------------------------- Sales Order Item
    add("Sales Order Item", "style_no", "Style No.", "Data", None, "micromax_export_break", None)
    add("Sales Order Item", "buyer_style_no", "Buyer Style No.", "Data", None, "style_no", None)
    add("Sales Order Item", "buyer_color", "Buyer Color", "Data", None, "buyer_style_no", None)
    add("Sales Order Item", "buyer_size", "Buyer Size", "Data", None, "buyer_color", None)
    add("Sales Order Item", "season", "Season", "Data", None, "buyer_size", None)
    add("Sales Order Item", "hs_code", "HS Code", "Data", None, "season", None)
    add("Sales Order Item", "country_of_origin", "Country of Origin", "Link", "Country", "hs_code", None)
    add("Sales Order Item", "export_quantity", "Export Quantity", "Float", None, "country_of_origin", None)
    add("Sales Order Item", "carton_quantity", "Carton Quantity", "Int", None, "export_quantity", None)
    add("Sales Order Item", "net_weight", "Net Weight", "Float", None, "carton_quantity", None)
    add("Sales Order Item", "gross_weight", "Gross Weight", "Float", None, "net_weight", None)

    # ---------------------------------------------------------- CRM Lead (fundraising tracker)
    # These map fields from the donor-prospecting spreadsheet that have no
    # equivalent on the stock CRM Lead doctype (Segment/City/Expected Amount
    # reuse the existing industry/territory/annual_revenue fields instead).
    add("CRM Lead", "fundraising_section", "Fundraising Details", "Section Break", None, "territory", None)
    add("CRM Lead", "priority", "Priority", "Select", "\nA+\nA\nB\nC", "fundraising_section", None)
    add("CRM Lead", "csr_department", "CSR/ESG Department", "Data", None, "priority", None)
    add("CRM Lead", "address", "Address", "Small Text", None, "csr_department", None)
    add("CRM Lead", "fundraising_column_break", None, "Column Break", None, "address", None)
    add("CRM Lead", "focus_area", "Focus Area", "Data", None, "fundraising_column_break", None)
    add("CRM Lead", "education_focus", "Education Focus", "Data", None, "focus_area", None)
    add("CRM Lead", "proposed_ask", "Proposed Ask", "Small Text", None, "education_focus", None)
    add("CRM Lead", "first_contact_date", "First Contact", "Date", None, "proposed_ask", None)
    add("CRM Lead", "remarks", "Remarks", "Small Text", None, "first_contact_date", None)

    # Second contact person on the same lead (e.g. the CSR head plus the finance contact) — shown in its own
    # "Second Contact" tab in the React lead page, and in the desk's Person tab under the main contact.
    add("CRM Lead", "second_contact_section", "Second Contact Person", "Section Break", None, "mobile_no", None)
    add("CRM Lead", "second_contact_name", "Person Name", "Data", None, "second_contact_section", None)
    add("CRM Lead", "second_contact_gender", "Gender", "Link", "Gender", "second_contact_name", None)
    add("CRM Lead", "second_contact_designation", "Designation", "Data", None, "second_contact_gender", None)
    add("CRM Lead", "second_contact_column_break", None, "Column Break", None, "second_contact_designation", None)
    add("CRM Lead", "second_contact_email", "Email", "Data", "Email", "second_contact_column_break", None)
    add("CRM Lead", "second_contact_mobile", "Cell No", "Data", "Phone", "second_contact_email", None)

    return data


def _applicable(data):
    """Keep only the fields this site can hold.

    - doctype not installed on this site (e.g. CRM Lead without the crm app) -> skip it
    - Link to a doctype that doesn't exist here (e.g. a MicroMax-only doctype) -> skip the field
    - a native field of the same name already exists (ERPNext added it later) -> skip, or the doctype
      ends up with a duplicate fieldname
    """
    kept = {}
    for dt, fields in data.items():
        if not frappe.db.exists("DocType", dt):
            continue
        meta = frappe.get_meta(dt)
        for field in fields:
            if field["fieldtype"] == "Link" and not frappe.db.exists("DocType", field["options"]):
                continue
            if meta.has_field(field["fieldname"]) and not frappe.db.exists(
                "Custom Field", f"{dt}-{field['fieldname']}"
            ):
                continue
            kept.setdefault(dt, []).append(field)
    return kept


def _adopt_orphaned_custom_fields(data):
    """Custom Fields carried over from old apps can name a module that no longer exists as a Module Def,
    and updating such a record fails link validation. Hand any we are about to update to MM Core first."""
    for dt, fields in data.items():
        for field in fields:
            name = f"{dt}-{field['fieldname']}"
            module = frappe.db.get_value("Custom Field", name, "module")
            if module and not frappe.db.exists("Module Def", module):
                frappe.db.set_value("Custom Field", name, "module", "MM Core", update_modified=False)


def make_custom_fields():
    if not frappe.db.table_exists("Custom Field"):
        return
    data = _applicable(_build_custom_fields())
    if data:
        _adopt_orphaned_custom_fields(data)
        create_custom_fields(data, ignore_validate=True)
