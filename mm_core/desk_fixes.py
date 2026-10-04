"""Small desk corrections for third-party apps (icons, number cards), re-applied after every migrate.

Frappe creates one "App" Desktop Icon per app from its `add_to_apps_screen` hook, once, and never
recreates it while it exists — so correcting that record sticks.
"""

import frappe

# App icons whose route points at a workspace the app doesn't ship -> the workspace name that would
# make the route valid. nl_school (Junior-School, version-16) declares app_home "/desk/scholarship"
# under the title "Scholarship Management", but only ships the "Junior School" workspace, which has
# its own working icon; the generated one opens "Page scholarship not found".
BROKEN_APP_ROUTES = {"/desk/scholarship": "Scholarship"}


def hide_broken_app_icons():
    if not frappe.db.table_exists("Desktop Icon"):
        return
    for route, workspace in BROKEN_APP_ROUTES.items():
        if frappe.db.exists("Workspace", workspace):
            continue  # the app shipped it after all: the icon works, leave it
        for name in frappe.get_all("Desktop Icon", filters={"icon_type": "App", "link": route, "hidden": 0}, pluck="name"):
            frappe.db.set_value("Desktop Icon", name, "hidden", 1)
            print(f"mm_core: hid Desktop Icon '{name}' ({route} has no workspace)")
    frappe.clear_cache()


# Number cards that filter on a field their doctype doesn't have -> (field that must be missing, new filters,
# new dynamic filters). nl_school's "Open/Closed Assessment Plans" cards filter Assessment Plan.status, which
# education v16 doesn't have (Assessment Plan is submittable; it has schedule_date), so the Junior School
# workspace shows "You do not have permission to access field: Assessment Plan.status". Open = scheduled
# today or later, Closed = scheduled before today; cancelled plans are left out of both.
NUMBER_CARD_FIXES = {
    "Open Assessment Plans": (
        ("Assessment Plan", "status"),
        [["Assessment Plan", "docstatus", "!=", 2, False]],
        [["Assessment Plan", "schedule_date", ">=", "frappe.datetime.get_today()"]],
    ),
    "Closed Assessment Plans": (
        ("Assessment Plan", "status"),
        [["Assessment Plan", "docstatus", "!=", 2, False]],
        [["Assessment Plan", "schedule_date", "<", "frappe.datetime.get_today()"]],
    ),
}


def fix_number_cards():
    import json

    for card, ((doctype, field), filters, dynamic) in NUMBER_CARD_FIXES.items():
        if not frappe.db.exists("Number Card", card) or not frappe.db.exists("DocType", doctype):
            continue
        if frappe.get_meta(doctype).has_field(field):
            continue  # the field exists now: the card works as shipped
        frappe.db.set_value("Number Card", card, {"filters_json": json.dumps(filters),
                                                  "dynamic_filters_json": json.dumps(dynamic)}, update_modified=False)
        print(f"mm_core: fixed Number Card '{card}' ({doctype}.{field} doesn't exist)")


def apply_all():
    hide_broken_app_icons()
    fix_number_cards()
    frappe.clear_cache()
