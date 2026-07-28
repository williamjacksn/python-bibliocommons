"""Tests for the BiblioCommonsClient."""

from __future__ import annotations

import json
from http.cookiejar import Cookie

import httpx
import pytest

from bibliocommons import BiblioCommonsClient, BranchItem, SearchResult


@pytest.fixture
def client() -> BiblioCommonsClient:
    """Return a fresh client for the Seattle Public Library."""
    return BiblioCommonsClient("seattle")


def _make_cookie(name: str, value: str, domain: str) -> Cookie:
    """Build a CookieJar-compatible cookie."""
    return Cookie(
        version=1,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=True,
        domain_initial_dot=domain.startswith("."),
        path="/",
        path_specified=True,
        secure=True,
        expires=None,
        discard=False,
        comment=None,
        comment_url=None,
        rest={},
        rfc2109=False,
    )


def _fake_login_page(request: httpx.Request) -> httpx.Response:
    """Return a minimal login page with a CSRF token."""
    return httpx.Response(
        200,
        text=(
            "<html><body>"
            '<form action="/user/login" method="post">'
            '<input name="authenticity_token" value="abc123"/>'
            "</form></body></html>"
        ),
        request=request,
    )


def _fake_login_post_response(request: httpx.Request) -> httpx.Response:
    """Return a 302 redirect to the SSO page."""
    return httpx.Response(
        302,
        headers={"location": "https://seattle.bibliocommons.com/sso/web?token=xyz"},
        request=request,
    )


def _fake_sso_response(request: httpx.Request) -> httpx.Response:
    """Return a successful SSO landing page."""
    return httpx.Response(
        200, text="<html><body>logged in</body></html>", request=request
    )


def test_search_parses_embedded_bibs(client: BiblioCommonsClient) -> None:
    """Search extracts bibs from the embedded React state on the search page."""
    # Build a minimal embedded state with one bib
    bibs = {
        "S1": {
            "briefInfo": {
                "title": "1984",
                "authors": ["Orwell, George"],
                "format": "PAPERBACK",
                "publicationDate": "1977",
                "callNumber": "FIC ORWELL",
                "contentType": "BK",
            },
            "availability": {
                "availableCopies": 7,
                "totalCopies": 67,
            },
        }
    }
    # The real v2 page embeds minified JSON; match that shape exactly.
    embedded = json.dumps({"entities": {"bibs": bibs}}, separators=(",", ":"))
    html = (
        f"<html><body><script>window.__INITIAL_STATE__ = {embedded}"
        f"</script></body></html>"
    )

    client.httpx_client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=html))
    )

    results = client.search("1984")

    assert len(results) == 1
    assert results[0] == SearchResult(
        bib_id="S1",
        title="1984",
        author="Orwell, George",
        format="PAPERBACK",
        publication_date="1977",
        call_number="FIC ORWELL",
        content_type="BK",
        available_copies=7,
        total_copies=67,
    )


def test_search_returns_empty_when_no_entities(client: BiblioCommonsClient) -> None:
    """Search gracefully handles pages without embedded bibs."""
    client.httpx_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text="<html><body></body></html>")
        )
    )
    results = client.search("nonexistent")
    assert results == []


def test_authenticate_handles_duplicate_cookies(client: BiblioCommonsClient) -> None:
    """Auth succeeds when the SSO chain sets duplicate cookies across domains."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/user/login" in url and request.method == "GET":
            return _fake_login_page(request)
        if "/user/login" in url and request.method == "POST":
            # Simulate cookies set on multiple domains by writing them
            # directly into the shared client jar. This mirrors what httpx
            # does after following the SSO redirect chain.
            client.httpx_client.cookies.jar.set_cookie(
                _make_cookie("bc_access_token", "tok1", ".bibliocommons.com")
            )
            client.httpx_client.cookies.jar.set_cookie(
                _make_cookie("bc_access_token", "", "seattle.bibliocommons.com")
            )
            client.httpx_client.cookies.jar.set_cookie(
                _make_cookie("session_id", "sess-12345-999", ".bibliocommons.com")
            )
            client.httpx_client.cookies.jar.set_cookie(
                _make_cookie("session_id", "old", "seattle.bibliocommons.com")
            )
            return _fake_login_post_response(request)
        if "/sso/web" in url:
            # SSO sets another token on the library domain; empty value should
            # be ignored in favor of the earlier non-empty .bibliocommons.com
            # token.
            client.httpx_client.cookies.jar.set_cookie(
                _make_cookie("bc_access_token", "tok2", "seattle.bibliocommons.com")
            )
            return _fake_sso_response(request)
        return httpx.Response(404, request=request)

    client.httpx_client = httpx.Client(transport=httpx.MockTransport(handler))
    client.authenticate("card123", "pin456")

    assert client.account_id == 1000
    assert client.httpx_client.headers["X-Access-Token"] == "tok1"
    assert client.httpx_client.headers["X-Session-Id"] == "sess-12345-999"


def test_authenticate_raises_when_missing_access_token(
    client: BiblioCommonsClient,
) -> None:
    """Auth raises a clear error if the access token cookie is missing."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "GET":
            return _fake_login_page(request)
        if "/user/login" in url and request.method == "POST":
            # Simulate receiving only a session_id cookie after login.
            client.httpx_client.cookies.jar.set_cookie(
                _make_cookie("session_id", "sess-1-2", ".bibliocommons.com")
            )
            return httpx.Response(
                302,
                headers={"location": "https://seattle.bibliocommons.com/"},
                request=request,
            )
        return httpx.Response(404, request=request)

    client.httpx_client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(RuntimeError, match="no bc_access_token cookie"):
        client.authenticate("card", "pin")


def test_get_availability_parses_branch_items(client: BiblioCommonsClient) -> None:
    """Availability returns one BranchItem per physical copy."""
    payload = {
        "entities": {
            "bibItems": {
                "item1": {
                    "branch": {"name": "Northtown", "code": "56"},
                    "collection": "Adult",
                    "callNumber": "FIC ORWELL",
                    "availability": {
                        "status": "AVAILABLE",
                        "libraryStatus": "Available",
                    },
                },
                "item2": {
                    "branch": {"name": "Northtown", "code": "56"},
                    "collection": "Adult",
                    "callNumber": "FIC ORWELL",
                    "availability": {
                        "status": "UNAVAILABLE",
                        "libraryStatus": "Checked Out",
                    },
                },
            }
        }
    }

    client.httpx_client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    )
    client.httpx_client.headers["X-Access-Token"] = "tok"
    client.httpx_client.headers["X-Session-Id"] = "sess"

    items = client.get_availability("S1")

    assert len(items) == 2
    assert items[1] == BranchItem(
        branch_name="Northtown",
        branch_code="56",
        collection="Adult",
        call_number="FIC ORWELL",
        status="UNAVAILABLE",
        library_status="Checked Out",
    )


def test_get_availability_filters_by_branch(client: BiblioCommonsClient) -> None:
    """The branch_filter option limits results to matching branches."""
    payload = {
        "entities": {
            "bibItems": {
                "item1": {
                    "branch": {"name": "Northtown", "code": "56"},
                    "collection": "Adult",
                    "callNumber": "FIC ORWELL",
                    "availability": {
                        "status": "AVAILABLE",
                        "libraryStatus": "Available",
                    },
                },
                "item2": {
                    "branch": {"name": "Lake City", "code": "LCY"},
                    "collection": "Adult",
                    "callNumber": "FIC ORWELL",
                    "availability": {
                        "status": "AVAILABLE",
                        "libraryStatus": "Available",
                    },
                },
            }
        }
    }

    client.httpx_client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    )
    client.httpx_client.headers["X-Access-Token"] = "tok"
    client.httpx_client.headers["X-Session-Id"] = "sess"

    items = client.get_availability("S1", branch_filter="Northtown")
    assert len(items) == 1
    assert items[0].branch_name == "Northtown"


def test_search_gateway_delegates_to_gateway_api(client: BiblioCommonsClient) -> None:
    """search_gateway hits the gateway /bibs/search endpoint."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"entities": {"bibs": {}}})

    client.httpx_client = httpx.Client(transport=httpx.MockTransport(handler))
    client.httpx_client.headers["X-Access-Token"] = "tok"
    client.httpx_client.headers["X-Session-Id"] = "sess"

    client.search_gateway("weezer", format="MUSIC_CD", page=2, sort_by="title")

    assert captured["url"].startswith(
        "https://gateway.bibliocommons.com/v2/libraries/seattle/bibs/search"
    )
    assert captured["params"]["query"] == "weezer"
    assert captured["params"]["searchType"] == "keyword"
    assert captured["params"]["f_FORMAT"] == "MUSIC_CD"
    assert captured["params"]["page"] == "2"
    assert captured["params"]["sortBy"] == "title"


def test_get_availability_raw_returns_full_json(client: BiblioCommonsClient) -> None:
    """get_availability_raw returns the unprocessed gateway response."""
    payload = {"availability": {"totalCopies": 10}, "entities": {"bibItems": {}}}

    client.httpx_client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    )
    client.httpx_client.headers["X-Access-Token"] = "tok"
    client.httpx_client.headers["X-Session-Id"] = "sess"

    result = client.get_availability_raw("S1")
    assert result == payload
