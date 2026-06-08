"""
AppWay Feedback – Lambda handler
POST /feedback → validates payload → sends SES email to configured recipients.
"""
import json
import os
import re
import boto3

_ses = boto3.client("sesv2", region_name=os.environ.get("AWS_REGION", "eu-west-1"))

_TO_RAW   = os.environ.get("FEEDBACK_TO_ADDRESSES", "")
_FROM     = os.environ.get("FEEDBACK_FROM_ADDRESS", "")
_TO_LIST  = [a.strip() for a in _TO_RAW.split(",") if a.strip()]

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


# ── helpers ────────────────────────────────────────────────────────────

def _resp(code: int, obj: dict) -> dict:
    return {
        "statusCode": code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST,OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        },
        "body": json.dumps(obj),
    }


def _stars(n) -> str:
    if n is None:
        return "–"
    return "★" * int(n) + "☆" * (5 - int(n)) + f"  ({n}/5)"


# ── handler ────────────────────────────────────────────────────────────

def lambda_handler(event, _context):
    # handle CORS preflight
    if event.get("requestContext", {}).get("http", {}).get("method") == "OPTIONS":
        return _resp(200, {})

    # parse body
    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return _resp(400, {"error": "invalid json"})

    # honeypot
    if body.get("website"):
        return _resp(200, {"ok": True})   # silently discard bots

    # extract fields
    first    = str(body.get("first_name") or "").strip()
    last     = str(body.get("last_name")  or "").strip()
    email    = str(body.get("email")      or "").strip()
    phone    = str(body.get("phone")      or "").strip() or None
    feedback = str(body.get("feedback")   or "").strip()
    rating   = body.get("rating")

    # validate required
    if not first:
        return _resp(400, {"error": "first_name is required"})
    if not last:
        return _resp(400, {"error": "last_name is required"})
    if not email or not _EMAIL_RE.match(email):
        return _resp(400, {"error": "valid email is required"})
    if not feedback or len(feedback) < 5:
        return _resp(400, {"error": "feedback must be at least 5 characters"})
    if len(feedback) > 5000:
        return _resp(400, {"error": "feedback too long (max 5000 chars)"})
    if rating is not None:
        try:
            rating = int(rating)
        except (TypeError, ValueError):
            return _resp(400, {"error": "invalid rating"})
        if rating not in (1, 2, 3, 4, 5):
            return _resp(400, {"error": "rating must be 1–5"})

    # build email
    subject = f"[AppWay Feedback] {first} {last}"
    text_body = (
        f"Clinician feedback received via AppWay\n"
        f"{'=' * 50}\n\n"
        f"Name    : {first} {last}\n"
        f"Email   : {email}\n"
        f"Phone   : {phone or '–'}\n"
        f"Rating  : {_stars(rating)}\n\n"
        f"Feedback:\n{'-' * 40}\n{feedback}\n"
    )

    # send via SES v2
    try:
        _ses.send_email(
            FromEmailAddress=_FROM,
            Destination={"ToAddresses": _TO_LIST},
            ReplyToAddresses=[email],
            Content={
                "Simple": {
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body":    {"Text": {"Data": text_body, "Charset": "UTF-8"}},
                }
            },
        )
    except Exception as exc:
        print(f"SES error: {exc}")
        return _resp(500, {"error": "failed to send email; please try again later"})

    return _resp(200, {"ok": True})
