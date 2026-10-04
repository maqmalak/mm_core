import frappe
from frappe.utils import escape_html

from mm_core.crm_reminders import _notify

# Outgoing send/error notifications live in crm_reminders.py (polled off the
# same */5 cron as task/event reminders — Email Queue's own status update
# doesn't fire a doc_event hook, see that module's _notify_mail_send_status
# docstring). This module only covers the other half: a new *inbound* email
# on a CRM Lead/Deal, which genuinely is a normal `doc.insert()` and so gets
# a real after_insert hook. Neither this app nor the vendored crm app pings
# anyone for this today — crm.utils.on_communication_insert only
# auto-creates a Lead from an unrecognized sender.


def on_communication_after_insert(doc, method=None):
	if doc.communication_medium != "Email" or doc.sent_or_received != "Received":
		return
	if doc.reference_doctype not in ("CRM Lead", "CRM Deal"):
		return

	owner_field = "lead_owner" if doc.reference_doctype == "CRM Lead" else "deal_owner"
	to_user = frappe.db.get_value(doc.reference_doctype, doc.reference_name, owner_field)
	if not to_user:
		return

	subject = escape_html(doc.subject or "(no subject)")
	sender = escape_html(doc.sender or "someone")
	_notify(
		to_user,
		f"New email from {doc.sender or 'someone'}: {doc.subject or '(no subject)'}",
		f"<p>New email from <b>{sender}</b>: “{subject}”.</p>",
		doc.reference_doctype,
		doc.reference_name,
		notification_type="Email",
	)
