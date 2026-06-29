"""
jira_client.py
==============
A dedicated module for authenticating with and fetching tasks from
the Jira Cloud REST API.

Authentication uses HTTP Basic Auth with an API token (recommended for Cloud).
The Jira Python library is used as a higher-level abstraction, with a fallback
to raw `requests` calls if needed.
"""

import os
import logging
from typing import Optional
from dotenv import load_dotenv

# --- Try to import the `jira` library; fall back to raw requests ---
try:
    from jira import JIRA, JIRAError
    JIRA_LIB_AVAILABLE = True
except ImportError:
    JIRA_LIB_AVAILABLE = False

import requests
from requests.auth import HTTPBasicAuth

# Load environment variables from .env file
load_dotenv()

# Configure logging for this module
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Configuration – pulled from .env
# ─────────────────────────────────────────────────────────────────────────────
JIRA_SERVER    = os.getenv("JIRA_SERVER", "").rstrip("/")
JIRA_EMAIL     = os.getenv("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN", "")


class JiraClientError(Exception):
    """Raised when a Jira API operation fails."""
    pass


class JiraClient:
    """
    Encapsulates all Jira Cloud REST API operations.

    Preferred path: uses the `jira` Python library for cleaner handling.
    Fallback path:  uses raw `requests` for environments where the library
                    is not installed.
    """

    def __init__(
        self,
        server: Optional[str] = None,
        email: Optional[str] = None,
        api_token: Optional[str] = None,
    ):
        self.server    = server    or JIRA_SERVER
        self.email     = email     or JIRA_EMAIL
        self.api_token = api_token or JIRA_API_TOKEN

        self._validate_config()
        self._client = None  # Lazy-initialised

    # ─────────────────────────────────────────────────────────────────────────
    # Public helpers
    # ─────────────────────────────────────────────────────────────────────────

    def test_connection(self) -> dict:
        """
        Verifies credentials and returns the current user's profile.

        Returns:
            dict: Jira user profile information.
        Raises:
            JiraClientError: On auth failure or connection error.
        """
        try:
            if JIRA_LIB_AVAILABLE:
                client = self._get_client()
                myself = client.myself()
                logger.info("Jira connection OK – user: %s", myself.get("displayName"))
                return myself
            else:
                url  = f"{self.server}/rest/api/3/myself"
                resp = self._raw_get(url)
                logger.info("Jira connection OK – user: %s", resp.get("displayName"))
                return resp
        except Exception as exc:
            raise JiraClientError(f"Connection test failed: {exc}") from exc

    def fetch_issues(self, jql: str, max_results: int = 50) -> list[dict]:
        """
        Executes a JQL query and returns a flat list of task dictionaries
        ready for display in a Streamlit dataframe.

        Args:
            jql:         A valid Jira Query Language string.
            max_results: Upper bound on results returned.

        Returns:
            List of dicts with keys: key, summary, status, priority, assignee,
            reporter, updated, story_points, description.
        Raises:
            JiraClientError: On API or auth errors.
        """
        logger.info("Fetching issues with JQL: %s  (max=%d)", jql, max_results)
        try:
            if JIRA_LIB_AVAILABLE:
                return self._fetch_with_library(jql, max_results)
            else:
                return self._fetch_with_requests(jql, max_results)
        except JiraClientError:
            raise
        except Exception as exc:
            raise JiraClientError(f"Failed to fetch issues: {exc}") from exc

    # ─────────────────────────────────────────────────────────────────────────
    # Private – jira library path
    # ─────────────────────────────────────────────────────────────────────────

    def _get_client(self) -> "JIRA":
        """Lazily initialise and cache the JIRA client object."""
        if self._client is None:
            try:
                self._client = JIRA(
                    server=self.server,
                    basic_auth=(self.email, self.api_token),
                    options={"verify": True},
                )
            except JIRAError as exc:
                raise JiraClientError(
                    f"Could not connect to Jira ({self.server}): {exc.text}"
                ) from exc
        return self._client

    def _fetch_with_library(self, jql: str, max_results: int) -> list[dict]:
        """Uses the `jira` library to execute the query."""
        client = self._get_client()
        try:
            issues = client.search_issues(
                jql_str=jql,
                maxResults=max_results,
                fields=[
                    "summary", "status", "priority", "assignee",
                    "reporter", "updated", "story_points", "description",
                    "issuetype", "labels", "customfield_10016",  # story points field
                ],
            )
        except JIRAError as exc:
            raise JiraClientError(
                f"JQL search failed: {exc.text} (HTTP {exc.status_code})"
            ) from exc

        return [self._normalise_issue_lib(issue) for issue in issues]

    @staticmethod
    def _normalise_issue_lib(issue) -> dict:
        """Convert a jira-library Issue object into a plain dict."""
        fields = issue.fields
        # Story points can live in the standard field or a custom field
        story_pts = (
            getattr(fields, "story_points", None)
            or getattr(fields, "customfield_10016", None)
            or 0
        )
        return {
            "key":          issue.key,
            "summary":      fields.summary or "",
            "status":       fields.status.name if fields.status else "Unknown",
            "priority":     fields.priority.name if fields.priority else "None",
            "assignee":     fields.assignee.displayName if fields.assignee else "Unassigned",
            "reporter":     fields.reporter.displayName if fields.reporter else "Unknown",
            "updated":      str(fields.updated)[:10] if fields.updated else "",
            "story_points": story_pts,
            "description":  _truncate(str(fields.description or ""), 200),
            "issue_type":   fields.issuetype.name if fields.issuetype else "Task",
            "labels":       ", ".join(fields.labels) if fields.labels else "",
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Private – raw requests path (fallback)
    # ─────────────────────────────────────────────────────────────────────────

    def _fetch_with_requests(self, jql: str, max_results: int) -> list[dict]:
        """Uses raw HTTP requests as a fallback when jira library is absent."""
        url    = f"{self.server}/rest/api/3/search"
        params = {
            "jql":        jql,
            "maxResults": max_results,
            "fields":     "summary,status,priority,assignee,reporter,updated,story_points,description,issuetype,labels",
        }
        data = self._raw_get(url, params=params)
        issues = data.get("issues", [])
        return [self._normalise_issue_raw(i) for i in issues]

    @staticmethod
    def _normalise_issue_raw(issue: dict) -> dict:
        """Convert a raw REST API issue dict into a plain dict."""
        f = issue.get("fields", {})
        return {
            "key":          issue.get("key", ""),
            "summary":      f.get("summary", ""),
            "status":       (f.get("status") or {}).get("name", "Unknown"),
            "priority":     (f.get("priority") or {}).get("name", "None"),
            "assignee":     (f.get("assignee") or {}).get("displayName", "Unassigned"),
            "reporter":     (f.get("reporter") or {}).get("displayName", "Unknown"),
            "updated":      str(f.get("updated", ""))[:10],
            "story_points": f.get("story_points") or f.get("customfield_10016") or 0,
            "description":  _truncate(str(f.get("description") or ""), 200),
            "issue_type":   (f.get("issuetype") or {}).get("name", "Task"),
            "labels":       ", ".join(f.get("labels", [])),
        }

    def _raw_get(self, url: str, params: Optional[dict] = None) -> dict:
        """Perform an authenticated GET request and return parsed JSON."""
        try:
            resp = requests.get(
                url,
                auth=HTTPBasicAuth(self.email, self.api_token),
                params=params,
                headers={"Accept": "application/json"},
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response else "?"
            try:
                detail = exc.response.json().get("errorMessages", [str(exc)])
            except Exception:
                detail = str(exc)
            raise JiraClientError(
                f"HTTP {status} from Jira: {detail}"
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            raise JiraClientError(
                f"Cannot reach Jira server at {self.server}: {exc}"
            ) from exc
        except requests.exceptions.Timeout:
            raise JiraClientError("Jira request timed out after 30 seconds.")

    # ─────────────────────────────────────────────────────────────────────────
    # Private – validation
    # ─────────────────────────────────────────────────────────────────────────

    def _validate_config(self):
        """Raise early if required environment variables are missing."""
        missing = []
        if not self.server:    missing.append("JIRA_SERVER")
        if not self.email:     missing.append("JIRA_EMAIL")
        if not self.api_token: missing.append("JIRA_API_TOKEN")
        if missing:
            raise JiraClientError(
                f"Missing required environment variables: {', '.join(missing)}. "
                "Please check your .env file."
            )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _truncate(text: str, max_len: int) -> str:
    """Return text clipped to max_len characters with ellipsis if needed."""
    return text[:max_len] + "…" if len(text) > max_len else text


# ─────────────────────────────────────────────────────────────────────────────
# Quick smoke-test (run as a script)
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    client = JiraClient()
    print("Testing connection …")
    user = client.test_connection()
    print(f"Logged in as: {user.get('displayName')} <{user.get('emailAddress')}>")

    jql = 'assignee = currentUser() AND status = "In Progress"'
    issues = client.fetch_issues(jql, max_results=10)
    print(f"\nFound {len(issues)} issue(s):")
    for i in issues:
        print(f"  [{i['key']}] {i['summary'][:80]}  ({i['status']})")
