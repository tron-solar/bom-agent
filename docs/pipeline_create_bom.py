"""
pipeline_create_bom.py — orchestrator for the create_bom -> draft BOM -> review-comment flow.

Runs in the Railway bom-trigger service (NOT inside the MCP — the MCP cannot watch for create_bom
opening). Every external dependency is an INJECTED CALLABLE so this stays headless and testable:

    mcp.get_project(project_id, include_virtual_properties=True)
    mcp.list_project_files(project_id)            # caller paginates ?page=N&page_size=100
    mcp.get_project_file(file_id, project_id)
    mcp.list_project_forms(project_id)
    mcp.get_form(form_id)
    mcp.create_project_file(project_id, url, name=..., phaseInstanceId=...)
    mcp.create_project_comment(project_id, body)
    download(url) -> local_path        # fetch the planset PDF's downloadUrl
    upload_to_bucket(local_path) -> public_url     # UNKNOWN #1 — host the finished BOM
    mention(user_id, name) -> str                  # UNKNOWN #2 — Coperniq @-tag markup

The engine modules (extractor, racking_engine, electrical_engine, bom_writer, filter_blank_rows,
planset_confirm) are imported normally. This file wires them to Coperniq.
"""
from __future__ import annotations
import os


def run_create_bom(project_id, *, mcp, download, upload_to_bucket, mention,
                   engine, outputs_dir="/tmp/bom_out"):
    """Execute the full pipeline for one project. Returns a result dict.

    engine: an object exposing the engine entry points your build already uses, e.g.
        engine.extract(pdf_path, project, master_note_form) -> extracted fields
        engine.build_bom(extracted) -> (solar_rows, electrical_rows, special_orders, flags, name)
        engine.write(name, header, solar_rows, electrical_rows, special_orders, outputs_dir) -> xlsx path
    (Adapt the three calls to your actual consolidated builder; the orchestration around them is the point.)
    """
    os.makedirs(outputs_dir, exist_ok=True)

    # --- 1. PULL project + confirm planset + master note ---
    project = mcp.get_project(project_id, include_virtual_properties=True)
    assignee = (project.get("custom", {}) or {}).get("create_bom_assignee") or {}
    assignee_id, assignee_name = assignee.get("id"), \
        f"{assignee.get('firstName','')} {assignee.get('lastName','')}".strip()

    files = _all_files(mcp, project_id)
    planset = _confirm_planset(files, project)        # raises PlansetNotConfirmed if none
    forms = mcp.list_project_forms(project_id) or []
    mn_form = next((f for f in forms if str(f.get("name","")).strip().lower() == "master note"), None)
    master_note_form = mcp.get_form(mn_form["id"]) if mn_form else None

    # --- 2. DOWNLOAD planset PDF ---
    pdf_path = download(planset["downloadUrl"])

    # --- 3. RUN ENGINE ---
    extracted = engine.extract(pdf_path, project, master_note_form)
    solar_rows, electrical_rows, special_orders, flags, base_name = engine.build_bom(extracted)
    hard = [f for f in flags if f.get("level") == "HARD"]
    notes = [f for f in flags if f.get("level") == "NOTE"]

    # --- 3b. HARD HOLD -> comment the holds, DO NOT upload ---
    if hard:
        body = _hold_comment(base_name, hard, mention, assignee_id, assignee_name)
        mcp.create_project_comment(project_id, body=body)
        return {"status": "HELD", "hard_flags": hard, "uploaded": False}

    # --- 4. WRITE + HOST + ATTACH (draft = name convention) ---
    xlsx_path = engine.write(base_name, _header(project, extracted),
                             solar_rows, electrical_rows, special_orders, outputs_dir)
    public_url = upload_to_bucket(xlsx_path)
    draft_name = f"{base_name}_DRAFT.xlsx"
    file_phase = project.get("phaseInstanceId")
    mcp.create_project_file(project_id, url=public_url, name=draft_name,
                            phaseInstanceId=file_phase)

    # --- 5. COMMENT + TAG the create_bom assignee to review ---
    body = _review_comment(draft_name, solar_rows, electrical_rows, notes,
                           mention, assignee_id, assignee_name)
    mcp.create_project_comment(project_id, body=body)

    return {"status": "DRAFT_UPLOADED", "file": draft_name, "notes": notes, "url": public_url}


# ---------- helpers ----------

class PlansetNotConfirmed(Exception):
    pass


def _all_files(mcp, project_id, max_pages=5):
    """Paginate list_project_files. The MCP wrapper should accept page/page_size; if it returns all
    at once, this still works (single page)."""
    out, page = [], 1
    while page <= max_pages:
        batch = mcp.list_project_files(project_id, page=page, page_size=100) \
            if _accepts_paging(mcp.list_project_files) else mcp.list_project_files(project_id)
        if not batch:
            break
        out.extend(batch)
        if not _accepts_paging(mcp.list_project_files) or len(batch) < 100:
            break
        page += 1
    return out


def _accepts_paging(fn):
    try:
        import inspect
        return "page" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def _confirm_planset(files, project):
    """Match '<First> <Last> REV<L>.pdf', pick highest revision. Strict — no fallback (planset_confirm
    logic). Returns the file dict (must carry a downloadUrl)."""
    import re
    title = (project.get("title") or "").strip()
    parts = title.split()
    if len(parts) < 2:
        raise PlansetNotConfirmed(f"Cannot derive name from project title {title!r}")
    first, last = parts[0], parts[-1]
    pat = re.compile(rf"{re.escape(first)}\s+{re.escape(last)}\s+REV([A-Z])\.pdf", re.IGNORECASE)
    cands = []
    for f in files:
        m = pat.search(str(f.get("name", "")))
        if m:
            cands.append((m.group(1).upper(), f))
    if not cands:
        raise PlansetNotConfirmed(f"No '{first} {last} REV<L>.pdf' among {len(files)} files")
    cands.sort(key=lambda t: t[0])
    return cands[-1][1]   # highest revision letter


def _header(project, extracted):
    # customer name from planset (extracted) per rule; zone/address from project
    return {
        "customer_name": extracted.get("planset_customer") or project.get("title"),
        "warehouse_zone": extracted.get("warehouse_zone", ""),
        "customer_address": (project.get("address") or [""])[0],
    }


def _tag(mention, user_id, name):
    return mention(user_id, name) if (mention and user_id) else (name or "the create_bom assignee")


def _review_comment(draft_name, solar_rows, electrical_rows, notes, mention, uid, uname):
    lines = [f"Draft BOM generated and attached: {draft_name}",
             f"Solar lines: {len([q for q in solar_rows.values() if q])} | "
             f"Electrical lines: {len([q for q in electrical_rows.values() if q])}"]
    if notes:
        lines.append("Confidence-report NOTES (verify):")
        lines += [f"  - {n.get('msg', n.get('detail',''))}" for n in notes]
    lines.append(f"{_tag(mention, uid, uname)} please double-check this draft BOM before it is finalized.")
    return "\n".join(lines)


def _hold_comment(base_name, hard, mention, uid, uname):
    lines = [f"BOM for {base_name} is ON HOLD — engine raised HARD flags; no draft was attached:"]
    lines += [f"  - {h.get('item')}: {h.get('msg', h.get('detail',''))}" for h in hard]
    lines.append(f"{_tag(mention, uid, uname)} please correct the source data and re-run create_bom.")
    return "\n".join(lines)
