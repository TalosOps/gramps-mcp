# gramps-mcp - AI-Powered Genealogy Research & Management
# Copyright (C) 2025 cabout.me
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""
Structured date input for every write tool.

Callers may describe a date as
``{"modifier": "range", "quality": "estimated", "start": "1804", "end": "1810"}``
instead of a raw Gramps ``dateval`` array. Both forms are checked so that a
range or span always carries an end date: a range written with only a start
year (e.g. "between 1804 (estimated)") is stored by the API but is malformed.

``sortval`` is never produced here; the server computes it from ``dateval``.
"""

import re
from typing import Any, Dict, List, Union

MODIFIERS = {
    "none": 0,
    "regular": 0,
    "before": 1,
    "after": 2,
    "about": 3,
    "range": 4,
    "span": 5,
    "textonly": 6,
    "from": 7,
    "to": 8,
}
QUALITIES = {"regular": 0, "none": 0, "estimated": 1, "calculated": 2}
COMPOUND_MODIFIERS = {4, 5}  # range ("between A and B"), span ("from A to B")
STRUCTURED_KEYS = {"start", "end"}
_DATE_PART = re.compile(r"^(-?\d{1,4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?$")


def _code(value: Union[str, int, None], table: Dict[str, int], what: str) -> int:
    """Translate a modifier/quality name (or code) into its Gramps code."""
    if value is None:
        return 0
    if isinstance(value, bool):
        raise ValueError(f"Invalid date {what}: {value!r}")
    if isinstance(value, int):
        if value not in table.values():
            raise ValueError(f"Invalid date {what} code: {value}")
        return value
    key = str(value).strip().lower()
    if key not in table:
        raise ValueError(
            f"Invalid date {what} '{value}'. Use one of: {', '.join(sorted(table))}"
        )
    return table[key]


def _parse_part(part: Any, label: str) -> List[Any]:
    """Turn '1804', '1804-03', '1804-03-15', 1804 or {year,month,day} into
    Gramps' [day, month, year, slash] quadruple."""
    if isinstance(part, bool):
        raise ValueError(f"Invalid date {label}: {part!r}")
    if isinstance(part, int):
        year, month, day = part, 0, 0
    elif isinstance(part, dict):
        year = int(part.get("year") or 0)
        month = int(part.get("month") or 0)
        day = int(part.get("day") or 0)
    elif isinstance(part, str):
        match = _DATE_PART.match(part.strip())
        if not match:
            raise ValueError(
                f"Invalid date {label} '{part}'. Use YYYY, YYYY-MM or YYYY-MM-DD"
            )
        year = int(match.group(1))
        month = int(match.group(2) or 0)
        day = int(match.group(3) or 0)
    else:
        raise ValueError(f"Invalid date {label}: {part!r}")
    if not 0 <= month <= 12 or not 0 <= day <= 31:
        raise ValueError(f"Invalid date {label}: month {month}, day {day}")
    if day and not month:
        raise ValueError(f"Invalid date {label}: a day needs a month")
    if not year:
        raise ValueError(f"Invalid date {label}: a year is required")
    return [day, month, year, False]


def is_structured(value: Any) -> bool:
    """True if a date dict uses the structured form rather than raw dateval."""
    if not isinstance(value, dict):
        return False
    named = any(isinstance(value.get(k), str) for k in ("modifier", "quality"))
    return bool(STRUCTURED_KEYS & set(value)) or named


def structured_to_gramps(value: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert a structured date to a Gramps Date dict.

    Args:
        value (Dict[str, Any]): ``modifier``, ``quality``, ``start``, ``end``
            and optional ``text``.

    Returns:
        Dict[str, Any]: Gramps Date dict without ``sortval``.

    Raises:
        ValueError: On unknown modifiers/qualities, malformed parts, a range or
            span without ``end``, or an ``end`` on a single date.
    """
    unknown = set(value) - {"modifier", "quality", "start", "end", "text"}
    if unknown:
        raise ValueError(f"Unknown date keys: {', '.join(sorted(unknown))}")
    modifier = _code(value.get("modifier"), MODIFIERS, "modifier")
    quality = _code(value.get("quality"), QUALITIES, "quality")
    text = value.get("text") or ""
    if modifier == MODIFIERS["textonly"]:
        if not text:
            raise ValueError("A textonly date needs 'text'")
        return {
            "_class": "Date",
            "modifier": modifier,
            "quality": quality,
            "dateval": [0, 0, 0, False],
            "text": text,
        }
    if value.get("start") in (None, ""):
        raise ValueError("A date needs 'start'")
    dateval = _parse_part(value["start"], "start")
    if modifier in COMPOUND_MODIFIERS:
        if value.get("end") in (None, ""):
            name = "range" if modifier == 4 else "span"
            raise ValueError(
                f"A {name} date needs both 'start' and 'end' "
                "(got only a start; use 'after' or 'about' for an open date)"
            )
        dateval += _parse_part(value["end"], "end")
    elif value.get("end") not in (None, ""):
        raise ValueError("'end' is only allowed with modifier range or span")
    return {
        "_class": "Date",
        "modifier": modifier,
        "quality": quality,
        "dateval": dateval,
        "text": text,
    }


def check_raw_date(value: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate a raw Gramps Date dict and strip values the server computes.

    Raises:
        ValueError: If a range/span lacks a complete second date.
    """
    date = {k: v for k, v in value.items() if k not in ("sortval", "year")}
    modifier = date.get("modifier", 0)
    dateval = date.get("dateval") or [0, 0, 0, False]
    if modifier in COMPOUND_MODIFIERS:
        if not isinstance(dateval, list) or len(dateval) < 8 or not dateval[6]:
            raise ValueError(
                "A range/span date needs a second date: dateval must be "
                "[d1, m1, y1, false, d2, m2, y2, false] with y2 set"
            )
    if date:
        date.setdefault("_class", "Date")
    return date


def normalize_date(value: Any) -> Any:
    """Normalize one date value of either form; other values pass through."""
    if is_structured(value):
        return structured_to_gramps(value)
    if isinstance(value, dict):
        return check_raw_date(value)
    return value


def normalize_dates(obj: Any) -> Any:
    """
    Recursively normalize every ``date`` field in a payload.

    Dates sit at many depths (events, citations, names, addresses, media
    refs...), so the whole payload is walked.
    """
    if isinstance(obj, dict):
        return {
            k: normalize_date(v) if k == "date" else normalize_dates(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [normalize_dates(v) for v in obj]
    return obj
