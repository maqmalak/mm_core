"""System status for the React home page's Database card (System Managers only): database size and health,
largest tables, and server CPU / memory / load — the same figures as POS Awesome's System Status panel, without
depending on that app (the school site doesn't have it) and behind a role check."""

import os
import time

import frappe
from frappe.utils import cint, flt

CACHE_SECONDS = 60


@frappe.whitelist()
def get_status(refresh: int = 0) -> dict:
    frappe.only_for("System Manager")
    key = f"mm_core_system_status::{frappe.local.site}"
    if not cint(refresh):
        cached = frappe.cache.get_value(key)
        if cached:
            return cached
    t0 = time.perf_counter()
    out = {"database": _database(), "server": _server()}
    out["took_ms"] = round((time.perf_counter() - t0) * 1000)
    frappe.cache.set_value(key, out, expires_in_sec=CACHE_SECONDS)
    return out


def _database():
    db = {"engine": frappe.conf.get("db_type") or "mariadb", "name": frappe.conf.get("db_name")}
    try:
        db["version"] = frappe.db.sql("select version()")[0][0]
        name = db["name"] or frappe.db.get_database_name()
        size, tables, rows = frappe.db.sql("""select sum(data_length + index_length), count(*), sum(table_rows)
            from information_schema.tables where table_schema = %s""", (name,))[0]
        db.update({"size": cint(size), "tables": cint(tables), "rows": cint(rows)})
        status = dict(frappe.db.sql("""show global status where variable_name in
            ('Threads_connected', 'Slow_queries', 'Uptime', 'Questions', 'Max_used_connections')"""))
        variables = dict(frappe.db.sql("show global variables where variable_name in ('max_connections', 'innodb_buffer_pool_size')"))
        db.update({
            "connections": cint(status.get("Threads_connected")),
            "max_used_connections": cint(status.get("Max_used_connections")),
            "max_connections": cint(variables.get("max_connections")),
            "slow_queries": cint(status.get("Slow_queries")),
            "uptime": cint(status.get("Uptime")),
            "queries_per_sec": round(flt(status.get("Questions")) / max(1, cint(status.get("Uptime"))), 1),
            "buffer_pool": cint(variables.get("innodb_buffer_pool_size")),
        })
        db["top_tables"] = [{"name": n, "size": cint(s), "rows": cint(r)} for n, s, r in frappe.db.sql(
            """select table_name, data_length + index_length, table_rows from information_schema.tables
            where table_schema = %s order by 2 desc limit 5""", (name,))]
    except Exception as e:
        db["error"] = str(e)[:200]
    return db


def _server():
    srv = {"cpu_cores": os.cpu_count()}
    try:
        import psutil

        mem, disk = psutil.virtual_memory(), psutil.disk_usage(frappe.get_site_path())
        srv.update({
            "cpu_percent": psutil.cpu_percent(interval=0.3),
            "memory_percent": mem.percent, "memory_total": mem.total, "memory_used": mem.used,
            "disk_percent": disk.percent, "disk_total": disk.total, "disk_used": disk.used,
            "load_avg": [round(x, 2) for x in os.getloadavg()] if hasattr(os, "getloadavg") else None,
            "uptime": int(time.time() - psutil.boot_time()),
        })
    except Exception as e:
        srv["error"] = str(e)[:200]
    return srv
