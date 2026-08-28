"""Repair checkbox field ownership before comparison using expected Excel options."""
from __future__ import annotations
from copy import deepcopy
from rapidfuzz import fuzz
from utils.text_utils import clean_text, norm

def _opts(rule): return [clean_text(x) for x in (getattr(rule,"expected_options",None) or []) if clean_text(x)]
def _actual(value):
    return {norm(x.rsplit("-",1)[0]) for x in str(value or "").split(";") if x.strip()}


def _normalise_regrouped(regrouped):
    """Return regroup output as [{option, selected, bbox}, ...].

    Supported inputs:
      * list[dict]
      * list[str], e.g. "Yes-selected"
      * list[tuple], e.g. ("Yes", True)
      * dict[label] = bool/dict/string
      * a single string
    Invalid entries are skipped rather than breaking the full comparison.
    """
    if regrouped is None:
        return []

    if isinstance(regrouped, dict):
        # Geometry/LLM regroupers may return a wrapper object such as
        # {options: [...], matched: 5, expected: 7, confidence: .9}. Only the
        # options array is data. Wrapper properties must never become labels.
        if isinstance(regrouped.get("options"), (list, tuple)):
            items = list(regrouped["options"])
        elif any(k in regrouped for k in ("option", "label", "key", "name")):
            items = [regrouped]
        else:
            return []
    elif isinstance(regrouped, (list, tuple, set)):
        items = list(regrouped)
    else:
        items = [regrouped]

    output = []
    for item in items:
        option = ""
        selected = False
        bbox = None

        if isinstance(item, dict):
            option = (
                item.get("option")
                or item.get("label")
                or item.get("key")
                or item.get("name")
                or ""
            )
            raw_selected = item.get("selected", item.get("state", False))
            bbox = item.get("bbox") or item.get("label_bbox")

        elif isinstance(item, (list, tuple)):
            if item:
                option = item[0]
            raw_selected = item[1] if len(item) > 1 else False
            bbox = item[2] if len(item) > 2 else None

        elif isinstance(item, str):
            value = item.strip()
            lowered = value.lower()
            raw_selected = False
            # Parse suffix carefully: unselected contains the word selected.
            for suffix in (":unselected", "-unselected", " unselected"):
                if lowered.endswith(suffix):
                    option = value[: -len(suffix)].strip(" :-")
                    raw_selected = False
                    break
            else:
                for suffix in (":selected", "-selected", " selected"):
                    if lowered.endswith(suffix):
                        option = value[: -len(suffix)].strip(" :-")
                        raw_selected = True
                        break
                else:
                    option = value
        else:
            continue

        option = str(option or "").strip(" :-")
        if not option:
            continue

        if isinstance(raw_selected, str):
            state = raw_selected.strip().lower().strip(":")
            selected = state in {"selected", "true", "1", "yes", "x", "checked"}
        else:
            selected = bool(raw_selected)

        output.append({"option": option, "selected": selected, "bbox": bbox})

    return output


def repair_checkbox_fields(rules, fields, key_value_pairs):
    repaired=[deepcopy(f) for f in (fields or [])]
    for rule in rules:
        expected=_opts(rule)
        if not expected: continue
        expected_set={norm(x) for x in expected}
        candidates=[]
        for field in repaired:
            if field.get("kind")!="checkbox_group": continue
            label_score=fuzz.token_set_ratio(norm(rule.item or ""),norm(field.get("key") or ""))
            actual=_actual(field.get("value"))
            overlap=len(expected_set & actual)/max(1,len(expected_set)) if actual else 0
            section=20 if not rule.section or norm(rule.section)==norm(field.get("section") or "") else 0
            candidates.append((label_score+35*overlap+section,field,actual))
        if not candidates: continue
        candidates.sort(key=lambda x:x[0],reverse=True)
        _,field,actual=candidates[0]
        raw=field.get("raw") if isinstance(field.get("raw"),dict) else {}
        label_bbox=field.get("label_bbox") or raw.get("label_bbox") or field.get("bbox") or raw.get("bbox")
        regrouped=None
        if isinstance(label_bbox,(list,tuple)) and len(label_bbox)==4 and key_value_pairs:
            try:
                from modules.checkbox_geometry import regroup
                regrouped=regroup(expected,label_bbox,field.get("page",1),key_value_pairs)
            except Exception: regrouped=None
        normalised_regrouped = _normalise_regrouped(regrouped)
        if normalised_regrouped:
            field.setdefault("raw", {})["precomparison_original_value"] = field.get("value")
            field["value"] = ";".join(
                f"{x['option']}-{'selected' if x['selected'] else 'unselected'}"
                for x in normalised_regrouped
            )
            field["children"] = normalised_regrouped
            field["raw"]["checkbox_repair_source"] = "EXPECTED_OPTIONS_AND_GEOMETRY"
            field["raw"]["checkbox_repair_expected_options"] = expected
            field["raw"]["checkbox_repair_output_count"] = len(normalised_regrouped)
            labels = {norm(x.get("option") or "") for x in normalised_regrouped}
            forbidden = {"OPTIONS", "MATCHED", "EXPECTED", "CONFIDENCE", "SCORE", "REASON", "RESULT"}
            field["raw"]["checkbox_payload_valid"] = not bool(labels & forbidden)
        elif actual and not (expected_set & actual):
            # Prevent a fully disjoint group from masquerading as the rule.
            field.setdefault("raw",{})["checkbox_option_conflict"]=True
    return repaired
