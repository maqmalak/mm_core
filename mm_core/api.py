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
