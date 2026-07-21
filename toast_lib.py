"""Reusable Toast API client for CIEL / Sophie (read-only Standard API access).

Robust HTTP: pooled requests.Session with urllib3 Retry (handles 429 + connection
resets + 5xx with backoff). Safe for large concurrent historical pulls.
"""
import json, os, time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = os.path.dirname(os.path.abspath(__file__))
API = "https://ws-api.toasttab.com"

RESTAURANTS = {
    "CIEL":   "b2baeb31-a168-4725-924f-5d7917f7871d",
    "Sophie": "b1f6089c-2ceb-46b9-adbe-14221ff035e5",
}
TZ_OFFSET = "-0500"  # Houston; bucket by each order's businessDate so DST is irrelevant

_session = None


def session():
    global _session
    if _session is None:
        s = requests.Session()
        retry = Retry(
            total=8, connect=8, read=8, status=8,
            backoff_factor=1.0,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        ad = HTTPAdapter(max_retries=retry, pool_connections=6, pool_maxsize=6)
        s.mount("https://", ad)
        _session = s
    return _session


def _get(url, headers, timeout=90):
    try:
        r = session().get(url, headers=headers, timeout=timeout)
    except Exception as e:
        return 0, str(e)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, r.text[:1000]


def authenticate():
    creds = json.load(open(os.path.join(BASE, "creds.json")))
    r = session().post(f"{API}/authentication/v1/authentication/login", json={
        "clientId": creds["clientId"],
        "clientSecret": creds["clientSecret"],
        "userAccessType": "TOAST_MACHINE_CLIENT",
    }, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"Auth failed {r.status_code}: {r.text[:300]}")
    tok = r.json()["token"]
    rec = {"accessToken": tok["accessToken"], "obtained": time.time(),
           "expiresIn": tok.get("expiresIn", 3600)}
    json.dump(rec, open(os.path.join(BASE, "token.json"), "w"))
    return rec["accessToken"]


def get_token():
    p = os.path.join(BASE, "token.json")
    if os.path.exists(p):
        t = json.load(open(p))
        if time.time() - t.get("obtained", 0) < t.get("expiresIn", 3600) - 300:
            return t["accessToken"]
    return authenticate()


def api_get(path, restaurant_guid, params=None, token=None):
    token = token or get_token()
    headers = {"Authorization": f"Bearer {token}",
               "Toast-Restaurant-External-ID": restaurant_guid}
    from urllib.parse import urlencode
    q = ("?" + urlencode(params)) if params else ""
    return _get(f"{API}{path}{q}", headers)
