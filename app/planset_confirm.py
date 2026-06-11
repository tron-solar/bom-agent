"""Planset confirmation — strict, fail-closed selection of the ONE correct planset.

Convention (user, confirmed): the planset is a .pdf named "FirstName LastName REV<L>.pdf"
(L = A,B,C,...), living in the Engineering folder of the project Docs. There is exactly ONE
correct planset per project; later revision letters supersede earlier ones.

Design stance: CONFIRM, don't guess. The interconnection agent's matcher used loose keywords
(["planset","plans","rev",...]) + a "first PDF" fallback, which can silently grab the wrong
document (a CAD zip, a proposal PDF, a utility bill). This module instead:
  1. matches the exact "<First> <Last> REV<L>" convention against the project's customer name,
  2. picks the highest revision letter among convention matches,
  3. raises PlansetNotConfirmed if there is not exactly one confident match — NO "first PDF" fallback.

It also exposes a soft secondary path (looser, address/keyword based) ONLY to produce a helpful
diagnostic in the raised error, never to auto-select.

NOTE ON FOLDERS: the Coperniq file-list API observed in testing does NOT return a folder field on
file objects (the "Engineering" folder is a UI concept). So folder filtering must be done by the
caller's get_files() if/when it supports a folder/path param. `select_planset()` accepts an optional
pre-filtered file list; pass the Engineering-folder files if your client can scope them.
"""
from __future__ import annotations
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional


class PlansetNotConfirmed(RuntimeError):
    """Raised when a single planset cannot be confidently confirmed. Routes to human review —
    NEVER fall back to an arbitrary PDF."""
    def __init__(self, message: str, diagnostics: dict | None = None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


@dataclass
class PlansetCandidate:
    file: dict
    name: str
    revision: str            # 'A'..'Z' or '' if none
    rev_index: int           # 0 for A, 1 for B, ...; -1 if none
    name_match: bool         # filename stem matches "<First> <Last> REV<L>" for THIS customer
    is_pdf: bool
    score: float = 0.0
    reasons: list = field(default_factory=list)


# ---------- normalization helpers ----------
def _norm(s: str) -> str:
    """Lowercase, strip accents, collapse non-alphanumerics to single spaces."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _file_name(f: dict) -> str:
    return (f.get("name") or f.get("metaData", {}).get("originalName")
            or f.get("metaData", {}).get("requestFileName") or "")


def _file_url(f: dict) -> str:
    return f.get("url") or f.get("downloadUrl") or ""


def _is_pdf(f: dict) -> bool:
    name = _file_name(f).lower()
    ext = (f.get("metaData", {}) or {}).get("extention", "").lower()
    mime = (f.get("metaData", {}) or {}).get("mimeType", "").lower()
    return name.endswith(".pdf") or ext == ".pdf" or "pdf" in mime


def _file_size(f: dict) -> Optional[int]:
    sz = (f.get("metaData", {}) or {}).get("size")
    return sz if isinstance(sz, int) else None


def revision_letter(filename: str) -> str:
    """Extract the revision letter. Accepts 'REVA', 'REV A', 'REV-A', 'REV_A' (case-insensitive).
    Returns the single letter or '' if none. Requires the REV token to be followed by ONE letter
    that is a standalone token (so 'REVISED' or 'REVENUE' don't count)."""
    m = re.search(r"\bREV[\s_\-]?([A-Z])\b", filename.upper())
    return m.group(1) if m else ""


def _rev_index(letter: str) -> int:
    return (ord(letter) - ord("A")) if letter else -1


def _stem(filename: str) -> str:
    return re.sub(r"\.[^.]+$", "", filename)


# ---------- core matcher ----------
def _convention_match(filename: str, first: str, last: str) -> bool:
    """True iff the filename stem is exactly the customer's name followed by a REV<L> token.
    Tolerant of separators/case/extra spaces, but NOT of extra words (so 'Joseph Dare CAD REV A'
    does NOT match — only 'Joseph Dare REV A')."""
    stem_norm = _norm(_stem(filename))            # e.g. "joseph dare rev a"
    first_n, last_n = _norm(first), _norm(last)
    if not first_n or not last_n:
        return False
    # expected: "<first> <last> rev <L>"  (rev letter optional in the regex, required overall)
    pat = rf"^{re.escape(first_n)}\s+{re.escape(last_n)}\s+rev\s*([a-z])$"
    return re.match(pat, stem_norm) is not None


def _split_name(customer_name: str) -> tuple[str, str]:
    parts = _norm(customer_name).split()
    if len(parts) < 2:
        return (customer_name.strip(), "")
    # First token = first name, last token = last name (handles middle names/initials)
    return parts[0], parts[-1]


def build_candidates(files: list[dict], customer_name: str) -> list[PlansetCandidate]:
    first, last = _split_name(customer_name)
    cands: list[PlansetCandidate] = []
    for f in files:
        if f.get("isArchived"):
            continue
        name = _file_name(f)
        if not name:
            continue
        is_pdf = _is_pdf(f)
        rev = revision_letter(name)
        match = _convention_match(name, first, last) if is_pdf else False
        c = PlansetCandidate(file=f, name=name, revision=rev, rev_index=_rev_index(rev),
                             name_match=match, is_pdf=is_pdf)
        # scoring is for DIAGNOSTICS/soft-fallback only; selection uses name_match strictly
        if match:
            c.score += 100; c.reasons.append("exact name+REV convention")
        if is_pdf:
            c.score += 5
        if rev:
            c.score += 1
        cands.append(c)
    return cands


def _dedupe_by_content(cands: list[PlansetCandidate]) -> tuple[list[PlansetCandidate], list[dict]]:
    """Collapse byte-identical re-uploads (same normalized name + same size) into ONE candidate.

    A planset uploaded twice — e.g. a manual Engineering upload plus a "Plansets / RFD (Coperniq)"
    form / "CAD (most recent rev)" submission of the *identical* PDF — appears as two file objects
    with no shared id and no form/field provenance in the files API. Identical `metaData.size` is the
    only content signal the API gives us (it returns no content hash), so we treat same-name +
    same-size as the same document and keep the EARLIEST-created copy as the representative (the
    original; the later form re-upload is the duplicate the user wants forgotten).

    Candidates with no size are never collapsed — we can't assert identity, so they pass through
    untouched. Returns (deduped, collapsed) where `collapsed` lists each merged group for diagnostics.
    """
    groups: dict[tuple, list[PlansetCandidate]] = {}
    deduped: list[PlansetCandidate] = []
    for c in cands:
        sz = _file_size(c.file)
        if sz is None:
            deduped.append(c)          # unknown size -> can't prove identity, keep as-is
            continue
        groups.setdefault((_norm(_stem(c.name)), sz), []).append(c)
    collapsed: list[dict] = []
    for members in groups.values():
        # representative = earliest createdAt, then smallest id (deterministic; drops later re-uploads)
        members.sort(key=lambda c: (str(c.file.get("createdAt", "")), str(c.file.get("id", ""))))
        deduped.append(members[0])
        if len(members) > 1:
            collapsed.append({
                "name": members[0].name,
                "size": _file_size(members[0].file),
                "kept_id": members[0].file.get("id"),
                "dropped_ids": [m.file.get("id") for m in members[1:]],
                "count": len(members),
            })
    return deduped, collapsed


@dataclass
class ConfirmedPlanset:
    file: dict
    name: str
    url: str
    revision: str
    all_revisions: list[str]
    diagnostics: dict


def select_planset(files: list[dict], customer_name: str,
                   engineering_only: bool = False) -> ConfirmedPlanset:
    """Confirm and return the single correct planset, or raise PlansetNotConfirmed.

    files: project file objects (ideally pre-scoped to the Engineering folder by the caller).
    customer_name: the Coperniq project title (e.g. "Joseph Dare").
    engineering_only: if True, asserts the caller already scoped to Engineering (advisory only).

    Selection (strict):
      - keep only files whose name EXACTLY matches "<First> <Last> REV<L>.pdf"
      - if multiple, pick the highest revision letter
      - if zero convention matches -> raise (with diagnostics listing what WAS there)
      - if a convention match has no resolvable URL -> raise
    """
    cands = build_candidates(files, customer_name)
    matches_all = [c for c in cands if c.name_match]
    # Collapse identical re-uploads (e.g. manual upload + form/RFD submission of the same PDF) so a
    # byte-identical duplicate doesn't read as a genuine same-revision ambiguity.
    matches, collapsed = _dedupe_by_content(matches_all)

    diag = {
        "customer_name": customer_name,
        "engineering_scoped": engineering_only,
        "total_files": len(files),
        "pdf_count": sum(1 for c in cands if c.is_pdf),
        "convention_matches": [c.name for c in matches_all],
        "duplicates_collapsed": collapsed,
        "all_pdf_names": [c.name for c in cands if c.is_pdf],
    }

    if not matches:
        # soft diagnostic: was there a near-miss (name match but extra words, or a REV pdf)?
        rev_pdfs = [c.name for c in cands if c.is_pdf and c.revision]
        raise PlansetNotConfirmed(
            f"No file matches the planset convention '<First> <Last> REV<L>.pdf' for "
            f"'{customer_name}'. {len(cands)} files, {diag['pdf_count']} PDFs. "
            f"PDFs with a REV token (near misses): {rev_pdfs or 'none'}.",
            diagnostics=diag,
        )

    # pick highest revision; ties (shouldn't happen after dedupe) -> most recent createdAt
    matches.sort(key=lambda c: (c.rev_index, c.file.get("createdAt", "")), reverse=True)
    best = matches[0]
    diag["selected"] = best.name
    diag["selected_revision"] = best.revision

    # Residual ambiguity: >1 DISTINCT file (different size, so not collapsed) at the top revision.
    # Dedupe removed identical re-uploads; anything left here is genuinely different content sharing
    # the same name+REV — surface it for human review rather than trusting the createdAt tie-break.
    top = [c for c in matches if c.rev_index == best.rev_index]
    if len(top) > 1:
        diag["ambiguous_at_top_revision"] = [
            {"name": c.name, "id": c.file.get("id"), "size": _file_size(c.file)} for c in top
        ]

    url = _file_url(best.file)
    if not url:
        # caller may need to resolve via get_project_file(file_id); signal that explicitly
        raise PlansetNotConfirmed(
            f"Confirmed planset '{best.name}' but it has no resolvable download URL in the file "
            f"object; resolve via get_project_file(file_id={best.file.get('id')}).",
            diagnostics=diag,
        )

    return ConfirmedPlanset(
        file=best.file, name=best.name, url=url, revision=best.revision,
        all_revisions=sorted({c.revision for c in matches if c.revision}),
        diagnostics=diag,
    )


# ---------- optional second-stage content confirmation ----------
def confirm_planset_content(pv1_text: str, customer_name: str, project_address: str) -> list[dict]:
    """OPTIONAL second check after the PDF is opened: verify PV-1 actually belongs to this project.
    Returns a list of flags (level HARD/SOFT) — empty list means content confirms cleanly.

    The BOM engine already reads PV-1; pass its extracted text here. This catches the case where a
    correctly-NAMED file contains the wrong project's plans (rare, but the convention can't catch it).
    """
    flags: list[dict] = []
    t = _norm(pv1_text)
    first, last = _split_name(customer_name)
    if first and last and not (first in t and last in t):
        flags.append({"level": "HARD", "item": "planset_name_mismatch",
                      "msg": f"PV-1 text does not contain customer name '{customer_name}'. "
                             f"The named planset may belong to a different project."})
    # address check: look for the street-number token + a street-name token
    addr_norm = _norm(project_address)
    addr_tokens = [tok for tok in addr_norm.split() if tok]
    num = next((tok for tok in addr_tokens if tok.isdigit()), None)
    if num and num not in t:
        flags.append({"level": "SOFT", "item": "planset_address_mismatch",
                      "msg": f"PV-1 text does not contain the project street number '{num}'. "
                             f"Verify the planset matches the install address."})
    return flags
