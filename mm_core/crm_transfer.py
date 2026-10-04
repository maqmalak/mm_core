"""Move CRM data from one site to another (e.g. demo -> wise), keeping names, dates, owners and links.

Two steps, each run against its own site, with a folder in between:

    bench --site demo execute mm_core.crm_transfer.export_crm --kwargs "{'path': '/home/me/crm-export'}"
    bench --site wise execute mm_core.crm_transfer.import_crm --kwargs "{'path': '/home/me/crm-export'}"

Export only reads. It writes `crm.json` (every record, children included) and `files/` (attachments).

Import inserts the records as they are (`db_insert`): original names, creation/modified, owners; no
validation, hooks, notifications or emails fire. It
  - refuses to run if any CRM Organization / Lead / Deal / Task / Call Log name already exists on the
    target (their timelines would merge into unrelated records); `dry_run=1` reports without writing;
  - refuses to run if CRM records hold values in fields the target lacks (they'd be lost), unless
    `allow_missing_fields=1`;
  - skips lookup records (statuses, sources, ...) and contacts that already exist by name;
  - maps users the target doesn't have to `fallback_user` (default: the target's default outgoing
    mail account's address if that is a user, else Administrator) and lists them;
  - points imported emails at the target's default incoming (else outgoing) Email Account;
  - advances numbering (CRM-LEAD-/CRM-DEAL- series, EV. series, CRM Task counter) past what it imported.
The source site is not changed — disable or archive there separately.
"""

import json
import os
import re
import shutil

import frappe
from frappe.utils import get_files_path

# The records being moved, in import order.
PRIMARY = ["CRM Organization", "CRM Lead", "CRM Deal"]
# Lookup lists the primary records link to; small, copied whole, existing names skipped.
MASTERS = [
    "CRM Lead Status", "CRM Deal Status", "CRM Lead Source", "CRM Industry", "CRM Territory",
    "CRM Product", "CRM Lost Reason", "CRM Communication Status",
]
# Names that must not already exist on the target (would collide with what we bring).
NO_CONFLICT = PRIMARY + ["CRM Task", "CRM Call Log"]
FORMAT = 1


# ----------------------------------------------------------------------------- export
def _docs(doctype, filters=None, names=None):
    if not frappe.db.exists("DocType", doctype):
        return []
    if names is not None:
        if not names:
            return []
        filters = {"name": ["in", list(names)]}
    out = []
    for name in frappe.get_all(doctype, filters=filters or {}, pluck="name", order_by="creation asc"):
        out.append(frappe.get_doc(doctype, name).as_dict(convert_dates_to_str=True, no_nulls=True))
    return out


def export_crm(path: str) -> dict:
    """Read every CRM record and its activity from this site into `path` (created if missing)."""
    os.makedirs(os.path.join(path, "files"), exist_ok=True)
    data = {"format": FORMAT, "source_site": frappe.local.site, "doctypes": {}}
    put = lambda dt, docs: data["doctypes"].setdefault(dt, []).extend(docs)  # noqa: E731

    for dt in MASTERS:
        put(dt, _docs(dt))

    primary = {dt: _docs(dt) for dt in PRIMARY}
    for dt in PRIMARY:
        put(dt, primary[dt])
    refs = {(dt, d["name"]) for dt in PRIMARY for d in primary[dt]}
    by_type = {dt: [n for t, n in refs if t == dt] for dt in PRIMARY}

    def linked(doctype, type_field, name_field):
        names = set()
        for dt, ns in by_type.items():
            if ns and frappe.db.exists("DocType", doctype):
                names.update(frappe.get_all(doctype, filters={type_field: dt, name_field: ["in", ns]}, pluck="name"))
        return names

    # The CRM's own task / note / call tables move whole — including entries not tied to a lead or deal.
    tasks, notes, calls = _docs("CRM Task"), _docs("FCRM Note"), _docs("CRM Call Log")
    put("CRM Task", tasks)
    put("FCRM Note", notes)
    put("CRM Call Log", calls)
    notes = {d["name"] for d in notes}

    # Contacts / addresses: linked through Dynamic Link, or as a Deal's contacts.
    contacts, addresses = set(), set()
    for dt, ns in by_type.items():
        if not ns:
            continue
        for parenttype, bucket in (("Contact", contacts), ("Address", addresses)):
            bucket.update(frappe.get_all("Dynamic Link", filters={"parenttype": parenttype, "link_doctype": dt,
                                                                  "link_name": ["in", ns]}, pluck="parent"))
    for deal in primary["CRM Deal"]:
        contacts.update(c["contact"] for c in deal.get("contacts", []) if c.get("contact"))
        if deal.get("contact"):
            contacts.add(deal["contact"])
    put("Contact", _docs("Contact", names=contacts))
    put("Address", _docs("Address", names=addresses))

    # Emails: referenced directly or through a timeline link.
    comms = linked("Communication", "reference_doctype", "reference_name")
    for dt, ns in by_type.items():
        if ns:
            comms.update(frappe.get_all("Communication Link", filters={"link_doctype": dt, "link_name": ["in", ns]},
                                        pluck="parent"))
    put("Communication", _docs("Communication", names=comms))

    # Comments, history and assignments on the tasks / notes / call logs too.
    by_type["CRM Task"] = [str(d["name"]) for d in tasks]
    by_type["FCRM Note"] = list(notes)
    by_type["CRM Call Log"] = [d["name"] for d in calls]
    put("Comment", _docs("Comment", names=linked("Comment", "reference_doctype", "reference_name")))
    put("Version", _docs("Version", names=linked("Version", "ref_doctype", "docname")))
    put("ToDo", _docs("ToDo", names=linked("ToDo", "reference_type", "reference_name")))
    put("WhatsApp Message", _docs("WhatsApp Message", names=linked("WhatsApp Message", "reference_doctype", "reference_docname")))

    events = linked("Event", "reference_doctype", "reference_docname")
    for dt, ns in by_type.items():
        if ns:
            events.update(frappe.get_all("Event Participants", filters={"reference_doctype": dt,
                                                                       "reference_docname": ["in", ns]}, pluck="parent"))
    put("Event", _docs("Event", names=events))

    # Attachments on the records and on their emails / notes; the files themselves are copied too.
    attach_to = dict(by_type)
    attach_to["Communication"] = list(comms)
    attach_to["FCRM Note"] = list(notes)
    files = set()
    for dt, ns in attach_to.items():
        if ns:
            files.update(frappe.get_all("File", filters={"attached_to_doctype": dt, "attached_to_name": ["in", ns]},
                                        pluck="name"))
    file_docs = _docs("File", names=files)
    put("File", file_docs)
    copied = 0
    for f in file_docs:
        src = _file_path(f)
        if src and os.path.exists(src):
            dst = os.path.join(path, "files", "private" if f.get("is_private") else "public", os.path.basename(src))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1

    with open(os.path.join(path, "crm.json"), "w") as fh:
        json.dump(data, fh, indent=1, default=str)
    counts = {dt: len(v) for dt, v in data["doctypes"].items() if v}
    print("Exported from", frappe.local.site, "to", path, json.dumps(counts, indent=1), "\nfiles copied:", copied)
    return counts


def _file_path(f):
    url = f.get("file_url") or ""
    if not url or url.startswith("http"):
        return None
    return get_files_path(os.path.basename(url), is_private=1 if url.startswith("/private/") else 0)


# ----------------------------------------------------------------------------- import
def _user_fields(doctype):
    meta = frappe.get_meta(doctype)
    fields = [f.fieldname for f in meta.fields if f.fieldtype == "Link" and f.options == "User"]
    return ["owner", "modified_by"] + fields


def import_crm(path: str, dry_run: int = 0, fallback_user: str | None = None, email_account: str | None = None,
               allow_missing_fields: int = 0) -> dict:
    """Insert the export in `path` into this site. See the module docstring."""
    with open(os.path.join(path, "crm.json")) as fh:
        data = json.load(fh)
    if data.get("format") != FORMAT:
        frappe.throw(f"Unknown export format {data.get('format')}")
    docs = data["doctypes"]

    # 1. Conflicts on the records whose timelines we bring.
    conflicts = {}
    for dt in NO_CONFLICT:
        names = [d["name"] for d in docs.get(dt, [])]
        if names and frappe.db.exists("DocType", dt):
            hit = frappe.get_all(dt, filters={"name": ["in", names]}, pluck="name")
            if hit:
                conflicts[dt] = hit
    if conflicts:
        print("CONFLICT - these already exist on", frappe.local.site, "- nothing imported:", json.dumps(conflicts, indent=1))
        return {"conflicts": conflicts}

    # 1b. Filled-in values in fields this site doesn't have would be lost (e.g. CRM Lead fundraising fields
    # on a site without mm_core). Stop for the CRM's own records unless explicitly allowed; report the rest.
    lost = {}
    for dt, rows in docs.items():
        if not rows or not frappe.db.exists("DocType", dt):
            continue
        columns = set(frappe.get_meta(dt).get_valid_columns())
        for row in rows:
            for k, v in row.items():
                if k not in columns and k != "doctype" and not k.startswith("__") and not isinstance(v, list) and v not in (None, "", 0):
                    lost.setdefault(dt, set()).add(k)
    blocking = {dt: sorted(f) for dt, f in lost.items() if dt in NO_CONFLICT + ["FCRM Note"]}
    if blocking and not allow_missing_fields:
        print("MISSING FIELDS - these CRM fields have data but don't exist on", frappe.local.site,
              "- nothing imported (install mm_core / run bench migrate, or pass allow_missing_fields=1):",
              json.dumps(blocking, indent=1))
        return {"missing_fields": blocking}

    # 2. Users and the mail account on this site.
    if not fallback_user:
        acc = frappe.db.get_value("Email Account", {"default_outgoing": 1}, "email_id")
        fallback_user = acc if acc and frappe.db.exists("User", acc) else "Administrator"
    if not email_account:
        email_account = (frappe.db.get_value("Email Account", {"default_incoming": 1, "enable_incoming": 1})
                         or frappe.db.get_value("Email Account", {"default_outgoing": 1}))
    missing_users = set()

    def map_user(u):
        if not u or u in ("Administrator", "Guest") or frappe.db.exists("User", u):
            return u
        missing_users.add(u)
        return fallback_user

    # 3. Insert, in the export's order (masters, primary, activity).
    inserted, skipped = {}, {}
    for dt, rows in docs.items():
        if not rows:
            continue
        if not frappe.db.exists("DocType", dt):
            skipped[dt] = f"doctype not on this site ({len(rows)})"
            continue
        meta = frappe.get_meta(dt)
        ufields = _user_fields(dt)
        n_in = n_skip = 0
        for row in rows:
            if frappe.db.exists(dt, row["name"]):
                n_skip += 1
                continue
            row = dict(row)
            for f in ufields:
                if row.get(f):
                    row[f] = map_user(row[f])
            if row.get("_assign"):
                try:
                    row["_assign"] = json.dumps([map_user(u) for u in json.loads(row["_assign"])])
                except ValueError:
                    pass
            if dt == "ToDo":
                for f in ("allocated_to", "assigned_by"):
                    row[f] = map_user(row.get(f))
            if dt == "Communication" and row.get("email_account"):
                row["email_account"] = email_account or ""
            if dt == "File" and row.get("folder") and not frappe.db.exists("File", row["folder"]):
                row["folder"] = "Home/Attachments"
            if not dry_run:
                doc = frappe.get_doc(row)
                doc.flags.ignore_links = True
                doc.db_insert()
                for child in doc.get_all_children():
                    child.db_insert()
            n_in += 1
        inserted[dt] = n_in
        if n_skip:
            skipped[dt] = f"{n_skip} already here"

    # 4. Attachment files.
    copied = 0
    for scope in ("private", "public"):
        src_dir = os.path.join(path, "files", scope)
        if os.path.isdir(src_dir):
            dst_dir = get_files_path(is_private=1 if scope == "private" else 0)
            for fname in os.listdir(src_dir):
                dst = os.path.join(dst_dir, fname)
                if not os.path.exists(dst):
                    if not dry_run:
                        shutil.copy2(os.path.join(src_dir, fname), dst)
                    copied += 1

    # 5. Numbering continues after what was imported.
    if not dry_run:
        _advance_numbering(docs)
        frappe.db.commit()
        frappe.clear_cache()

    report = {
        "site": frappe.local.site, "dry_run": bool(dry_run), "from": data.get("source_site"),
        "inserted": {k: v for k, v in inserted.items() if v}, "skipped": skipped, "files_copied": copied,
        "users_mapped_to": fallback_user if missing_users else None, "missing_users": sorted(missing_users),
        "email_account": email_account,
        "values_not_kept (field not on this site)": {k: sorted(v) for k, v in lost.items()},
    }
    print(("DRY RUN - nothing written\n" if dry_run else "") + json.dumps(report, indent=1, default=str))
    return report


def _advance_numbering(docs):
    for dt, rows in docs.items():
        if not rows or not frappe.db.exists("DocType", dt):
            continue
        autoname = frappe.get_meta(dt).autoname or ""
        names = [r["name"] for r in rows]
        if autoname == "autoincrement":
            top = max(int(n) for n in names if str(n).isdigit()) if names else 0
            current = frappe.db.sql(f"select max(name) from `tab{dt}`")[0][0] or 0
            frappe.db.set_next_sequence_val(dt, max(top, int(current)), is_val_used=True)
        elif autoname.startswith("naming_series:") or "#" in autoname:
            for n in names:
                m = re.match(r"^(.*?)(\d+)$", str(n))
                if m:
                    frappe.db.sql(
                        "insert into `tabSeries` (name, current) values (%s, %s) "
                        "on duplicate key update current = greatest(current, values(current))",
                        (m.group(1), int(m.group(2))),
                    )
