#!/usr/bin/env python3
"""
Outreach Automation — Flask Blueprint
Fetch recruiter emails via Microsoft Graph API, classify with AI,
generate reply suggestions, and send replies.
"""

import os
import json
from datetime import datetime, timedelta

import msal
import requests
from flask import Blueprint, request, jsonify, session
from anthropic import Anthropic

outreach_bp = Blueprint("outreach", __name__)

SESSION_VERSION = "4"

# Module-level MSAL app for token caching
_msal_app = None


def _is_logged_in():
    return (session.get("logged_in") is True and
            session.get("version") == SESSION_VERSION)


def _get_msal_app():
    global _msal_app
    if _msal_app is None:
        client_id = os.environ.get("AZURE_CLIENT_ID", "")
        tenant_id = os.environ.get("AZURE_TENANT_ID", "")
        client_secret = os.environ.get("AZURE_CLIENT_SECRET", "")
        if not client_id or not tenant_id or not client_secret:
            raise RuntimeError("AZURE_CLIENT_ID, AZURE_TENANT_ID, and AZURE_CLIENT_SECRET must be set")
        _msal_app = msal.ConfidentialClientApplication(
            client_id,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
            client_credential=client_secret,
        )
    return _msal_app


def _get_graph_token():
    app = _get_msal_app()
    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" in result:
        return result["access_token"]
    raise RuntimeError(f"Token error: {result.get('error_description', result.get('error', 'Unknown'))}")


def _graph_headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _parse_api_response(raw):
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        lines = [l for l in lines if not l.startswith("```")]
        raw = "\n".join(lines)
    return json.loads(raw)


# --- Routes ---

@outreach_bp.route("/outreach/emails", methods=["POST"])
def outreach_emails():
    if not _is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401

    try:
        return _outreach_emails_impl()
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


def _outreach_emails_impl():
    data = request.get_json()
    recruiter_email = data.get("recruiter_email", "")
    date_str = data.get("date", "")

    if not recruiter_email or not date_str:
        return jsonify({"error": "Recruiter and date required"}), 400

    # Build date filter
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        dt_next = dt + timedelta(days=1)
        date_filter = f"receivedDateTime ge {dt.strftime('%Y-%m-%dT00:00:00Z')} and receivedDateTime lt {dt_next.strftime('%Y-%m-%dT00:00:00Z')}"
    except ValueError:
        return jsonify({"error": "Invalid date format"}), 400

    try:
        token = _get_graph_token()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500

    headers = _graph_headers(token)

    # Fetch inbox emails for the date
    all_emails = []
    url = (
        f"https://graph.microsoft.com/v1.0/users/{recruiter_email}/mailFolders/Inbox/messages"
        f"?$filter={date_filter}"
        f"&$select=id,subject,from,toRecipients,receivedDateTime,bodyPreview,body,conversationId,isRead"
        f"&$orderby=receivedDateTime desc"
        f"&$top=100"
    )

    while url:
        resp = requests.get(url, headers=headers)
        if resp.status_code != 200:
            return jsonify({"error": f"Graph API error: {resp.status_code} — {resp.text[:300]}"}), 500
        result = resp.json()
        all_emails.extend(result.get("value", []))
        url = result.get("@odata.nextLink")

    if not all_emails:
        return jsonify({"emails": [], "counts": {"requirements": 0, "candidate_replies": 0, "action_needed": 0, "fyi": 0}})

    # Classify all emails in one Haiku call
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY not set"}), 500

    client = Anthropic(api_key=api_key)

    # Build batch classification prompt
    email_summaries = []
    for i, em in enumerate(all_emails):
        sender = em.get("from", {}).get("emailAddress", {}).get("name", "Unknown")
        subject = em.get("subject", "(no subject)")
        preview = (em.get("bodyPreview", "") or "")[:200]
        email_summaries.append(f"[{i}] From: {sender} | Subject: {subject} | Preview: {preview}")

    batch_prompt = "\n".join(email_summaries)

    system_prompt = (
        "You are an email classifier for a recruitment company. "
        "Classify each email into exactly one category:\n"
        '- "requirements": Job requirements, job descriptions, or client job orders\n'
        '- "candidate_replies": Replies from candidates about job opportunities\n'
        '- "action_needed": Emails requiring urgent action or follow-up\n'
        '- "fyi": Newsletters, notifications, FYI emails, or low-priority items\n\n'
        "Return ONLY valid JSON: an array of objects with keys \"index\" (integer) and \"category\" (string).\n"
        "Example: [{\"index\": 0, \"category\": \"requirements\"}, {\"index\": 1, \"category\": \"candidate_replies\"}]"
    )

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            system=system_prompt,
            messages=[{"role": "user", "content": f"Classify these {len(all_emails)} emails:\n\n{batch_prompt}"}],
        )
        classifications = _parse_api_response(response.content[0].text)
    except Exception:
        # Fallback: classify all as FYI if AI fails
        classifications = [{"index": i, "category": "fyi"} for i in range(len(all_emails))]

    # Build category map
    cat_map = {}
    for c in classifications:
        cat_map[c.get("index", -1)] = c.get("category", "fyi")

    # Build response
    counts = {"requirements": 0, "candidate_replies": 0, "action_needed": 0, "fyi": 0}
    emails_out = []
    for i, em in enumerate(all_emails):
        category = cat_map.get(i, "fyi")
        if category not in counts:
            category = "fyi"
        counts[category] += 1

        sender_obj = em.get("from", {}).get("emailAddress", {})
        emails_out.append({
            "id": em.get("id", ""),
            "subject": em.get("subject", "(no subject)"),
            "from_name": sender_obj.get("name", "Unknown"),
            "from_email": sender_obj.get("address", ""),
            "time": em.get("receivedDateTime", ""),
            "preview": em.get("bodyPreview", ""),
            "body": em.get("body", {}).get("content", ""),
            "conversation_id": em.get("conversationId", ""),
            "is_read": em.get("isRead", False),
            "category": category,
        })

    return jsonify({"emails": emails_out, "counts": counts})


@outreach_bp.route("/outreach/suggest", methods=["POST"])
def outreach_suggest():
    if not _is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401

    try:
        return _outreach_suggest_impl()
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


def _outreach_suggest_impl():
    data = request.get_json()
    recruiter_email = data.get("recruiter_email", "")
    email_id = data.get("email_id", "")
    conversation_id = data.get("conversation_id", "")
    recruiter_name = data.get("recruiter_name", "")

    if not recruiter_email or not email_id:
        return jsonify({"error": "Missing recruiter_email or email_id"}), 400

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY not set"}), 500

    try:
        token = _get_graph_token()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500

    headers = _graph_headers(token)
    import re

    # Fetch the specific email we're replying to
    email_url = f"https://graph.microsoft.com/v1.0/users/{recruiter_email}/messages/{email_id}?$select=id,subject,from,toRecipients,body,receivedDateTime"
    resp = requests.get(email_url, headers=headers)
    if resp.status_code != 200:
        return jsonify({"error": f"Could not fetch email: {resp.status_code}"}), 500

    email_data = resp.json()
    sender_name = email_data.get("from", {}).get("emailAddress", {}).get("name", "Unknown")
    sender_email = email_data.get("from", {}).get("emailAddress", {}).get("address", "")
    subject = email_data.get("subject", "(no subject)")
    body_html = email_data.get("body", {}).get("content", "")
    clean_body = re.sub(r'<[^>]+>', '', body_html).strip()
    # Collapse excessive whitespace/newlines
    clean_body = re.sub(r'\n{3,}', '\n\n', clean_body)
    clean_body = clean_body[:3000]

    # Fetch conversation thread for additional context (if available)
    thread_text = ""
    if conversation_id:
        thread_url = (
            f"https://graph.microsoft.com/v1.0/users/{recruiter_email}/messages"
            f"?$filter=conversationId eq '{conversation_id}'"
            f"&$orderby=receivedDateTime asc"
            f"&$select=id,subject,from,body,receivedDateTime"
            f"&$top=10"
        )
        resp = requests.get(thread_url, headers=headers)
        if resp.status_code == 200:
            msgs = resp.json().get("value", [])
            for m in msgs:
                if m.get("id") == email_id:
                    continue  # Skip the email we already have
                msg_sender = m.get("from", {}).get("emailAddress", {}).get("name", "Unknown")
                msg_body = re.sub(r'<[^>]+>', '', m.get("body", {}).get("content", "")).strip()
                msg_body = re.sub(r'\n{3,}', '\n\n', msg_body)
                thread_text += f"\n--- From: {msg_sender} ---\n{msg_body[:1000]}\n"

    client = Anthropic(api_key=api_key)

    system_prompt = (
        f"You are a professional recruitment email assistant for {recruiter_name} at ExcelTech Computers Pte Ltd.\n"
        "Draft a professional reply to the EMAIL YOU ARE REPLYING TO below.\n"
        "Rules:\n"
        "- Your reply must DIRECTLY address the content of the email — answer questions, acknowledge information, or take action on what was asked\n"
        "- Address the sender by their name\n"
        "- Keep it concise and professional\n"
        "- Do NOT write a generic recruitment outreach email — this is a REPLY to a specific email\n"
        "- End with:\n"
        f"  {recruiter_name}\n"
        "  ExcelTech Computers Pte Ltd\n\n"
        "Return ONLY the reply text, no extra commentary."
    )

    user_content = f"EMAIL YOU ARE REPLYING TO:\nFrom: {sender_name} <{sender_email}>\nSubject: {subject}\n\n{clean_body}"
    if thread_text:
        user_content += f"\n\nPREVIOUS MESSAGES IN THREAD (for context only):\n{thread_text}"

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        suggestion = response.content[0].text.strip()
    except Exception as e:
        return jsonify({"error": f"AI error: {e}"}), 500

    return jsonify({"suggestion": suggestion})


@outreach_bp.route("/outreach/send", methods=["POST"])
def outreach_send():
    if not _is_logged_in():
        return jsonify({"error": "Not authenticated"}), 401

    try:
        return _outreach_send_impl()
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


def _outreach_send_impl():
    data = request.get_json()
    recruiter_email = data.get("recruiter_email", "")
    email_id = data.get("email_id", "")
    reply_body = data.get("reply_body", "")

    if not recruiter_email or not email_id or not reply_body:
        return jsonify({"error": "Missing required fields"}), 400

    try:
        token = _get_graph_token()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500

    headers = _graph_headers(token)

    # Convert plain text to HTML
    html_body = reply_body.replace("\n", "<br>")

    reply_url = f"https://graph.microsoft.com/v1.0/users/{recruiter_email}/messages/{email_id}/reply"
    payload = {
        "message": {
            "body": {
                "contentType": "HTML",
                "content": html_body
            }
        }
    }

    resp = requests.post(reply_url, headers=headers, json=payload)
    if resp.status_code in (200, 202):
        return jsonify({"status": "sent"})
    else:
        return jsonify({"error": f"Send failed: {resp.status_code} — {resp.text[:300]}"}), 500
