"""HMAC signing for outgoing webhook URLs.

Apollo's phone-reveal webhook is unauthenticated (Apollo can't sign in to
us), so we sign each webhook URL we hand to Apollo with HMAC-SHA256 over
(request_id, candidate_id) and verify the signature on the callback.
"""

import hmac
import hashlib
import os


def _secret() -> bytes:
    key = os.environ.get("SECRET_KEY")
    if not key:
        raise RuntimeError("SECRET_KEY environment variable is required")
    return key.encode("utf-8")


def sign_phone_reveal(request_id: str, candidate_id: str) -> str:
    msg = f"phone-reveal:{request_id}:{candidate_id}".encode("utf-8")
    return hmac.new(_secret(), msg, hashlib.sha256).hexdigest()


def verify_phone_reveal(request_id: str, candidate_id: str, sig: str) -> bool:
    if not (request_id and candidate_id and sig):
        return False
    expected = sign_phone_reveal(request_id, candidate_id)
    return hmac.compare_digest(expected, sig)
