"""Approvals inbox for the React app: what is waiting for the current user, and recent workflow changes.

Pending items come from
- every active Workflow: documents in a state with a transition the user's roles allow (so an HR Manager sees
  pending leave even where no Workflow Action row was created for them), plus the user's open Workflow Actions;
- HR requests without a workflow: Expense Claims (Draft) and Shift Requests (Draft) where the user is the
  approver or holds an HR role, and Leave Applications (Open) when leave has no active workflow.

Recent changes are the "Workflow" comments Frappe writes on every transition (and that act() writes for the
HR requests decided without a workflow).
"""

import frappe
from frappe import _
from frappe.utils import add_days, flt, nowdate

HR_ROLES = {"HR Manager", "HR User"}
HR_DOCTYPES = ("Leave Application", "Expense Claim", "Shift Request", "Attendance Request", "Compensatory Leave Request", "Employee Advance")
PER_DOCTYPE = 100


def _exists(dt):
    return bool(frappe.db.exists("DocType", dt))


def _title(dt, d):
    if dt == "Leave Application":
        days = f"{flt(d.get('total_leave_days')):g} day{'' if flt(d.get('total_leave_days')) == 1 else 's'}"
        return f"{d.get('employee_name')} · {d.get('leave_type')} · {days} from {d.get('from_date')}"
    if dt == "Expense Claim":
        return f"{d.get('employee_name')} · Expense claim"
    if dt == "Employee Advance":
        months = d.get("mm_installment_months")
        return f"{d.get('employee_name')} · salary loan{f' · {months} instalments' if months else ''}"
    if dt == "Shift Request":
        return f"{d.get('employee_name')} · {d.get('shift_type')} from {d.get('from_date')}"
    for f in ("title", "employee_name", "customer_name", "supplier_name", "party_name"):
        if d.get(f):
            return str(d.get(f))
    return ""


def _amount(dt, d):
    for f in ("total_claimed_amount", "advance_amount", "base_grand_total", "grand_total", "total_amount", "paid_amount"):
        if d.get(f):
            return flt(d.get(f))
    return None


def _fields(dt, wanted):
    meta = frappe.get_meta(dt)
    return ["name", "modified", "owner"] + [f for f in wanted if meta.has_field(f)]


COMMON = ["title", "employee_name", "customer_name", "supplier_name", "party_name", "base_grand_total", "grand_total",
          "total_amount", "total_claimed_amount", "leave_type", "total_leave_days", "from_date", "to_date", "shift_type",
          "company", "status", "approval_status", "advance_amount", "mm_installment_months"]


def _workflow_pending(user, roles, company, out, seen):
    for wf in frappe.get_all("Workflow", {"is_active": 1}, ["name", "document_type", "workflow_state_field"]):
        dt = wf.document_type
        if not _exists(dt) or not frappe.has_permission(dt, "read"):
            continue
        transitions = frappe.get_all("Workflow Transition", {"parent": wf.name}, ["state", "action", "allowed"])
        # Waiting for approval = states before submission (docstatus 0); submitted states' actions (Close, Reopen…)
        # are follow-ups, not approvals.
        open_states = set(frappe.get_all("Workflow Document State", {"parent": wf.name, "doc_status": "0"}, pluck="state"))
        actionable = {}
        for t in transitions:
            if t.state not in open_states:
                continue
            if t.allowed in roles or user == "Administrator":
                actionable.setdefault(t.state, []).append(t.action)
        if not actionable:
            continue
        field = wf.workflow_state_field or "workflow_state"
        filters = {field: ["in", list(actionable)], "docstatus": ["<", 2]}
        if company and frappe.get_meta(dt).has_field("company"):
            filters["company"] = company
        for d in frappe.get_all(dt, filters=filters, fields=_fields(dt, COMMON + [field]), order_by="modified desc",
                                limit=PER_DOCTYPE):
            key = (dt, d.name)
            if key in seen:
                continue
            seen.add(key)
            state = d.get(field)
            out.append({"doctype": dt, "name": d.name, "state": state, "title": _title(dt, d), "amount": _amount(dt, d),
                        "since": str(d.modified), "owner": d.owner, "actions": actionable.get(state, []),
                        "category": "hr" if dt in HR_DOCTYPES else "workflow", "workflow": wf.name})


def _hr_pending(user, roles, company, out, seen):
    hr = bool(HR_ROLES & set(roles)) or user == "Administrator"
    active = set(frappe.get_all("Workflow", {"is_active": 1}, pluck="document_type"))

    def add(dt, filters, approver_field, state_of):
        if not _exists(dt) or dt in active or not frappe.has_permission(dt, "read"):
            return
        if company and frappe.get_meta(dt).has_field("company"):
            filters["company"] = company
        if not hr:
            filters[approver_field] = user
        for d in frappe.get_all(dt, filters=filters, fields=_fields(dt, COMMON + [approver_field]),
                                order_by="modified desc", limit=PER_DOCTYPE):
            if (dt, d.name) in seen:
                continue
            seen.add((dt, d.name))
            out.append({"doctype": dt, "name": d.name, "state": state_of(d), "title": _title(dt, d),
                        "amount": _amount(dt, d), "since": str(d.modified), "owner": d.owner,
                        "actions": ["Approve", "Reject"], "category": "hr", "workflow": None})

    add("Leave Application", {"status": "Open", "docstatus": 0}, "leave_approver", lambda d: "Open")
    add("Expense Claim", {"approval_status": "Draft", "docstatus": 0}, "expense_approver", lambda d: "Pending approval")
    add("Shift Request", {"status": "Draft", "docstatus": 0}, "approver", lambda d: "Pending approval")
    # Salary loan requests (mm_core.loans): drafts waiting for HR. Approving submits them (the policy check runs).
    # Only HR decides loans (the requester must not see an Approve button on their own request).
    if hr and _exists("Employee Advance") and frappe.get_meta("Employee Advance").has_field("mm_is_loan"):
        add("Employee Advance", {"mm_is_loan": 1, "docstatus": 0}, "owner", lambda d: "Loan request")


@frappe.whitelist()
def get_inbox(company: str | None = None, days: int = 30) -> dict:
    user, roles = frappe.session.user, set(frappe.get_roles())
    pending, seen = [], set()
    if _exists("Workflow"):
        _workflow_pending(user, roles, company, pending, seen)
    _hr_pending(user, roles, company, pending, seen)
    pending.sort(key=lambda x: x["since"], reverse=True)

    recent = []
    for c in frappe.get_all("Comment", {"comment_type": "Workflow", "creation": [">=", add_days(nowdate(), -int(days))]},
                            ["reference_doctype", "reference_name", "content", "owner", "creation"],
                            order_by="creation desc", limit=200):
        if not c.reference_doctype or not frappe.has_permission(c.reference_doctype, "read"):
            continue
        recent.append({"doctype": c.reference_doctype, "name": c.reference_name, "state": frappe.utils.strip_html(c.content or ""),
                       "by": frappe.utils.get_fullname(c.owner), "at": str(c.creation),
                       "category": "hr" if c.reference_doctype in HR_DOCTYPES else "workflow"})
        if len(recent) >= 80:
            break
    return {"pending": pending, "recent": recent,
            "counts": {"pending": len(pending), "hr": sum(1 for p in pending if p["category"] == "hr")}}


@frappe.whitelist(methods=["POST"])
def act(doctype: str, name: str, action: str) -> dict:
    """Approve / Reject (or any workflow action) on one document."""
    doc = frappe.get_doc(doctype, name)
    if frappe.db.exists("Workflow", {"document_type": doctype, "is_active": 1}):
        from frappe.model.workflow import apply_workflow, get_transitions

        if action not in [t.action for t in get_transitions(doc)]:
            frappe.throw(_("Action {0} is not available to you for {1} {2}").format(action, doctype, name))
        doc = apply_workflow(doc, action)
        if doc.docstatus == 1:
            _close_assignments(doctype, name)
        return {"name": doc.name, "state": doc.get("workflow_state"), "docstatus": doc.docstatus}

    if action not in ("Approve", "Reject"):
        frappe.throw(_("Unknown action {0}").format(action))
    approve = action == "Approve"
    doc.check_permission("submit")
    if doctype == "Leave Application":
        doc.status = "Approved" if approve else "Rejected"
    elif doctype == "Expense Claim":
        doc.approval_status = "Approved" if approve else "Rejected"
    elif doctype == "Shift Request":
        doc.status = "Approved" if approve else "Rejected"
    elif doctype == "Employee Advance":
        if not approve:
            # A rejected loan request is closed without ever being submitted (no ledger impact), kept for history.
            doc.check_permission("write")
            frappe.db.set_value("Employee Advance", name, {"docstatus": 2, "status": "Cancelled"})
            doc.add_comment("Workflow", "Rejected")
            _close_assignments(doctype, name)
            return {"name": name, "state": "Rejected", "docstatus": 2}
    else:
        frappe.throw(_("{0} can't be approved here").format(doctype))
    doc.submit()
    doc.add_comment("Workflow", "Approved" if approve else "Rejected")
    _close_assignments(doctype, name)
    return {"name": doc.name, "state": "Approved" if approve else "Rejected", "docstatus": doc.docstatus}


def _close_assignments(doctype, name):
    """The decision is made: the approver's to-do for it is done."""
    try:
        from frappe.desk.form.assign_to import close_all_assignments

        close_all_assignments(doctype, name)
    except Exception:
        frappe.db.set_value("ToDo", {"reference_type": doctype, "reference_name": name, "status": "Open"}, "status", "Closed")
