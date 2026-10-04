import frappe
from frappe.model.document import Document


def _existing_link(doctype: str, value: str | None) -> str | None:
	"""Only point a Link field at a value that's actually a valid master
	record — scraped Segment/City text rarely matches a CRM Industry /
	CRM Territory record verbatim, and setting a Link to a non-existent
	value throws on save."""
	if value and frappe.db.exists(doctype, value):
		return value
	return None


# The CRM Lead Source every lead converted from this queue is tagged with.
SCRAPER_LEAD_SOURCE = "Prospect Scraper"


def _get_or_create(doctype: str, name_field: str, value: str | None, extra: dict | None = None) -> str | None:
	"""The master record called `value`, created first if it doesn't exist yet; None if `value` is blank or
	the record can't be created.

	An existing record is returned untouched (never overwritten). `extra` fields are only used when creating.
	Master data is created with permissions ignored: converting a prospect is an explicit, permission-checked
	action already, and a user who may convert must not be blocked because their role can't create a Territory.
	A failure here is logged and skipped rather than raised, so a bad master record can't lose the conversion —
	the lead is still created, just without that link.
	"""
	value = (value or "").strip()[:140]
	if not value:
		return None
	existing = frappe.db.exists(doctype, value)  # the stored name, so its capitalisation wins
	if existing:
		return existing
	try:
		doc = frappe.get_doc({"doctype": doctype, name_field: value, **{k: v for k, v in (extra or {}).items() if v}})
		doc.insert(ignore_permissions=True)
		return doc.name
	except Exception:
		frappe.log_error(title=f"Prospect Scraper: could not create {doctype} '{value}'")
		return None


class CRMProspectScrape(Document):
	@frappe.whitelist()
	def duplicate(self):
		"""A fresh review-queue row for the SAME company, for adding another contact person there.

		Converting a row is one-shot (it remembers its lead), so a company with two leads needs two rows.
		The copy keeps everything about the company (name, website, address, city, segment, CSR info, shared
		email/phone …) but starts clean as a new candidate: status back to Pending Review, no linked lead, no
		scrape error, and the contact-person fields (focal person, designation) emptied for the next person.
		Converting it later reuses the company's Organization / Territory / Lead Source — they already exist
		from the first lead — so only the new Lead is created."""
		new = frappe.copy_doc(self)
		new.status = "Pending Review"
		new.converted_lead = None
		new.scrape_error = None
		new.focal_person = None
		new.designation = None
		new.last_research_date = frappe.utils.today()
		new.insert()
		return new.name

	@frappe.whitelist()
	def convert_to_lead(self):
		"""Turn this reviewed staging row into a real CRM Lead. Only ever
		called by hand from the review queue — approving a row here never
		auto-creates a Lead, so nothing is duplicated behind the scenes."""
		if self.converted_lead and frappe.db.exists("CRM Lead", self.converted_lead):
			return self.converted_lead

		# Master data the lead points at — each is created only if it doesn't already exist.
		territory = _get_or_create("CRM Territory", "territory_name", self.city)
		industry = _existing_link("CRM Industry", self.segment)
		source = _get_or_create(
			"CRM Lead Source", "source_name", SCRAPER_LEAD_SOURCE, {"details": "Leads created from the Prospect Scraper review queue."}
		)
		org_extra = {"website": self.website, "industry": industry, "territory": territory}
		# CRM Organization.address is a Link to "Address" until micromax.install.make_crm_organization_address_freetext
		# has run; scraped text isn't an Address record, so only pass it once the field takes free text.
		if frappe.get_meta("CRM Organization").get_field("address").fieldtype != "Link":
			org_extra["address"] = self.address
		organization = _get_or_create("CRM Organization", "organization_name", self.donor_name, org_extra)

		first_name, _, last_name = (self.focal_person or "").partition(" ")
		lead = frappe.new_doc("CRM Lead")
		lead.update(
			{
				"first_name": first_name or self.donor_name or "Unknown",
				"last_name": last_name or None,
				"organization": organization or self.donor_name,
				"website": self.website,
				"industry": industry,
				"territory": territory,
				"source": source,
				"address": self.address,
				"csr_department": self.csr_department,
				"focus_area": self.focus_area,
				"proposed_ask": self.proposed_ask,
				"job_title": self.designation,
				"email": self.email,
				"phone": self.phone,
				"first_contact_date": self.last_research_date,
				"remarks": f"Sourced from {self.research_source or self.source_url}",
				"status": "New",
			}
		)
		lead.insert()

		self.converted_lead = lead.name
		self.status = "Converted"
		self.save()
		return lead.name
