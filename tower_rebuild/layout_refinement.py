"""Use connected ink components to keep proposed face crops from cutting rods.

This only refines image regions. It never infers a connection or member mark.
"""
import fitz
import numpy as np
from scipy import ndimage
from scipy.optimize import linear_sum_assignment


def refine_face_regions(pdf, regions):
    changes = []
    try:
        doc = fitz.open(pdf)
    except (fitz.FileDataError, RuntimeError):
        return changes
    with doc:
        for page_number, page in enumerate(doc, 1):
            targets = [r for r in regions if r.evidence.page == page_number and r.kind in {"front", "face"}]
            if not targets or page.rotation:
                continue
            pix = page.get_pixmap(matrix=fitz.Matrix(1600/page.rect.width, 1600/page.rect.width), alpha=False)
            pixels = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
            mask = pixels[:, :, :3].min(axis=2) < 120
            labels, _ = ndimage.label(ndimage.binary_dilation(mask, iterations=2))
            candidates = []
            for slices in ndimage.find_objects(labels):
                if slices is None:
                    continue
                y, x = slices
                w, h = x.stop-x.start, y.stop-y.start
                if w > 70 and h > 65 and w < pix.width*.8 and h < pix.height*.8:
                    candidates.append(np.array([x.start/pix.width, y.start/pix.height, x.stop/pix.width, y.stop/pix.height]))
            if not candidates:
                continue
            # Match larger views before narrow sections. A global assignment
            # prevents two proposals from snapping to the same physical drawing.
            cost = np.zeros((len(targets), len(candidates)))
            for i, target in enumerate(targets):
                box = np.array(target.evidence.bbox)
                for j, candidate in enumerate(candidates):
                    center_error = np.linalg.norm((box[:2]+box[2:]-candidate[:2]-candidate[2:])/2)
                    size_error = np.linalg.norm((box[2:]-box[:2])-(candidate[2:]-candidate[:2]))
                    cost[i, j] = center_error + .25*size_error
            ii, jj = linear_sum_assignment(cost)
            for i, j in zip(ii, jj):
                if cost[i, j] > .23:
                    continue
                target, candidate = targets[i], candidates[j]
                old = list(target.evidence.bbox)
                # Keep nearby member callouts and dimension extensions around ink.
                expanded = np.clip(candidate + [-.012, -.012, .012, .016], 0, 1)
                target.evidence.bbox = tuple(float(v) for v in expanded)
                changes.append({"region": target.id, "before": old, "after": list(target.evidence.bbox),
                                "method": "connected ink component; geometry still requires review"})
    return changes
