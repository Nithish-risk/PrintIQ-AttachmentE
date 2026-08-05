def polygon_to_bbox(poly):
    if not poly:
        return [0, 0, 0, 0]
    xs, ys = [], []
    # Case 1: flat list of numbers [x1, y1, x2, y2, ...] — the format used by the
    # newer azure-ai-documentintelligence SDK (DocumentWord/Line/SelectionMark.polygon).
    if all(isinstance(v, (int, float)) for v in poly):
        if len(poly) < 2:
            return [0, 0, 0, 0]
        xs = [float(v) for v in poly[0::2]]
        ys = [float(v) for v in poly[1::2]]
    else:
        # Case 2: list of point objects / dicts exposing x / y (older SDK style).
        for p in poly:
            if isinstance(p, dict):
                xs.append(float(p.get("x", 0))); ys.append(float(p.get("y", 0)))
            else:
                xs.append(float(getattr(p, "x", 0))); ys.append(float(getattr(p, "y", 0)))
    if not xs or not ys:
        return [0, 0, 0, 0]
    return [min(xs), min(ys), max(xs), max(ys)]

def normalize_bbox(bbox, width, height):
    if not width or not height:
        return bbox
    x0,y0,x1,y1 = bbox
    return [x0/width, y0/height, x1/width, y1/height]

def denormalize_bbox(bbox, width, height):
    x0,y0,x1,y1 = bbox
    return [x0*width, y0*height, x1*width, y1*height]

def bbox_center(b):
    return [(b[0]+b[2])/2, (b[1]+b[3])/2]

def center_inside(inner, outer, tol=0):
    cx,cy = bbox_center(inner)
    return outer[0]-tol <= cx <= outer[2]+tol and outer[1]-tol <= cy <= outer[3]+tol

def overlap_ratio(a,b):
    x0=max(a[0],b[0]); y0=max(a[1],b[1]); x1=min(a[2],b[2]); y1=min(a[3],b[3])
    inter=max(0,x1-x0)*max(0,y1-y0)
    area=max(1e-9,(a[2]-a[0])*(a[3]-a[1]))
    return inter/area
