import pytest

from wbscraper.dates import to_iso
from wbscraper.redact import redact
from wbscraper.textutil import clean_text, is_deleted, looks_like_bot, text_hash


def test_redacts_contact_details_but_keeps_meaning():
    t = ("Email jane.doe@example.com or call (916) 555-1234 or 916-555-1234. Thanks u/some_guy and @bob_w, "
         "see r/Wastewater and https://www.linkedin.com/in/jane-doe. I live at 123 Oak Street.")
    out = redact(t)
    assert "jane.doe@example.com" not in out and "555-1234" not in out
    assert "some_guy" not in out and "bob_w" not in out and "jane-doe" not in out and "Oak Street" not in out
    assert "r/Wastewater" in out
    assert "[REDACTED_EMAIL]" in out and "[REDACTED_PHONE]" in out


def test_does_not_mangle_ids_or_agency_addresses():
    out = redact("Global ID T10000012345, WDID 5S01C123456, epoch 1700000000, office at 1001 I Street")
    assert "T10000012345" in out and "5S01C123456" in out and "1700000000" in out and "1001 I Street" in out


def test_redaction_can_be_disabled_per_class():
    assert "a@b.com" in redact("a@b.com", emails=False)


@pytest.mark.parametrize("value,expected", [
    (1700000000, "2023-11-14T22:13:20Z"),
    (1700000000000, "2023-11-14T22:13:20Z"),
    ("1700000000", "2023-11-14T22:13:20Z"),
    ("2024-03-05T14:22:10+00:00", "2024-03-05T14:22:10Z"),
    ("2024-03-05T14:22:10-08:00", "2024-03-05T22:22:10Z"),
    ("Tue, 05 Mar 2024 14:22:10 GMT", "2024-03-05T14:22:10Z"),
])
def test_dates_are_normalised_to_iso_utc(value, expected):
    assert to_iso(value, now=1_900_000_000) == expected


@pytest.mark.parametrize("value", [None, "", "not a date", 0, 100, "1970-01-01", 99999999999999999999, 4_000_000_000])
def test_missing_or_implausible_dates_become_none(value):
    assert to_iso(value, now=1_900_000_000) is None


def test_text_helpers():
    assert clean_text("a\r\nb\x00c ") == "a\nbc"
    assert is_deleted("[removed]") and is_deleted(" [deleted] ")
    assert looks_like_bot("I am a bot, and this action was performed automatically.")
    assert text_hash("Hello, World!") == text_hash("hello   world")
