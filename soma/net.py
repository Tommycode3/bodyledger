"""Minimal HTTP on urllib: zero dependencies, like the rest of the toolchain."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

UA = "soma/0.1 (+local personal data sync)"


class HttpError(Exception):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} on {url}: {body[:300]}")
        self.status = status
        self.body = body
        self.url = url


def request(method: str, url: str, *, params: dict | None = None,
            json_body: dict | None = None, form: dict | None = None,
            headers: dict | None = None, timeout: int = 120) -> dict:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    data = None
    h = {"User-Agent": UA, "Accept": "application/json"}
    if json_body is not None:
        data = json.dumps(json_body).encode()
        h["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise HttpError(e.code, e.read().decode("utf-8", "replace"), url) from None
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw}


def get(url: str, **kw) -> dict:
    return request("GET", url, **kw)


def post(url: str, **kw) -> dict:
    return request("POST", url, **kw)
