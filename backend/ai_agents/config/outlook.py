"""Microsoft Graph API integration for Outlook email."""

import base64
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

# In-memory token cache: {email: {"token": str, "expires_at": float}}
_token_cache: dict[str, dict] = {}


def get_access_token(user_email: str) -> str:
    """OAuth2 client credentials flow (app-level, not user-level).
    Cached per email with 1hr TTL."""
    cached = _token_cache.get(user_email)
    if cached and cached["expires_at"] > time.time():
        return cached["token"]

    tenant = os.environ["AZURE_TENANT_ID"]
    resp = httpx.post(
        TOKEN_URL.format(tenant=tenant),
        data={
            "client_id": os.environ["AZURE_CLIENT_ID"],
            "client_secret": os.environ["AZURE_CLIENT_SECRET"],
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        },
    )
    resp.raise_for_status()
    data = resp.json()

    _token_cache[user_email] = {
        "token": data["access_token"],
        "expires_at": time.time() + 3500,  # ~1hr, small buffer
    }
    return data["access_token"]


def _headers(user_email: str) -> dict:
    return {
        "Authorization": f"Bearer {get_access_token(user_email)}",
        "Content-Type": "application/json",
    }


def send_email(from_email: str, to_email: str, subject: str, body: str,
               attachment_path: str | None = None) -> dict:
    """Send email via Graph API from a specific user's mailbox.
    Returns {message_id, thread_id, sent_at}."""
    message = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": body},
        "toRecipients": [{"emailAddress": {"address": to_email}}],
    }

    if attachment_path:
        file_path = Path(attachment_path)
        file_bytes = file_path.read_bytes()
        message["attachments"] = [{
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": file_path.name,
            "contentType": "application/vnd.openxmlformats-officedocument"
                           ".wordprocessingml.document",
            "contentBytes": base64.b64encode(file_bytes).decode(),
        }]

    # Use sendMail (sends immediately, no draft)
    url = f"{GRAPH_BASE}/users/{from_email}/sendMail"
    resp = httpx.post(url, headers=_headers(from_email),
                      json={"message": message, "saveToSentItems": True},
                      timeout=30)
    resp.raise_for_status()

    # sendMail returns 202 with no body; fetch the latest sent message
    # to get message_id and thread_id
    sent_url = (f"{GRAPH_BASE}/users/{from_email}/mailFolders/sentitems"
                f"/messages?$top=1&$orderby=sentDateTime desc"
                f"&$select=id,conversationId,sentDateTime")
    sent_resp = httpx.get(sent_url, headers=_headers(from_email), timeout=15)
    sent_resp.raise_for_status()
    sent_msg = sent_resp.json().get("value", [{}])[0]

    return {
        "message_id": sent_msg.get("id", ""),
        "thread_id": sent_msg.get("conversationId", ""),
        "sent_at": sent_msg.get("sentDateTime",
                                datetime.now(timezone.utc).isoformat()),
    }


def _strip_html(html: str) -> str:
    """Strip HTML tags, return plain text."""
    return BeautifulSoup(html, "html.parser").get_text(separator="\n").strip()


def get_unread_emails(user_email: str, hours_back: int = 24,
                      limit: int = 50) -> list[dict]:
    """Fetch unread inbox emails from the last N hours."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours_back)
             ).strftime("%Y-%m-%dT%H:%M:%SZ")

    url = (f"{GRAPH_BASE}/users/{user_email}/mailFolders/inbox/messages"
           f"?$filter=isRead eq false and receivedDateTime ge {since}"
           f"&$top={limit}&$orderby=receivedDateTime desc"
           f"&$select=id,conversationId,from,subject,body,receivedDateTime")

    resp = httpx.get(url, headers=_headers(user_email), timeout=20)
    resp.raise_for_status()

    results = []
    for msg in resp.json().get("value", []):
        sender = msg.get("from", {}).get("emailAddress", {})
        results.append({
            "message_id": msg["id"],
            "thread_id": msg.get("conversationId", ""),
            "sender_email": sender.get("address", ""),
            "sender_name": sender.get("name", ""),
            "subject": msg.get("subject", ""),
            "body_text": _strip_html(msg.get("body", {}).get("content", "")),
            "received_at": msg.get("receivedDateTime", ""),
        })
    return results


def mark_as_read(user_email: str, message_id: str) -> None:
    """Mark a specific message as read."""
    url = f"{GRAPH_BASE}/users/{user_email}/messages/{message_id}"
    resp = httpx.patch(url, headers=_headers(user_email),
                       json={"isRead": True}, timeout=10)
    resp.raise_for_status()


def get_thread(user_email: str, thread_id: str) -> list[dict]:
    """Get all messages in a conversation thread, sorted by date."""
    url = (f"{GRAPH_BASE}/users/{user_email}/messages"
           f"?$filter=conversationId eq '{thread_id}'"
           f"&$orderby=receivedDateTime asc"
           f"&$select=id,from,subject,body,receivedDateTime")

    resp = httpx.get(url, headers=_headers(user_email), timeout=20)
    resp.raise_for_status()

    results = []
    for msg in resp.json().get("value", []):
        sender = msg.get("from", {}).get("emailAddress", {})
        results.append({
            "message_id": msg["id"],
            "sender_email": sender.get("address", ""),
            "sender_name": sender.get("name", ""),
            "subject": msg.get("subject", ""),
            "body_text": _strip_html(msg.get("body", {}).get("content", "")),
            "received_at": msg.get("receivedDateTime", ""),
        })
    return results
