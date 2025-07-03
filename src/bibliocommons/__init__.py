import dataclasses
import datetime
import httpx
import lxml.html


@dataclasses.dataclass
class LibraryLoan:
    item_id: str
    title: str
    subtitle: str
    medium: str
    due: datetime.date
    renewable: bool


class BiblioCommonsClient:
    account_id: int

    def __init__(self, library_subdomain: str):
        self.library_subdomain = library_subdomain
        self.httpx_client = httpx.Client()

    def authenticate(self, username: str, password: str):
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
        access_token = self.httpx_client.cookies.get("bc_access_token")
        session_id = self.httpx_client.cookies.get("session_id")
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
            medium = bib.get("briefInfo").get("format")
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
