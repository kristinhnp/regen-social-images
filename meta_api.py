"""
A small client for Meta's Graph API.

Uses only the Python standard library, so there is nothing extra to
install. Access tokens are sent in the POST body rather than the query
string, and are scrubbed from every error message before it is shown.
"""

import json
import mimetypes
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request

import config

GRAPH_HOST = "https://graph.facebook.com"
TIMEOUT = 60
RETRIES = 3


class MetaError(Exception):
    """A Graph API error already translated into plain English."""

    def __init__(self, message, code=None, subcode=None, retryable=False):
        super().__init__(message)
        self.code = code
        self.subcode = subcode
        self.retryable = retryable


# Error codes worth explaining properly instead of echoing Meta's wording.
_FRIENDLY = {
    190: (
        "Your access token is no longer valid. It has either expired or been "
        "revoked.\nRun:  .venv/bin/python check_token.py\n"
        "and follow the 'Getting your token' steps in the README to make a new one."
    ),
    200: (
        "Your token does not have permission to do this.\n"
        "The app needs pages_manage_posts, pages_read_engagement and "
        "pages_show_list for Facebook,\nplus instagram_basic and "
        "instagram_content_publish for Instagram.\n"
        "Re-generate the token with those boxes ticked."
    ),
    100: (
        "Meta rejected one of the values that was sent. If this is an "
        "Instagram post, the most\ncommon cause is an image URL it cannot "
        "reach, or an image that is not a JPEG."
    ),
    803: "That Page or Instagram account id does not exist, or your token cannot see it.",
    368: "The account is temporarily blocked from posting, usually for posting too often.",
}

_RETRYABLE_CODES = {1, 2, 4, 17, 32, 341, 613}


def _describe(payload, status):
    err = (payload or {}).get("error", {})
    code = err.get("code")
    subcode = err.get("error_subcode")
    meta_message = err.get("message", "")

    if code in _FRIENDLY:
        message = _FRIENDLY[code]
        if code == 100 and meta_message:
            message += "\n\nMeta said: " + meta_message
    elif meta_message:
        message = "Meta said: " + meta_message
    else:
        message = "Meta returned HTTP %s with no explanation." % status

    retryable = code in _RETRYABLE_CODES or (status is not None and status >= 500)
    return MetaError(config.redact(message), code, subcode, retryable)


def _encode_multipart(fields, file_field, file_path):
    """Build a multipart/form-data body for uploading one image."""
    boundary = "----RegenHolistics%s" % random.randint(10 ** 12, 10 ** 13)
    crlf = b"\r\n"
    parts = []
    for key, value in fields.items():
        parts.append(("--" + boundary).encode())
        parts.append(('Content-Disposition: form-data; name="%s"' % key).encode())
        parts.append(b"")
        parts.append(str(value).encode("utf-8"))

    filename = os.path.basename(file_path)
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    with open(file_path, "rb") as f:
        blob = f.read()
    parts.append(("--" + boundary).encode())
    parts.append(
        ('Content-Disposition: form-data; name="%s"; filename="%s"'
         % (file_field, filename)).encode()
    )
    parts.append(("Content-Type: " + ctype).encode())
    parts.append(b"")
    body = crlf.join(parts) + crlf + blob + crlf + ("--" + boundary + "--").encode() + crlf
    return body, "multipart/form-data; boundary=" + boundary


class Graph(object):
    def __init__(self, token, version=None):
        self.token = token
        self.version = version or config.graph_version()

    def _url(self, path):
        return "%s/%s/%s" % (GRAPH_HOST, self.version, path.lstrip("/"))

    def _send(self, request):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = None
            raise _describe(payload, exc.code)
        except urllib.error.URLError as exc:
            raise MetaError(
                config.redact("Could not reach Meta (%s). Check your internet "
                              "connection." % exc.reason),
                retryable=True,
            )

    def _with_retries(self, build_request, label):
        last = None
        for attempt in range(1, RETRIES + 1):
            try:
                return build_request()
            except MetaError as exc:
                last = exc
                if not exc.retryable or attempt == RETRIES:
                    raise
                wait = 2 ** attempt + random.random()
                config.safe_print(
                    "    %s failed (attempt %d of %d). Retrying in %.0fs."
                    % (label, attempt, RETRIES, wait)
                )
                time.sleep(wait)
        raise last

    def get(self, path, params=None, label="request"):
        params = dict(params or {})
        params["access_token"] = self.token

        def run():
            url = self._url(path) + "?" + urllib.parse.urlencode(params)
            return self._send(urllib.request.Request(url, method="GET"))

        return self._with_retries(run, label)

    def post(self, path, params=None, image_path=None, label="request"):
        params = dict(params or {})
        params["access_token"] = self.token

        def run():
            if image_path:
                body, ctype = _encode_multipart(params, "source", image_path)
                request = urllib.request.Request(
                    self._url(path), data=body, method="POST"
                )
                request.add_header("Content-Type", ctype)
            else:
                request = urllib.request.Request(
                    self._url(path),
                    data=urllib.parse.urlencode(params).encode("utf-8"),
                    method="POST",
                )
            return self._send(request)

        return self._with_retries(run, label)


def url_is_public(url, timeout=20):
    """
    Confirm an image URL is reachable without any credentials.

    Instagram fetches the image itself, so a private repo or a bad path
    fails inside Meta with a vague message. Better to catch it here.
    """
    try:
        request = urllib.request.Request(url, method="GET")
        request.add_header("Range", "bytes=0-0")
        request.add_header("User-Agent", "regen-holistics-publisher")
        with urllib.request.urlopen(request, timeout=timeout) as r:
            ctype = r.headers.get("Content-Type", "")
            return True, ctype
    except urllib.error.HTTPError as exc:
        return False, "HTTP %s" % exc.code
    except Exception as exc:  # noqa: BLE001 - surfaced to the user as text
        return False, exc.__class__.__name__
