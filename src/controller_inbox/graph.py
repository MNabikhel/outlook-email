from __future__ import annotations

import base64
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any, Iterable

import httpx
import msal

from controller_inbox.extract import html_to_text
from controller_inbox.models import RawAttachment, RawMessage


GRAPH_BASE = "https://graph.microsoft.com/v1.0"
# MSAL asks for "offline_access" (the refresh token) by itself and refuses a sign-in that names it, or
# "openid" or "profile", so they are not listed here.
DELEGATED_SCOPES = ["User.Read", "Mail.Read", "Mail.ReadWrite", "Mail.Send"]
APP_SCOPES = ["https://graph.microsoft.com/.default"]


class GraphError(RuntimeError):
    pass


class GraphClient:
    def __init__(
        self,
        *,
        client_id: str,
        tenant_id: str = "common",
        client_secret: str = "",
        mailbox: str = "",
        cache_path: Path | None = None,
        timeout: float = 30.0,
    ):
        self.client_id = client_id
        self.tenant_id = tenant_id or "common"
        self.client_secret = client_secret
        self.mailbox = mailbox
        self.cache_path = cache_path
        self.timeout = timeout
        self._cache = msal.SerializableTokenCache()
        if cache_path and cache_path.exists():
            self._cache.deserialize(cache_path.read_text(encoding="utf-8"))
        authority = f"https://login.microsoftonline.com/{self.tenant_id}"
        if client_secret:
            self.app = msal.ConfidentialClientApplication(
                client_id,
                authority=authority,
                client_credential=client_secret,
                token_cache=self._cache,
            )
        else:
            self.app = msal.PublicClientApplication(
                client_id,
                authority=authority,
                token_cache=self._cache,
            )

    def _persist_cache(self) -> None:
        if self.cache_path and self._cache.has_state_changed:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(self._cache.serialize(), encoding="utf-8")

    def acquire_token(self, *, device_code_printer=print) -> str:
        if self.client_secret:
            result = self.app.acquire_token_for_client(scopes=APP_SCOPES)
        else:
            accounts = self.app.get_accounts()
            result = None
            if accounts:
                result = self.app.acquire_token_silent(DELEGATED_SCOPES, account=accounts[0])
            if not result:
                flow = self.app.initiate_device_flow(scopes=DELEGATED_SCOPES)
                if "user_code" not in flow:
                    raise GraphError(f"Could not start device login: {flow}")
                device_code_printer(flow["message"])
                result = self.app.acquire_token_by_device_flow(flow)
        if not result or "access_token" not in result:
            raise GraphError(result.get("error_description") if result else "No token returned")
        self._persist_cache()
        return result["access_token"]

    def _user_root(self) -> str:
        if self.mailbox:
            return f"/users/{self.mailbox}"
        return "/me"

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        token = self.acquire_token()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"
        url = path if path.startswith("http") else f"{GRAPH_BASE}{path}"
        with httpx.Client(timeout=self.timeout) as client:
            response = client.request(method, url, headers=headers, **kwargs)
        if response.status_code == 429:
            raise GraphError("Microsoft Graph throttled the request (HTTP 429). Wait and retry.")
        if response.status_code >= 400:
            raise GraphError(f"Graph {response.status_code}: {response.text[:800]}")
        return response

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.request("GET", path, params=params).json()

    def get_bytes(self, path: str) -> bytes:
        return self.request("GET", path).content

    def signed_in_user(self) -> dict[str, Any]:
        if self.mailbox:
            return self.get_json(f"/users/{self.mailbox}")
        return self.get_json("/me")


class GraphMailbox:
    def __init__(self, client: GraphClient):
        self.client = client

    def list_messages(self, received_after: datetime | None = None) -> Iterable[RawMessage]:
        # Graph rejects a sort on a property unless the filter names it first ("InefficientFilter"),
        # so receivedDateTime always leads, with an open lower bound when there is no cursor.
        since = received_after or datetime(1970, 1, 1, tzinfo=timezone.utc)
        stamp = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        filters = [f"receivedDateTime ge {stamp}", "isDraft eq false"]
        params = {
            "$top": "50",
            "$orderby": "receivedDateTime desc",
            "$select": (
                "id,subject,from,replyTo,receivedDateTime,body,bodyPreview,hasAttachments,"
                "importance,isRead,conversationId,internetMessageId"
            ),
            "$filter": " and ".join(filters),
        }
        url = f"{self.client._user_root()}/messages"
        while url:
            payload = self.client.get_json(url, params=params)
            params = None
            for item in payload.get("value", []):
                yield self._to_raw(item)
            url = payload.get("@odata.nextLink")

    def get_attachments(self, message_id: str) -> list[RawAttachment]:
        payload = self.client.get_json(f"{self.client._user_root()}/messages/{message_id}/attachments")
        attachments: list[RawAttachment] = []
        for item in payload.get("value", []):
            odata_type = item.get("@odata.type", "")
            if odata_type.endswith("fileAttachment") or item.get("contentBytes"):
                content = base64.b64decode(item.get("contentBytes") or "")
                attachments.append(
                    RawAttachment(
                        id=item.get("id") or item.get("name") or "attachment",
                        filename=item.get("name") or "untitled",
                        content_type=item.get("contentType") or "application/octet-stream",
                        size_bytes=int(item.get("size") or len(content)),
                        content=content,
                    )
                )
            elif odata_type.endswith("itemAttachment"):
                attachments.extend(self._item_attachment(message_id, item))
        return attachments

    def _item_attachment(self, message_id: str, item: dict[str, Any]) -> list[RawAttachment]:
        """A forwarded email (or other Outlook item) attached as an item. ``$value`` is its MIME,
        read like a forwarded .eml: its text, then the files it carried."""
        from controller_inbox.folder_mail import forwarded_attachments

        item_id = item.get("id") or item.get("name") or "item"
        name = item.get("name") or "forwarded item"
        mime = self.client.get_bytes(
            f"{self.client._user_root()}/messages/{message_id}/attachments/{item_id}/$value"
        )
        inner = BytesParser(policy=policy.default).parsebytes(mime or b"")
        found = forwarded_attachments(inner, f"{name}.eml")
        for att in found:
            att.id = f"{item_id}:{att.filename}"
        return found

    def apply_categories(self, message_id: str, categories: list[str], flag: bool) -> str:
        body: dict[str, Any] = {"categories": categories}
        if flag:
            body["flag"] = {"flagStatus": "flagged"}
            body["importance"] = "high"
        self.client.request("PATCH", f"{self.client._user_root()}/messages/{message_id}", json=body)
        return "written"

    def send_mail(self, to: str, subject: str, html: str) -> None:
        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "HTML", "content": html},
                "toRecipients": [{"emailAddress": {"address": to}}],
            },
            "saveToSentItems": True,
        }
        self.client.request("POST", f"{self.client._user_root()}/sendMail", json=payload)

    def _to_raw(self, item: dict[str, Any]) -> RawMessage:
        sender = (item.get("from") or {}).get("emailAddress") or {}
        body = item.get("body") or {}
        content = body.get("content") or ""
        if (body.get("contentType") or "").lower() == "html":
            text = html_to_text(content)
        else:
            text = content
        received = item.get("receivedDateTime") or datetime.now(timezone.utc).isoformat()
        received_dt = datetime.fromisoformat(received.replace("Z", "+00:00"))
        return RawMessage(
            id=item["id"],
            subject=item.get("subject") or "(no subject)",
            sender_name=sender.get("name") or "",
            sender_email=sender.get("address") or "",
            received_at=received_dt,
            body_text=text,
            body_preview=item.get("bodyPreview") or text[:240],
            has_attachments=bool(item.get("hasAttachments")),
            outlook_importance=(item.get("importance") or "normal").lower(),
            is_read=bool(item.get("isRead")),
            conversation_id=item.get("conversationId") or "",
            internet_message_id=item.get("internetMessageId") or "",
            source="graph",
            reply_to=_reply_to(item, sender.get("address") or ""),
            attachments=[],
        )


def _reply_to(item: dict[str, Any], sender: str) -> str:
    """Where replies go, for the fraud check: the first Reply-To address that isn't the sender's own."""
    addresses = [
        ((entry or {}).get("emailAddress") or {}).get("address") or "" for entry in item.get("replyTo") or []
    ]
    others = [address.strip().lower() for address in addresses if "@" in address and address.strip().lower() != sender.strip().lower()]
    return others[0] if others else ""
