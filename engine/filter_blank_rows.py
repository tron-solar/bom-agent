"""Canonical blank-row filter for Tron BOM output.
RULE: in the data region (row 5 down), hide EVERY row whose QUANTITY (col A) is
empty/blank -- whether it's a menu SKU row OR a fully-empty spacer row. The only
row exempted is the S-clip formula row on Solar (A30), which holds a formula that
may evaluate blank only when no modules exist; we keep it if it has a formula.
Rows are hidden, not deleted (clear the filter to restore)."""
import openpyxl

def has_qty(ws, r):
    v = ws.cell(r, 1).value
    if v is None: return False
    if isinstance(v, str):
        s = v.strip()
        if s == "": return False
        # a formula string (e.g. the S-clip '=IF(...)') counts as present
        return True
    return True  # numeric

def apply_qty_filter(path):
    wb = openpyxl.load_workbook(path)
    for sh in ("Solar BOM", "Electrical BOM"):
        if sh not in wb.sheetnames: continue
        ws = wb[sh]
        for r in range(5, ws.max_row + 1):
            ws.row_dimensions[r].hidden = not has_qty(ws, r)
    wb.save(path)

if __name__ == "__main__":
    import sys
    apply_qty_filter(sys.argv[1])
    print("filtered", sys.argv[1])
