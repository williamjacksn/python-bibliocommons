import dataclasses
import datetime
import json
import urllib.parse

import httpx
import lxml.html


def _translate_medium(medium: str) -> str:
    return {
        "BK": "book",
        "EAUDIOBOOK": "e-audiobook",
        "EBOOK": "e-book",
        "GRAPHIC_NOVEL": "graphic-novel",
    }.get(medium, medium)


@dataclasses.dataclass
class LibraryLoan:
    item_id: str
    title: str
    subtitle: str
    medium: str
    due: datetime.date
    renewable: bool


@dataclasses.dataclass
class SearchResult:
    """A single catalog search result."""
    bib_id: str
    title: str
    author: str
    format: str
    publication_date: str
    call_number: str
    content_type: str
    available_copies: int
    total_copies: int


@dataclasses.dataclass
class BranchItem:
    """A single copy of a title at a specific branch."""
    branch_name: str
    branch_code: str
    collection: str
    call_number: str
    status: str
    library_status: str


def _extract_bibs_json(html: str) -> dict:
    """Extract the bibs JSON blob from a BiblioCommons search page.

    The v2 search page embeds all results in a React preloaded state.
    We find the "bibs" object by bracket-counting from the marker.
    """
    start = html.find('"entities":{')
    if start == -1:
        return {}

    pos = html.find('"bibs":{', start)
    if pos == -1:
        return {}

    pos = html.index('{', pos)

    depth = 0
    in_string = False
    escape = False

    for i in range(pos, len(html)):
        ch = html[i]
        if escape:
            escape = False
            continue
        if ch == '\\':
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == '{':
            depth += 1
        if ch == '}':
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[pos:i + 1])
                except json.JSONDecodeError:
                    return {}
    return {}


class BiblioCommonsClient:
    account_id: int

    def __init__(self, library_subdomain: str) -> None:
        self.library_subdomain = library_subdomain
        self.httpx_client = httpx.Client()

    def authenticate(self, username: str, password: str) -> None:
        login_url = f"https://{self.library_subdomain}.bibliocommons.com/user/login"
        login_params = dict(destination="x")
        login_page = self.httpx_client.get(login_url, params=login_params)
        login_page.raise_for_status()
        login_doc = lxml.html.document_fromstring(login_page.content)
        auth_token_el = login_doc.cssselect('input[name="authenticity_token"]')[0]
        auth_token = auth_token_el.value
        data = {
            "authenticity_token": auth_token,
            "name": username,
            "user_pin": password,
        }
        login_action = self.httpx_client.post(
            login_url, data=data, follow_redirects=True
        )
        login_action.raise_for_status()

        # After SSO redirect, multiple cookies with the same name may exist
        # across domains (bibliocommons.com, chipublib.bibliocommons.com,
        # www.chipublib.org). Use jar.get() to pick the first non-empty value.
        access_token = None
        session_id = None
        for cookie in self.httpx_client.cookies.jar:
            if cookie.name == "bc_access_token" and cookie.value:
                access_token = cookie.value
            elif cookie.name == "session_id" and cookie.value:
                session_id = cookie.value

        if not access_token:
            raise RuntimeError("Authentication failed: no bc_access_token cookie")
        if not session_id:
            raise RuntimeError("Authentication failed: no session_id cookie")

        self.httpx_client.headers.update(
            {
                "X-Access-Token": access_token,
                "X-Session-Id": session_id,
            }
        )
        self.account_id = int(session_id.split("-")[-1]) + 1

    def get_checkouts(self) -> dict:
        checkouts_url = f"https://gateway.bibliocommons.com/v2/libraries/{self.library_subdomain}/checkouts"
        params = dict(accountId=self.account_id)
        checkouts = self.httpx_client.get(checkouts_url, params=params)
        checkouts.raise_for_status()
        response = checkouts.json()
        return response

    @property
    def loans(self) -> list[LibraryLoan]:
        result = []
        data = self.get_checkouts()
        for item in data.get("entities", {}).get("checkouts", {}).values():
            item_id = item.get("checkoutId")
            bib = (
                data.get("entities", {}).get("bibs", {}).get(item.get("metadataId"), {})
            )
            medium = _translate_medium(bib.get("briefInfo").get("format"))
            title = bib.get("briefInfo").get("title")
            subtitle = bib.get("briefInfo").get("subtitle")
            due = datetime.date.fromisoformat(item.get("dueDate"))
            result.append(
                LibraryLoan(
                    item_id=item_id,
                    title=title,
                    subtitle=subtitle,
                    medium=medium,
                    due=due,
                    renewable=False,
                )
            )
        return result

    # ------------------------------------------------------------------
    # Search — no authentication required
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        search_type: str = "smart",
        page: int = 1,
    ) -> list[SearchResult]:
        """Search the library catalog. No authentication required.

        Args:
            query: Search query string.
            search_type: Search type (smart, title, author, subject, etc.).
            page: Page number (25 results per page).

        Returns:
            List of SearchResult dataclasses.
        """
        search_url = (
            f"https://{self.library_subdomain}.bibliocommons.com/v2/search"
        )
        params = {
            "query": urllib.parse.quote(query),
            "searchType": search_type,
            "page": str(page),
        }
        query_string = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{search_url}?{query_string}"

        response = self.httpx_client.get(url)
        response.raise_for_status()
        bibs = _extract_bibs_json(response.text)

        results: list[SearchResult] = []
        for bib_id, bib in bibs.items():
            info = bib.get("briefInfo", {})
            avail = bib.get("availability", {})
            results.append(SearchResult(
                bib_id=bib_id,
                title=info.get("title", ""),
                author=(info.get("authors") or [""])[0],
                format=info.get("format", ""),
                publication_date=info.get("publicationDate", ""),
                call_number=info.get("callNumber", ""),
                content_type=info.get("contentType", ""),
                available_copies=avail.get("availableCopies", 0),
                total_copies=avail.get("totalCopies", 0),
            ))
        return results

    # ------------------------------------------------------------------
    # Availability — requires authentication
    # ------------------------------------------------------------------

    def get_availability(
        self,
        bib_id: str,
        *,
        branch_filter: str | None = None,
    ) -> list[BranchItem]:
        """Get branch-level availability for a title.

        Requires authentication (call authenticate() first).

        Args:
            bib_id: The BiblioCommons metadata ID (e.g. "S126C1872927").
            branch_filter: If set, only return items at branches whose
                name contains this string (case-insensitive).

        Returns:
            List of BranchItem dataclasses, one per physical copy.
        """
        url = (
            f"https://gateway.bibliocommons.com/v2/libraries/"
            f"{self.library_subdomain}/bibs/{bib_id}/availability"
            f"?locale=en-US"
        )
        response = self.httpx_client.get(url, headers={"Accept": "application/json"})
        response.raise_for_status()
        data = response.json()

        bib_items = data.get("entities", {}).get("bibItems", {})
        items: list[BranchItem] = []

        for _item_id, item in bib_items.items():
            branch = item.get("branch", {})
            avail = item.get("availability", {})
            branch_name = branch.get("name", "Unknown")

            if branch_filter is None or branch_filter.lower() in branch_name.lower():
                items.append(BranchItem(
                    branch_name=branch_name,
                    branch_code=branch.get("code", ""),
                    collection=item.get("collection", ""),
                    call_number=item.get("callNumber", ""),
                    status=avail.get("status", ""),
                    library_status=avail.get("libraryStatus", ""),
                ))
        return items
