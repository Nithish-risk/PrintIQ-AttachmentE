def assign_reading_order(fields):
    fields.sort(key=lambda f:(f.page,f.label_bbox.y0,f.label_bbox.x0))
    for i,f in enumerate(fields): f.document_order_index=i
    return fields
