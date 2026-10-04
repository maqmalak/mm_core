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
