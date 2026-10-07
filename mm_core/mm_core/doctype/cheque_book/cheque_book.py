"""Cheque Book: a book received from the bank and every leaf in it.

Leaf Status: Unused (blank) → Issued (handed to the payee) → Cleared (paid by the bank); or Void (spoiled /
cancelled by us), Stopped (stop-payment instruction), Dishonoured (returned unpaid by the bank).
Cheque Book Status: Unused → In Use → Exhausted (no blank leaf left); Cancelled when every leaf is void / stopped /
dishonoured (e.g. a lost book).

Leaves are generated from From Serial × No of Leaves when the table is empty. A bank Payment Entry whose
Cheque/Reference No matches an in-hand leaf of a book on the same bank account marks that leaf Issued (party, amount,
payment entry) — see mm_core.cheques.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt


VOID_STATES = ("Void", "Stopped", "Dishonoured")
OPEN_BOOK_STATES = ("Unused", "In Use")


def _serial(start, i):
	"""start "000123" + 2 → "000125" (keeps the width / leading zeros)."""
	return str(int(start) + i).zfill(len(start)) if str(start).isdigit() else f"{start}-{i + 1}"


class ChequeBook(Document):
	def validate(self):
		n = cint(self.no_of_leaves)
		if n <= 0:
			frappe.throw(_("No of Leaves must be more than 0."))
		self.from_serial = (self.from_serial or "").strip()
		if not self.leaves:
			for i in range(n):
				self.append("leaves", {"sno": i + 1, "cheque_no": _serial(self.from_serial, i), "status": "Unused"})
		self.to_serial = _serial(self.from_serial, n - 1)
		self._validate_leaves()
		self._check_overlap()
		self._summarise()

	def _validate_leaves(self):
		seen = set()
		for i, row in enumerate(self.leaves, 1):
			row.sno = row.sno or i
			if row.cheque_no in seen:
				frappe.throw(_("Row {0}: cheque no {1} appears twice.").format(i, row.cheque_no))
			seen.add(row.cheque_no)
			if row.status in VOID_STATES and not (row.cancel_reason or "").strip():
				frappe.throw(_("Row {0}: enter the reason cheque {1} was {2}.").format(i, row.cheque_no, row.status.lower()))
			if row.status in ("Issued", "Cleared") and not row.issue_date:
				row.issue_date = frappe.utils.nowdate()
			if row.party and not row.party_name and row.party_type:
				field = {"Supplier": "supplier_name", "Customer": "customer_name", "Employee": "employee_name"}.get(row.party_type)
				row.party_name = frappe.db.get_value(row.party_type, row.party, field) if field else row.party

	def _check_overlap(self):
		"""The same cheque number can't sit in two books of one bank account."""
		nos = [r.cheque_no for r in self.leaves]
		if not nos:
			return
		clash = frappe.db.sql("""select l.cheque_no, b.name from `tabCheque Book Leaf` l join `tabCheque Book` b on b.name = l.parent
			where b.bank_account = %s and b.name != %s and l.cheque_no in %s limit 1""", (self.bank_account, self.name or "", nos))
		if clash:
			frappe.throw(_("Cheque no {0} is already in cheque book {1} of this bank account.").format(clash[0][0], clash[0][1]))

	def _summarise(self):
		count = lambda *s: sum(1 for r in self.leaves if r.status in s)  # noqa: E731
		self.leaves_in_hand = count("Unused")
		self.leaves_issued = count("Issued", "Cleared")
		self.leaves_cancelled = count(*VOID_STATES)
		self.issued_amount = flt(sum(flt(r.amount) for r in self.leaves if r.status in ("Issued", "Cleared")))
		if self.leaves and self.leaves_cancelled == len(self.leaves):
			self.status = "Cancelled"
		elif self.leaves_in_hand == len(self.leaves):
			self.status = "Unused"
		elif self.leaves_in_hand:
			self.status = "In Use"
		else:
			self.status = "Exhausted"
