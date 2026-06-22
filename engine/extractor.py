"""
Planset PDF Extractor
Uses Claude Vision (claude-sonnet-4-20250514) to extract interconnection fields
from Tron Solar planset PDFs.

Part of the planset-extractor skill.
"""

import os
import base64
import json
import httpx
from dataclasses import dataclass, field
from typing import Optional
import fitz  # PyMuPDF


CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
# Current Sonnet alias (no date suffix). The old dated snapshot claude-sonnet-4-20250514 retired
# 2026-06-15 and now 404s. Override per-env without a code change via CLAUDE_MODEL.
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")


@dataclass
class ArrayInfo:
    tilt: float
    azimuth: float
    module_count: int
    dc_size_kw: float   # calculated: module_count * module_wattage / 1000
    roof_plane: Optional[int] = None   # which physical roof plane (1,2,...) this array sits on
    strings_on_plane: Optional[int] = None  # distinct string colors confined to THIS plane (PV-3.1)


@dataclass
class PlansetData:
    """All data extracted from the planset PDF."""

    # --- Cover Sheet (PV-1) ---
    customer_name: str
    customer_address: str
    system_size_dc_kw: float
    system_size_ac_kw: float
    module_manufacturer: str
    module_model: str
    module_wattage: float
    module_quantity: int
    inverter_manufacturer: str
    inverter_model: str
    inverter_quantity: int
    battery_manufacturer: Optional[str]
    battery_model: Optional[str]
    battery_quantity: Optional[int]
    battery_kwh: Optional[float]
    has_expansion_unit: bool
    expansion_model: Optional[str]
    expansion_quantity: Optional[int]
    utility_company: str
    ahj: str
    design_date: str

    # --- Three-Line Diagram (PV-5) ---
    meter_number: Optional[str] = None
    service_type: Optional[str] = None      # "underground" or "overhead"
    nominal_voltage: Optional[float] = None
    main_panel_amperage: Optional[int] = None
    interconnection_method: Optional[str] = None  # "load side" or "line side"

    # --- Roof Plan (PV-3) ---
    arrays: list = field(default_factory=list)  # list[ArrayInfo]

    # --- String map (PV-3.1) ---
    # strings never cross roof planes; one entry per physical plane: {plane: string_count}
    strings_per_plane: dict = field(default_factory=dict)

    # --- Expansion mount kit (resolved: plans -> master note -> default wall) ---
    expansion_mount_kit: Optional[str] = None   # "stack" or "wall"

    # --- Confidence & Warnings ---
    confidence_scores: dict = field(default_factory=dict)
    extraction_warnings: list = field(default_factory=list)


class PlansetExtractor:
    """
    Extracts interconnection data from Tron Solar planset PDFs
    using Claude Vision.
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ["ANTHROPIC_API_KEY"]
        self.headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    def _pdf_page_to_base64(self, pdf_path: str, page_number: int) -> str:
        """Extract a single page from PDF and return as base64 PNG."""
        doc = fitz.open(pdf_path)
        page = doc[page_number]
        # Render at 2x resolution for better OCR
        mat = fitz.Matrix(2.0, 2.0)
        pix = page.get_pixmap(matrix=mat)
        img_bytes = pix.tobytes("png")
        doc.close()
        return base64.standard_b64encode(img_bytes).decode("utf-8")

    def extract_page_pdf(self, pdf_path: str, label: str, output_path: str) -> bool:
        """
        Extract a single labeled page (e.g. 'PV-5') from the planset and save
        as a new single-page PDF. Returns True if found, False if not found
        (falls back to page 0 in that case).
        """
        page_num = self._find_page_by_label(pdf_path, label)
        found = page_num is not None
        if page_num is None:
            page_num = 0
        doc = fitz.open(pdf_path)
        new_doc = fitz.open()
        new_doc.insert_pdf(doc, from_page=page_num, to_page=page_num)
        new_doc.save(output_path)
        doc.close()
        new_doc.close()
        return found

    def _find_page_by_label(self, pdf_path: str, label: str) -> Optional[int]:
        """
        Find page number by searching for a label (e.g. 'PV-5') in page text.
        Returns 0-indexed page number or None if not found.
        """
        doc = fitz.open(pdf_path)
        for i, page in enumerate(doc):
            text = page.get_text()
            if label in text:
                doc.close()
                return i
        doc.close()
        return None

    async def _call_claude(self, images_b64: list, prompt: str) -> str:
        """Call Claude Vision with one or more page images."""
        content = []
        for img_b64 in images_b64:
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": img_b64,
                },
            })
        content.append({"type": "text", "text": prompt})

        async with httpx.AsyncClient() as client:
            response = await client.post(
                CLAUDE_API_URL,
                headers=self.headers,
                json={
                    "model": CLAUDE_MODEL,
                    "max_tokens": 2000,
                    "messages": [{"role": "user", "content": content}],
                },
                timeout=60,
            )
            response.raise_for_status()
            return response.json()["content"][0]["text"]

    def _parse_json(self, raw: str) -> dict:
        """Parse JSON from Claude response, stripping markdown fences if present."""
        clean = raw.strip()
        if clean.startswith("```"):
            lines = clean.split("\n")
            clean = "\n".join(lines[1:-1])
        return json.loads(clean)

    async def extract(self, pdf_path: str, coperniq_project: Optional[dict] = None,
                      master_note_form: Optional[dict] = None) -> PlansetData:
        """
        Main extraction entry point.
        Extracts data from PV-1, PV-3, PV-3.1 (string map), and PV-5.
        coperniq_project: the get_project() dict (fallback note source / other fields).
        master_note_form: the Coperniq get_form() dict for the project's "Master Note" form. The
        pipeline fetches it via list_project_forms(project_id) -> find name=="Master Note" ->
        get_form(form_id). This form (NOT project.custom) is where stack/wall wording actually lives.
        """
        warnings = []

        # --- Step 1: Extract cover sheet (PV-1, always page 0) ---
        cover_b64 = self._pdf_page_to_base64(pdf_path, 0)
        cover_data = await self._extract_cover_sheet(cover_b64)

        # --- Step 2: Extract three-line diagram (PV-5) ---
        pv5_page = self._find_page_by_label(pdf_path, "PV-5")
        if pv5_page is None:
            pv5_page = 4
            warnings.append("PV-5 page not found by label; using fallback page 4")
        pv5_b64 = self._pdf_page_to_base64(pdf_path, pv5_page)
        electrical_data = await self._extract_three_line(pv5_b64)

        # --- Step 3: Extract roof plan (PV-3) ---
        pv3_page = self._find_page_by_label(pdf_path, "PV-3")
        if pv3_page is None:
            pv3_page = 2
            warnings.append("PV-3 page not found by label; using fallback page 2")
        pv3_b64 = self._pdf_page_to_base64(pdf_path, pv3_page)
        module_wattage = cover_data.get("module_wattage", 400)
        array_data = await self._extract_roof_plan(pv3_b64, module_wattage)

        # --- Step 3.1: Extract string map (PV-3.1) — strings-per-plane by dashed-line color ---
        pv31_page = self._find_page_by_label(pdf_path, "PV-3.1")
        if pv31_page is None:
            # some plansets put the string map on PV-3 itself
            pv31_page = pv3_page
            warnings.append("PV-3.1 page not found by label; using PV-3 page for string map")
        pv31_b64 = self._pdf_page_to_base64(pdf_path, pv31_page)
        string_data = await self._extract_string_map(pv31_b64)

        # --- Step 4: Resolve expansion mount kit (plans -> master note -> default wall) ---
        plan_mount = string_data.get("plan_mount") or array_data.get("plan_mount")
        master_notes = None
        if coperniq_project is not None or master_note_form is not None:
            try:
                from electrical_engine import master_notes_from_coperniq
                master_notes = master_notes_from_coperniq(project=coperniq_project,
                                                          form=master_note_form)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"master-note parse failed: {e}")
        try:
            from electrical_engine import resolve_expansion_mount
            mount_kit = resolve_expansion_mount(plan_mount=plan_mount, master_notes=master_notes)
        except Exception:
            mount_kit = "wall"
        if master_note_form is None and not plan_mount:
            warnings.append("expansion mount: no plan keyword and no Master Note form supplied; "
                            "defaulted to wall — fetch get_form(Master Note) and pass master_note_form")

        # --- Merge and return ---
        return self._merge(cover_data, electrical_data, array_data,
                           string_data, mount_kit, warnings)

    async def _extract_string_map(self, image_b64: str) -> dict:
        """
        Extract strings-per-roof-plane from the PV-3.1 string map.

        Governing facts (Tron Solar):
          * A STRING is one dashed line of a SINGLE color. The STRING LEGEND on PV-3.1 maps each
            color to a string number (e.g. STRING #1 = red dashed, #2 = cyan dashed, ...).
          * Strings NEVER cross roof planes — every string is fully contained within one plane.
        So strings_per_plane[plane] = count of DISTINCT string colors whose dashed routing appears
        within that plane's module boundary. This count, not module count, drives J-boxes
        (max(1, ceil(strings_on_plane/4)) per plane).
        """
        prompt = """You are reading a Tron Solar STRING MAP (PV-3.1).

There is a STRING LEGEND that maps each dashed-line COLOR to a string number (STRING #1, #2, ...).
Each string is one continuously-colored dashed line. STRINGS NEVER CROSS ROOF PLANES — every
string is fully contained on a single physical roof plane.

For EACH physical roof plane (Roof #1, Roof #2, ...), determine how many DISTINCT string colors
(i.e. how many separate strings) are routed within that plane's modules. Count by color, using the
legend — do NOT infer string count from module count.

Also report whether any text near the expansion unit says "stack" or "wall mount".

Return ONLY valid JSON, no other text:
{
  "planes": [
    { "plane": 1, "string_numbers": [1,2,3,4,5], "string_count": 5 },
    { "plane": 2, "string_numbers": [6,7], "string_count": 2 }
  ],
  "total_strings": 7,
  "plan_mount": "stack or wall or null",
  "confidence": 0.0
}

Rules:
- string_count for a plane = number of distinct string colors confined to that plane
- string_numbers lists the legend string numbers found on that plane
- total_strings = sum of all planes' string_count (should equal the project string total)
- plan_mount: "stack"/"wall" only if explicitly written near the expansion unit, else null
- confidence 0.0-1.0 reflecting how clearly the colored strings could be separated per plane"""

        raw = await self._call_claude([image_b64], prompt)
        try:
            return self._parse_json(raw)
        except Exception:
            return {"planes": [], "total_strings": None, "plan_mount": None, "confidence": 0.0}

    async def _extract_cover_sheet(self, image_b64: str) -> dict:
        """Extract all fields from PV-1 cover sheet."""
        prompt = """You are extracting data from a Tron Solar planset cover sheet (PV-1).
Extract the following fields and return ONLY valid JSON, no other text.

{
  "customer_name": "string",
  "customer_address": "string",
  "system_size_dc_kw": number,
  "system_size_ac_kw": number,
  "module_manufacturer": "string",
  "module_model": "string",
  "module_wattage": number,
  "module_quantity": number,
  "inverter_manufacturer": "string",
  "inverter_model": "string",
  "inverter_quantity": number,
  "battery_manufacturer": "string or null",
  "battery_model": "string or null",
  "battery_quantity": "number or null",
  "battery_kwh": "number or null",
  "has_expansion_unit": boolean,
  "expansion_model": "string or null",
  "expansion_quantity": "number or null",
  "utility_company": "string",
  "ahj": "string",
  "design_date": "string",
  "confidence": {
    "module_info": 0.0,
    "inverter_info": 0.0,
    "battery_info": 0.0,
    "system_size": 0.0
  }
}

Rules:
- module_wattage is in Watts (W), not kW
- battery_kwh is total capacity for ALL batteries combined
- has_expansion_unit is true if a Powerwall 3 Expansion Unit or model 1807000 appears
- inverter_manufacturer must match exactly one of these ComEd options if possible:
  Tesla Inc., Enphase Energy, Inc., SolarEdge Technologies Ltd.
- battery_manufacturer must match exactly: Tesla Inc. (for Powerwall)
- Return null for missing optional fields, never omit keys
- confidence values are 0.0-1.0"""

        raw = await self._call_claude([image_b64], prompt)
        return self._parse_json(raw)

    async def _extract_three_line(self, image_b64: str) -> dict:
        """Extract electrical data from PV-5 three-line diagram."""
        prompt = """You are extracting data from a Tron Solar three-line electrical diagram (PV-5).
Extract the following fields and return ONLY valid JSON, no other text.

{
  "meter_number": "string or null",
  "service_type": "underground or overhead or null",
  "nominal_voltage": "number or null",
  "main_panel_amperage": "integer or null",
  "interconnection_method": "load side or line side or null"
}

Rules:
- meter_number is the electric meter serial/ID number shown near the meter symbol
- service_type: look for labels like "underground service entrance" or "overhead service"
- nominal_voltage: the utility service voltage (e.g. 120/240, 240) — return as number (use 240 for 120/240V)
- main_panel_amperage: the main breaker/panel amperage (e.g. 200, 100, 400)
- interconnection_method: "load side" if the PV connects on the load side of the main breaker;
  "line side" if it connects between the meter and main breaker
- Return null for any field not clearly visible"""

        raw = await self._call_claude([image_b64], prompt)
        return self._parse_json(raw)

    async def _extract_roof_plan(self, image_b64: str, module_wattage: float) -> dict:
        """Extract array layout from PV-3 roof plan."""
        prompt = f"""You are extracting solar array data from a Tron Solar roof plan (PV-3).
The module wattage is {module_wattage}W.

Extract all arrays and return ONLY valid JSON, no other text.

{{
  "arrays": [
    {{
      "tilt": number,
      "azimuth": number,
      "module_count": integer,
      "dc_size_kw": number
    }}
  ]
}}

Rules:
- Each distinct roof face with modules is a separate array
- tilt: the roof pitch/tilt angle in degrees (e.g. 20, 25, 30)
- azimuth: compass direction in degrees (South=180, East=90, West=270, North=0/360)
- module_count: number of panels on that array
- dc_size_kw: module_count * {module_wattage} / 1000 (calculate this yourself)
- If tilt or azimuth is not labeled, make a reasonable estimate based on roof orientation
- Return at least one array even if details are unclear"""

        raw = await self._call_claude([image_b64], prompt)
        return self._parse_json(raw)

    def _merge(
        self,
        cover: dict,
        electrical: dict,
        roof: dict,
        strings: dict,
        mount_kit: str,
        warnings: list,
    ) -> PlansetData:
        """Merge extraction results into a PlansetData instance."""
        # strings-per-plane from PV-3.1 (by dashed-line color; strings never cross planes)
        strings_per_plane = {}
        for p in strings.get("planes", []) or []:
            plane = p.get("plane")
            cnt = p.get("string_count")
            if plane is not None and cnt is not None:
                strings_per_plane[int(plane)] = int(cnt)

        arrays = []
        for idx, a in enumerate(roof.get("arrays", []), start=1):
            plane = a.get("roof_plane", idx)
            arrays.append(ArrayInfo(
                tilt=a.get("tilt", 20.0),
                azimuth=a.get("azimuth", 180.0),
                module_count=a.get("module_count", 0),
                dc_size_kw=a.get("dc_size_kw", 0.0),
                roof_plane=plane,
                strings_on_plane=strings_per_plane.get(int(plane)),
            ))

        confidence = cover.pop("confidence", {})

        return PlansetData(
            # Cover sheet
            customer_name=cover.get("customer_name", ""),
            customer_address=cover.get("customer_address", ""),
            system_size_dc_kw=cover.get("system_size_dc_kw", 0.0),
            system_size_ac_kw=cover.get("system_size_ac_kw", 0.0),
            module_manufacturer=cover.get("module_manufacturer", ""),
            module_model=cover.get("module_model", ""),
            module_wattage=cover.get("module_wattage", 0.0),
            module_quantity=cover.get("module_quantity", 0),
            inverter_manufacturer=cover.get("inverter_manufacturer", ""),
            inverter_model=cover.get("inverter_model", ""),
            inverter_quantity=cover.get("inverter_quantity", 1),
            battery_manufacturer=cover.get("battery_manufacturer"),
            battery_model=cover.get("battery_model"),
            battery_quantity=cover.get("battery_quantity"),
            battery_kwh=cover.get("battery_kwh"),
            has_expansion_unit=cover.get("has_expansion_unit", False),
            expansion_model=cover.get("expansion_model"),
            expansion_quantity=cover.get("expansion_quantity"),
            utility_company=cover.get("utility_company", ""),
            ahj=cover.get("ahj", ""),
            design_date=cover.get("design_date", ""),
            # Electrical
            meter_number=electrical.get("meter_number"),
            service_type=electrical.get("service_type"),
            nominal_voltage=electrical.get("nominal_voltage"),
            main_panel_amperage=electrical.get("main_panel_amperage"),
            interconnection_method=electrical.get("interconnection_method"),
            # Arrays
            arrays=arrays,
            # String map (PV-3.1)
            strings_per_plane=strings_per_plane,
            # Expansion mount kit (resolved)
            expansion_mount_kit=mount_kit,
            # Metadata
            confidence_scores=confidence,
            extraction_warnings=warnings,
        )
