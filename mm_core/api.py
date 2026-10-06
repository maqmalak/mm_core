import frappe
from frappe.sessions import get_csrf_token


@frappe.whitelist()
def get_csrf_token_for_session() -> str:
    """Expose the current session's CSRF token to the decoupled React frontend.

    Frappe injects ``window.csrf_token`` only into pages it renders itself, and
    ``frappe.sessions.get_csrf_token`` is not whitelisted. The React app is served by
    nginx (production) or Vite (dev), so it fetches the token here — in mm_core so every
    site on the bench has it, not only the ones with micromax.
    """
    return get_csrf_token()


@frappe.whitelist()
def get_linked_parent_docs(
    doctype: str,
    parenttype: str,
    purchase_order: str | None = None,
    link_field: str | None = None,
    link_value: str | None = None,
    extra_filters: str | dict | None = None,
):
    """Return distinct parent document names of a given parenttype whose child
    rows reference the given value on a link field.

    The link lives on the child table (e.g. Purchase Receipt Item.purchase_order,
    Purchase Invoice Item.purchase_receipt, or Payment Entry Reference.reference_name),
    but the REST ``get_list`` API strips the ``parent`` field for child tables.
    This server-side helper does the lookup and returns deduplicated parent names.

    :param doctype: child DocType to search, e.g. "Purchase Receipt Item"
    :param parenttype: parent DocType, e.g. "Purchase Receipt"
    :param purchase_order: legacy kwarg, kept for backwards compatibility with
        older frontend builds — equivalent to ``link_field="purchase_order"``.
    :param link_field: child-row fieldname to filter on, e.g. "purchase_order"
    :param link_value: the value to match, e.g. a Purchase Order name
    :param extra_filters: optional dict (or JSON string) of additional exact-match
        filters, e.g. {"reference_doctype": "Purchase Invoice"} when the same
        link_field/value pair could plausibly match rows belonging to more than
        one parent kind (Payment Entry Reference is shared by many doctypes).
    """
    field = link_field or "purchase_order"
    value = link_value if link_value is not None else purchase_order
    if not value:
        return []

    filters = {field: value, "parenttype": parenttype}
    if extra_filters:
        if isinstance(extra_filters, str):
            extra_filters = frappe.parse_json(extra_filters)
        filters.update(extra_filters)

    parent_names = frappe.get_all(
        doctype,
        filters=filters,
        fields=["parent"],
        limit_page_length=200,
    )
    return list(dict.fromkeys(r.parent for r in parent_names if r.parent))


@frappe.whitelist()
def get_available_reports(names: str | list) -> list[str]:
    """Of the given Report names, those that exist on this site, aren't disabled and the current user may run
    (Report.is_permitted: the report's roles, or read access to its reference doctype). The React reports
    hub shows a card only for these, so a site without HRMS / a user without Accounts sees no dead cards."""
    if isinstance(names, str):
        names = frappe.parse_json(names)
    out = []
    for name in names or []:
        if not frappe.db.exists("Report", {"name": name, "disabled": 0}):
            continue
        try:
            if frappe.get_cached_doc("Report", name).is_permitted():
                out.append(name)
        except Exception:
            continue
    return out


@frappe.whitelist()
def get_print_options(doctype: str) -> dict:
    """Print formats and letter heads for the React "Print" dialog — the same list the desk's print view offers.

    Print Format isn't readable by ordinary desk users, so the desk reads it from the doctype's meta; this does
    the same behind a print-permission check.
    """
    if not frappe.has_permission(doctype, "print"):
        frappe.throw(frappe._("Not permitted to print {0}").format(doctype), frappe.PermissionError)
    meta = frappe.get_meta(doctype)
    formats = frappe.get_all(
        "Print Format",
        filters={"doc_type": doctype, "disabled": 0},
        fields=["name", "standard", "print_format_type"],
        order_by="standard asc, name asc",  # custom (designed) formats first
    )
    names = [f.name for f in formats]
    if "Standard" not in names:
        names.append("Standard")  # Frappe's built-in layout
    letter_heads = frappe.get_all("Letter Head", filters={"disabled": 0}, fields=["name", "is_default"], order_by="is_default desc, name asc")
    return {
        "formats": names,
        "custom": [f.name for f in formats if f.standard == "No"],
        "default_format": meta.default_print_format or (names[0] if names else "Standard"),
        "letter_heads": [l.name for l in letter_heads],
        "default_letter_head": next((l.name for l in letter_heads if l.is_default), None),
    }


@frappe.whitelist()
def awesome_search(txt: str, limit: int = 8) -> dict:
    """DocTypes and Reports matching `txt` that the current user may open — for the React search bar (Ctrl+K),
    like the desk's awesome bar."""
    txt = (txt or "").strip()
    if len(txt) < 2:
        return {"doctypes": [], "reports": []}
    like, limit = f"%{txt}%", min(int(limit or 8), 25)
    doctypes = []
    for d in frappe.get_all("DocType", filters={"name": ["like", like], "istable": 0},
                            fields=["name", "module", "issingle"], order_by="name asc", limit=60):
        if frappe.has_permission(d.name, "read"):
            doctypes.append({"name": d.name, "module": d.module, "single": d.issingle})
        if len(doctypes) >= limit:
            break
    reports = []
    for r in frappe.get_all("Report", filters={"name": ["like", like], "disabled": 0},
                            fields=["name", "report_type", "ref_doctype", "module"], order_by="name asc", limit=60):
        try:
            if frappe.get_cached_doc("Report", r.name).is_permitted():
                reports.append({"name": r.name, "type": r.report_type, "ref_doctype": r.ref_doctype, "module": r.module})
        except Exception:
            continue
        if len(reports) >= limit:
            break
    # exact / prefix matches first
    key = lambda x: (not x["name"].lower().startswith(txt.lower()), x["name"])  # noqa: E731
    return {"doctypes": sorted(doctypes, key=key), "reports": sorted(reports, key=key)}
