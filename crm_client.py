"""
CRM API client for the Bellhaven assessment sandbox.
Wraps every endpoint we need with clean error handling and automatic pagination.
"""

import logging
import os
import time
from typing import Optional
import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://analyst-assessment-production.up.railway.app/api/v1"
# Never commit the token: set CRM_TOKEN in the shell (or as a GitHub Actions secret).
API_TOKEN = os.environ.get("CRM_TOKEN", "")
if not API_TOKEN:
    raise RuntimeError("CRM_TOKEN is not set. Export your CRM API token as CRM_TOKEN before running.")


def _with_id(account):
    """The API keys accounts by `account_id`; the rest of the code reads `id`."""
    if isinstance(account, dict) and "id" not in account and "account_id" in account:
        account["id"] = account["account_id"]
    return account


class CRMClient:
    def __init__(self, base_url: str = BASE_URL, token: str = API_TOKEN):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def _get(self, path: str, params: dict = None) -> dict:
        url = f"{self.base_url}/{path.lstrip('/')}"
        resp = self.session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, data: dict) -> dict:
        url = f"{self.base_url}/{path.lstrip('/')}"
        resp = self.session.post(url, json=data, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _patch(self, path: str, data: dict) -> dict:
        url = f"{self.base_url}/{path.lstrip('/')}"
        resp = self.session.patch(url, json=data, timeout=30)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------ #
    # Accounts
    # ------------------------------------------------------------------ #

    def get_all_accounts(self, page_size: int = 50) -> list[dict]:
        """Fetch every account, handling pagination.

        API uses: GET /api/v1/accounts?page=N&page_size=N
        Returns a list directly (not wrapped in an object).
        """
        accounts = []
        page = 1
        while True:
            try:
                data = self._get("accounts", params={"page_size": page_size, "page": page})
                # API returns a plain list
                if isinstance(data, list):
                    accounts.extend(data)
                    if len(data) < page_size:
                        break  # last page
                elif isinstance(data, dict):
                    # Handle wrapped responses just in case
                    items = data.get("accounts") or data.get("data") or data.get("results") or []
                    accounts.extend(items)
                    if not items or len(items) < page_size:
                        break
                else:
                    break
                page += 1
                time.sleep(0.15)
            except Exception as e:
                logger.error("Error fetching page %s of accounts: %s", page, e)
                break
        return [_with_id(a) for a in accounts]

    def search_accounts(self, q: str) -> list[dict]:
        """Search accounts by keyword."""
        try:
            data = self._get("accounts", params={"q": q})
            if not isinstance(data, list):
                data = data.get("accounts") or data.get("data") or data.get("results") or []
            return [_with_id(a) for a in data]
        except Exception as e:
            logger.error("Error searching accounts for %r: %s", q, e)
            return []

    def get_account(self, account_id: str) -> Optional[dict]:
        """Fetch a single account by ID."""
        try:
            return _with_id(self._get(f"accounts/{account_id}"))
        except Exception as e:
            logger.error("Error fetching account %s: %s", account_id, e)
            return None

    def create_account(self, data: dict) -> Optional[dict]:
        """Create a new account. Returns the created account dict."""
        try:
            result = _with_id(self._post("accounts", data))
            logger.info("Created account: %s", result.get("id") or result.get("name"))
            return result
        except Exception as e:
            logger.error("Error creating account %r: %s", data.get("name"), e)
            raise

    def update_account(self, account_id: str, data: dict) -> Optional[dict]:
        """Update an existing account with partial data."""
        try:
            result = _with_id(self._patch(f"accounts/{account_id}", data))
            logger.info("Updated account %s: %s", account_id, list(data.keys()))
            return result
        except Exception as e:
            logger.error("Error updating account %s: %s", account_id, e)
            raise
