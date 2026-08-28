from models.rule_models import PrintRule
from modules.field_grouping import collect_group, merge_group
from modules.rule_classifier import classify_rule


def _f(order, key, value, x0):
    return {"order": order, "kind": "text", "key": key, "value": value,
            "page": 1, "bbox": [x0, .20, x0 + .10, .21],
            "section": "LICENSE - PARTY A", "subsection": "GROOM/SPOUSE",
            "raw": {"bbox": [x0, .20, x0 + .10, .21]}}


def test_rule_driven_composite_gets_full_logical_key():
    fields=[_f(1,"CURRENT NAME- First","ALICE",.10),_f(2,"Middle","B",.25),
            _f(3,"Last","SMITH",.40),_f(4,"Suffix","",.55)]
    group=collect_group(fields[0],["First","Middle","Last","Suffix"],fields,set())
    merged=merge_group(group,["First","Middle","Last","Suffix"],[""," "," "," "],
                       canonical_key="CURRENT NAME - First, Middle, Last, Suffix")
    assert merged["key"] == "CURRENT NAME - First, Middle, Last, Suffix"
    assert merged["value"] == "ALICE B SMITH"
    assert merged["_candidate_reconstruction_source"] == "WORKBOOK_PARTS_AND_SAME_LINE_GEOMETRY"
    assert merged["_composite_complete"] is True
    assert len(merged["_group_orders"]) == 4


def test_single_anchor_is_not_relabelled_as_complete_composite():
    field=_f(1,"CURRENT NAME- First","ALICE",.10)
    merged=merge_group([field],["First","Middle","Last","Suffix"],canonical_key="CURRENT NAME - First, Middle, Last, Suffix")
    assert merged["key"] == "CURRENT NAME- First"
    assert merged["_composite_complete"] is False


def test_applicant_name_parts_override_accidental_date_signal():
    r=PrintRule(id="r",sheet="s",row_index=1,item="Applicant 1 Name:",
        instruction="PRINT <Party A Current Name> FORMAT: <First> + <Middle> + <Last> + <Suffix>",
        example="LINDA JUNE LEE 2026", expected_kind="date",
        part_labels=["First","Middle","Last","Suffix"])
    assert classify_rule(r).rule_type == "FIELD_TEXT"


def test_date_part_schema_can_still_be_date():
    r=PrintRule(id="r",sheet="s",row_index=1,item="DATE OF EVENT",
        instruction="FORMAT: <month> <dd> <yyyy>", example="APRIL 27, 2026",
        expected_kind="date", part_labels=["month","dd","yyyy"])
    assert classify_rule(r).rule_type == "DATE_FORMAT"
