from printiq_core.models import *
def flatten(v): return ' '.join(str(x) for x in v.values() if x) if isinstance(v,dict) else str(v or '').strip()
def kind_rule(r):
    if r.expected_kind=='checkbox_group' or r.rule_type=='CHECKBOX': return FieldKind.CHECKBOX_GROUP
    if r.expected_kind=='date' or r.rule_type=='DATE_FORMAT': return FieldKind.DATE
    if r.rule_type=='STATIC_TEXT': return FieldKind.STATIC_TEXT
    return FieldKind.TEXT
def adapt_rules(rules):
    out=[]
    for r in rules:
        if r.rule_type=='NO_PRINT_RULE' or not r.item: continue
        original=(r.raw or {}).get('_original',{})
        out.append(CanonicalRule(id=r.id,item_name=r.item,section=r.section,subsection=r.subsection,field_kind=kind_rule(r),document_order_index=len(out),instruction_original=original.get('instruction',r.instruction or ''),instruction_normalized=r.instruction or '',expected_options=list(r.expected_options or [])))
    return out
def _box(field,page):
    raw=field.get('raw') if isinstance(field.get('raw'),dict) else {}
    candidates=[field.get('label_bbox'),raw.get('label_bbox'),field.get('bbox'),raw.get('bbox'),field.get('value_bbox'),raw.get('value_bbox')]
    for v in candidates:
        if isinstance(v,(list,tuple)) and len(v)==4:
            return BBox(page=page,x0=float(v[0]),y0=float(v[1]),x1=float(v[2]),y1=float(v[3]))
    # Geometry is optional for matching. Keep the field instead of dropping it.
    return BBox(page=page)
def adapt_fields(fields):
    out=[]
    for i,f in enumerate(fields,1):
        if not isinstance(f,dict): continue
        page=int(f.get('page') or 1)
        out.append(LogicalPdfField(id=f'PDF-{i:04d}',kind=FieldKind.CHECKBOX_GROUP if f.get('kind')=='checkbox_group' else FieldKind.TEXT,page=page,label_text=str(f.get('key') or ''),value_text=flatten(f.get('value')),section=f.get('section'),subsection=f.get('subsection'),label_bbox=_box(f,page),raw=f))
    return out
