"""Gmail OAuth helpers for OTP retrieval and confirmation emails."""

from __future__ import annotations

import base64
import mimetypes
import re
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from autojob import config


OTP_REGEX = re.compile(r"\b(\d{4,8})\b")


@dataclass
class GmailAutomationService:
    """Thin Gmail API client for read/send flows used during auto-apply."""

    credentials_path: Path = config.GMAIL_CREDENTIALS_PATH
    token_path: Path = config.GMAIL_TOKEN_PATH

    def _require_google_deps(self):
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise RuntimeError(
                "Gmail automation dependencies are missing. Install google-auth-oauthlib and google-api-python-client."
            ) from exc
        return Request, Credentials, InstalledAppFlow, build

    def _get_credentials(self, scopes: list[str]):
        Request, Credentials, InstalledAppFlow, _ = self._require_google_deps()

        creds = None
        if self.token_path.exists():
            creds = Credentials.from_authorized_user_file(str(self.token_path), scopes)

        if creds and creds.valid:
            return creds

        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            self.token_path.write_text(creds.to_json(), encoding="utf-8")
            return creds

        if not self.credentials_path.exists():
            raise RuntimeError(
                f"Gmail OAuth credentials file not found at {self.credentials_path}. "
                "Download Desktop OAuth credentials from Google Cloud Console and place them there."
            )

        flow = InstalledAppFlow.from_client_secrets_file(str(self.credentials_path), scopes)
        creds = flow.run_local_server(port=8080, open_browser=True)
        self.token_path.write_text(creds.to_json(), encoding="utf-8")
        return creds

    def _build_service(self, scopes: list[str]):
        _, _, _, build = self._require_google_deps()
        creds = self._get_credentials(scopes)
        return build("gmail", "v1", credentials=creds)

    def get_recent_emails(
        self,
        keyword: str = "",
        max_results: int = 3,
        newer_than: str = "10m",
    ) -> list[dict[str, Any]]:
        """Fetch recent emails with decoded body text."""
        service = self._build_service(
            [
                "https://www.googleapis.com/auth/gmail.readonly",
            ]
        )
        query_parts = [f"newer_than:{newer_than}"]
        if keyword.strip():
            query_parts.append(keyword.strip())
        query = " ".join(query_parts)

        response = service.users().messages().list(
            userId="me",
            maxResults=max_results,
            q=query,
        ).execute()

        messages = response.get("messages", [])
        results: list[dict[str, Any]] = []
        for message in messages:
            full_message = service.users().messages().get(
                userId="me",
                id=message["id"],
                format="full",
            ).execute()
            results.append(self._parse_message(full_message))
        return results

    def extract_verification_code(self, keyword: str = "") -> str | None:
        """Return the first OTP-like code found in recent matching emails."""
        for email in self.get_recent_emails(keyword=keyword, max_results=5, newer_than="15m"):
            body = email.get("body", "")
            match = OTP_REGEX.search(body)
            if match:
                return match.group(1)
        return None

    def send_email(
        self,
        to: str,
        subject: str,
        body: str,
        attachments: list[str] | None = None,
    ) -> str:
        """Send an email via Gmail API and return the message id."""
        service = self._build_service(
            [
                "https://www.googleapis.com/auth/gmail.send",
                "https://www.googleapis.com/auth/gmail.readonly",
            ]
        )

        message = EmailMessage()
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)

        for attachment in attachments or []:
            path = Path(attachment)
            mime_type, _ = mimetypes.guess_type(path.name)
            maintype, subtype = (mime_type or "application/octet-stream").split("/", 1)
            with path.open("rb") as file_handle:
                message.add_attachment(
                    file_handle.read(),
                    maintype=maintype,
                    subtype=subtype,
                    filename=path.name,
                )

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        response = service.users().messages().send(
            userId="me",
            body={"raw": raw},
        ).execute()
        return response["id"]

    def _parse_message(self, message: dict[str, Any]) -> dict[str, Any]:
        headers = {
            header["name"]: header["value"]
            for header in message.get("payload", {}).get("headers", [])
        }
        return {
            "id": message.get("id", ""),
            "subject": headers.get("Subject", ""),
            "from": headers.get("From", ""),
            "date": headers.get("Date", ""),
            "body": self._extract_body(message.get("payload", {})),
        }

    def _extract_body(self, payload: dict[str, Any]) -> str:
        if payload.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")

        parts = payload.get("parts", [])
        for part in parts:
            if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
                return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
        for part in parts:
            if part.get("mimeType") == "text/html" and part.get("body", {}).get("data"):
                return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
        return ""
