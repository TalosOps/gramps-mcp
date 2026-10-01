"""
Pure tests for the GQL parser, local evaluator and server serializer.
"""

import pytest

from src.gramps_mcp.gql import GQLError, matches, parse, to_server, uses_not

NOTE = {
    "gramps_id": "N0743",
    "type": "Research",
    "text": {"string": 'Index: WM. Berry wrote to "Rosa" in 1861.'},
    "tag_list": [],
}


@pytest.mark.parametrize(
    "query,expected",
    [
        ('text ~ "WM."', True),
        ('"WM." in text', True),
        ("text ~ 'WM.'", True),
        ('text ~ "wm. berry"', True),
        ('text ~ "\\"Rosa\\""', True),
        ('text ~ \'"Rosa"\'', True),
        ('text ~ "zzz" or type = Research', True),
        ('not type = "General"', True),
        ('not text ~ "WM."', False),
        ('text !~ "WM."', False),
        ('(text ~ "zzz" or text ~ "1861") and not gramps_id = N0001', True),
        ("gramps_id = N0743 and type != Research", False),
        ("tag_list", False),
    ],
)
def test_local_evaluation(query, expected):
    assert matches(parse(query), NOTE) is expected


def test_list_fan_out_and_index():
    person = {"event_ref_list": [{"role": "Witness"}, {"role": "Primary"}]}
    assert matches(parse('event_ref_list.role = "Primary"'), person)
    assert not matches(parse('event_ref_list[0].role = "Primary"'), person)


def test_numbers_compare_numerically():
    assert matches(parse("gender > 0"), {"gender": 1})
    assert not matches(parse("gender > 1"), {"gender": 1})


def test_server_serialization_quotes_safely():
    assert to_server(parse('text.string ~ "WM."')) == 'text.string ~ "WM."'
    assert to_server(parse('description ~ "a \\"b\\""')) == "description ~ 'a \"b\"'"
    assert to_server(parse("\"x\" in description")) == 'description ~ "x"'


def test_queries_server_cannot_express():
    assert to_server(parse('not type = "Birth"')) is None
    assert to_server(parse('description !~ "x"')) is None
    assert to_server(parse('description ~ "a\\"b\'c"')) is None
    assert uses_not(parse('a = 1 and not b = 2'))


@pytest.mark.parametrize(
    "bad", ['text ~', 'text ~ "x" and', '(text ~ "x"', 'text "x"', 'text ~ "open']
)
def test_malformed_queries_raise(bad):
    with pytest.raises(GQLError):
        parse(bad)


def test_empty_query_matches_everything():
    assert parse("") is None and matches(None, NOTE) and to_server(None) == ""
