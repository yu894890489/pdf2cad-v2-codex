"""Conservative, deterministic stock classification from the raw BOM."""
import re
import unicodedata


def classify(specification):
    s = unicodedata.normalize("NFKC", specification).upper()
    s = s.replace("×", "X").replace("*", "X").replace("−", "-").replace("—", "-").replace("–", "-")
    s = re.sub(r"\s+", "", s)
    # Check plate notation BEFORE angle steel. Material grade is not geometry.
    if re.search(r"Q\d{3}[A-E]?-\d", s) or re.match(r"^-\d+(?:\.\d+)?X\d", s) or "钢板" in s or s.startswith(("PL", "Δ")):
        dimensions = re.search(r"(?:Q\d{3}[A-E]?)?-(\d+(?:\.\d+)?)(?:X(\d+(?:\.\d+)?))?", s)
        result = {"kind": "plate", "normalized": s}
        if dimensions:
            result['thickness_mm'] = float(dimensions[1])
            if dimensions[2]:result['width_mm'] = float(dimensions[2])
        return result
    angle = re.search(r"(?:Q\d{3}[A-E]?)?L(\d+(?:\.\d+)?)X(\d+(?:\.\d+)?)(?:X(\d+(?:\.\d+)?))?", s)
    if angle:
        a, b, t = angle.groups()
        legs = [float(a), float(b) if t else float(a)]
        thickness = float(t or b)
        if 0 < thickness < min(legs):
            return {"kind": "angle", "legs_mm": legs, "thickness_mm": thickness, "normalized": s}
    if re.match(r"^M\d+(?:X\d+)?$", s) or any(token in s for token in ("螺栓", "螺母", "垫圈", "垫片", "焊", "BOLT", "NUT")):
        return {"kind": "hardware", "normalized": s}
    if re.search(r"[ØΦ]\d+(?:\.\d+)?/[ØΦ]\d", s):
        return {"kind": "round_stock", "normalized": s}
    return {"kind": "unknown", "normalized": s}
