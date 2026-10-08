"""Shift roster for the React Roster page: who works which shift on each day of a range, with holidays and the
attendance outcome on past days."""

import frappe
from frappe import _
from frappe.utils import add_days, date_diff, getdate


def _hhmm(t):
    h, m, *_ = (str(t or "0:0").split(":") + ["0"])
    return f"{int(h) % 24:02d}:{int(m):02d}"


@frappe.whitelist()
def get_roster(company: str, start: str, end: str, department: str | None = None, shift_type: str | None = None) -> dict:
    if not frappe.has_permission("Shift Assignment", "read"):
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    start, end = getdate(start), getdate(end)
    if date_diff(end, start) > 62:
        frappe.throw(_("Choose up to two months"))
    ef = {"company": company, "status": "Active"}
    if department:
        ef["department"] = department
    emps = frappe.get_all("Employee", ef, ["name", "employee_name", "department", "designation", "default_shift", "date_of_joining"],
                          order_by="department, employee_name")
    names = [e.name for e in emps] or [""]
    sa_filters = {"employee": ["in", names], "docstatus": 1, "status": "Active", "start_date": ["<=", end]}
    assignments = frappe.get_all("Shift Assignment", sa_filters,
                                 ["name", "employee", "shift_type", "start_date", "end_date", "shift_location"], order_by="start_date")
    days = {}                      # employee -> {date: {shift, assignment, location}}
    for a in assignments:
        a_end = getdate(a.end_date) if a.end_date else end
        if a_end < start:
            continue
        d = max(getdate(a.start_date), start)
        while d <= min(a_end, end):
            days.setdefault(a.employee, {})[str(d)] = {"shift": a.shift_type, "assignment": a.name, "location": a.shift_location}
            d = add_days(d, 1)
    if shift_type:
        keep = {e for e, ds in days.items() if any(v["shift"] == shift_type for v in ds.values())}
        emps = [e for e in emps if e.name in keep or e.default_shift == shift_type]
    attendance = {}
    for r in frappe.get_all("Attendance", {"employee": ["in", names], "docstatus": 1, "attendance_date": ["between", [start, end]]},
                            ["employee", "attendance_date", "status", "late_entry", "early_exit", "leave_type"]):
        attendance.setdefault(r.employee, {})[str(r.attendance_date)] = {
            "status": r.status, "late": r.late_entry, "early": r.early_exit, "leave_type": r.leave_type}
    hol_list = frappe.db.get_value("Company", company, "default_holiday_list")
    holidays = {str(h.holiday_date): {"description": frappe.utils.strip_html(h.description or ""), "weekly_off": h.weekly_off}
                for h in frappe.get_all("Holiday", {"parent": hol_list, "holiday_date": ["between", [start, end]]},
                                        ["holiday_date", "description", "weekly_off"])} if hol_list else {}
    shifts = frappe.get_all("Shift Type", {"name": ["in", list({v["shift"] for ds in days.values() for v in ds.values()} | {e.default_shift for e in emps if e.default_shift}) or [""]]},
                            ["name", "start_time", "end_time"])
    return {
        "employees": emps, "days": days, "attendance": attendance, "holidays": holidays,
        "shifts": [{"name": s.name, "start": _hhmm(s.start_time), "end": _hhmm(s.end_time)} for s in shifts],
        "departments": sorted({e.department for e in frappe.get_all("Employee", {"company": company, "status": "Active"}, ["department"]) if e.department}),
    }
