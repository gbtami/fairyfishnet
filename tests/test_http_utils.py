import pytest

from fairyfishnet.errors import JsonResponseError
from fairyfishnet.http_utils import (
    base_url,
    is_newer_version,
    release_file_url,
    response_body_snippet,
    response_json,
    version_key,
)


class FakeResponse:
    def __init__(self, payload=None, text="", status_code=200, reason="OK", headers=None):
        self.payload = payload
        self.text = text
        self.status_code = status_code
        self.reason = reason
        self.headers = headers or {}

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def test_base_url_discards_path_and_query():
    assert base_url("https://example.org/fishnet/acquire?x=1") == "https://example.org/"


def test_response_body_snippet_escapes_and_truncates():
    response = FakeResponse(text="line1\r\n" + "x" * 20)
    assert response_body_snippet(response, limit=12) == "line1\\r\\nxxx..."


def test_response_body_snippet_handles_unreadable_body():
    class BrokenResponse:
        @property
        def text(self):
            raise RuntimeError("broken")

    assert response_body_snippet(BrokenResponse()) == "<unreadable response body>"


def test_response_json_returns_payload():
    assert response_json(FakeResponse({"ok": True}), "test") == {"ok": True}


def test_response_json_reports_http_context():
    response = FakeResponse(ValueError("bad"), "not-json", 502, "Bad Gateway", {"Content-Type": "text/plain"})
    with pytest.raises(JsonResponseError, match="test returned invalid JSON.*HTTP 502 Bad Gateway.*not-json"):
        response_json(response, "test")


def test_release_file_url_prefers_wheel():
    files = [
        {"packagetype": "sdist", "url": "source"},
        {"packagetype": "bdist_wheel", "url": "wheel"},
    ]
    assert release_file_url(files) == "wheel"


def test_release_file_url_falls_back_to_first_file():
    assert release_file_url([{"packagetype": "sdist", "url": "source"}]) == "source"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1.16.66", (1, 16, 66)),
        ("2.0rc1", (2, 0)),
        ("1.2.3+local", (1, 2, 3)),
        ("development", ()),
    ],
)
def test_version_key(value, expected):
    assert version_key(value) == expected


@pytest.mark.parametrize(
    ("candidate", "current", "expected"),
    [
        ("1.16.67", "1.16.66", True),
        ("1.17", "1.16.99", True),
        ("1.16.66", "1.16.66", False),
        ("1.16.65", "1.16.66", False),
        ("1.16.66.0", "1.16.66", False),
        ("unknown", "1.16.66", False),
    ],
)
def test_is_newer_version(candidate, current, expected):
    assert is_newer_version(candidate, current) is expected
