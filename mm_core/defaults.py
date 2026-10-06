"""Fill fields this bench makes mandatory that standard ERPNext / HRMS code doesn't set.

Journal Entry has a mandatory header Cost Center (mm_core.custom_fields); HRMS's payroll accrual / salary payment
journals and other system-made entries only set it on their lines, so the insert failed with "cost_center".
"""

import frappe


def fill_cost_center(doc, method=None):
    if not doc.meta.has_field("cost_center") or doc.get("cost_center"):
        return
    for table in ("accounts", "references", "deductions"):
        for row in doc.get(table) or []:
            if row.get("cost_center"):
                doc.cost_center = row.cost_center
                return
    if doc.get("company"):
        doc.cost_center = frappe.get_cached_value("Company", doc.company, "cost_center")
