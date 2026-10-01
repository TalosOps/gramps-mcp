"""
Pure tests for the edit logic behind update_<type> (no API needed).
"""

import pytest

from src.gramps_mcp.tools.editing import apply_edits, verify_readback

EVENT = {
    "_class": "Event",
    "handle": "evh",
    "gramps_id": "E0010",
    "change": 1,
    "type": "Birth",
    "description": "old",
    "place": "plh",
    "date": {"_class": "Date", "dateval": [1, 2, 1850, False], "sortval": 99},
    "citation_list": ["c1", "c2"],
    "note_list": [],
    "private": False,
}

FAMILY = {
    "_class": "Family",
    "handle": "fh",
    "father_handle": "dad",
    "child_ref_list": [{"_class": "ChildRef", "ref": "k1"}, {"_class": "ChildRef", "ref": "k2"}],
    "event_ref_list": [],
}


def test_remove_ref_from_handle_list():
    new, changed, _ = apply_edits(EVENT, "event", remove_refs={"citation_list": ["c1"]})
    assert new["citation_list"] == ["c2"]
    assert changed == ["citation_list"]
    assert EVENT["citation_list"] == ["c1", "c2"], "input must not be mutated"


def test_remove_ref_object_entry():
    new, _, _ = apply_edits(FAMILY, "family", remove_refs={"child_ref_list": ["k1"]})
    assert [c["ref"] for c in new["child_ref_list"]] == ["k2"]


def test_remove_absent_entry_fails():
    with pytest.raises(ValueError, match="Not in citation_list"):
        apply_edits(EVENT, "event", remove_refs={"citation_list": ["zz"]})


def test_clear_date_place_description():
    new, changed, _ = apply_edits(
        EVENT, "event", clear_fields=["date", "place", "description", "citation_list"]
    )
    assert new["date"] == {"_class": "Date"}
    assert new["place"] == "" and new["description"] == ""
    assert new["citation_list"] == []
    assert set(changed) == {"date", "place", "description", "citation_list"}


def test_set_replaces_list_and_strips_sortval():
    new, _, _ = apply_edits(
        EVENT,
        "event",
        set_fields={
            "citation_list": ["c9"],
            "date": {"dateval": [0, 0, 1851, False], "sortval": 5},
        },
    )
    assert new["citation_list"] == ["c9"]
    assert new["date"] == {"_class": "Date", "dateval": [0, 0, 1851, False]}


def test_add_ref_builds_ref_objects_and_skips_duplicates():
    new, _, notes = apply_edits(
        FAMILY,
        "family",
        add_refs={"child_ref_list": ["k2", "k3"], "event_ref_list": ["e1"]},
    )
    assert new["child_ref_list"][-1] == {"_class": "ChildRef", "ref": "k3"}
    assert new["event_ref_list"] == [{"_class": "EventRef", "ref": "e1", "role": "Family"}]
    assert any("k2 already present" in n for n in notes)


def test_note_text_string_is_wrapped():
    note = {"handle": "n", "text": {"_class": "StyledText", "string": "a", "tags": []}}
    new, _, _ = apply_edits(note, "note", set_fields={"text": "b"})
    assert new["text"] == {"_class": "StyledText", "string": "b", "tags": []}


@pytest.mark.parametrize("field", ["handle", "_class", "change"])
def test_protected_fields(field):
    with pytest.raises(ValueError, match="cannot be edited"):
        apply_edits(EVENT, "event", set_fields={field: "x"})


def test_unknown_field_lists_valid_ones():
    with pytest.raises(ValueError, match="no field 'descripton'.*description"):
        apply_edits(EVENT, "event", set_fields={"descripton": "typo"})


def test_unchanged_value_reports_no_change():
    _, changed, _ = apply_edits(EVENT, "event", set_fields={"description": "old"})
    assert changed == []


def test_verify_readback_tolerates_server_defaults():
    intended = {
        "date": {"_class": "Date"},
        "child_ref_list": [{"_class": "ChildRef", "ref": "k3"}],
        "father_handle": "",
        "type": "Unmarried",
    }
    stored = {
        "date": {"dateval": [0, 0, 0, False], "modifier": 0, "quality": 0, "text": ""},
        "child_ref_list": [{"_class": "ChildRef", "ref": "k3", "frel": {"value": 1}}],
        "father_handle": None,
        "type": {"_class": "FamilyRelType", "string": "Unmarried"},
    }
    assert verify_readback(intended, stored, list(intended)) == []


def test_verify_readback_catches_mismatch():
    intended = {"citation_list": [], "date": {"dateval": [0, 0, 1850, False]}}
    stored = {"citation_list": ["c1"], "date": {"dateval": [0, 0, 0, False]}}
    assert verify_readback(intended, stored, ["citation_list", "date"]) == [
        "citation_list",
        "date",
    ]
