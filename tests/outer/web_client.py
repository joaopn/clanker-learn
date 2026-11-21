"""A small HTTPS client for the web server's tests, run in the neighbour container by
`python3 -c <this file's text>`: one request per run, described as JSON on standard input,
answered as JSON on standard output. It keeps the cookies itself, in a file named by the
request, sets the Origin header, and does not verify the certificate (the server's own,
self-signed); asked for "cert", it returns that certificate instead.

The request: {"url", "method"?, "jar"?, "origin"?, "json"?, "filler"?}. "origin" is the
header's value, the request's own origin when it is missing, and no header when it is
null. "filler" sends {"proof": "<that many x>"} as the body. The answer: {"status", "body",
"set_cookie": [raw Set-Cookie headers], "headers": {lower-case name: value}}, or {"cert":
<PEM>}; status 0 when nothing answered, with the error in "body"."""
import http.cookies
import json
import os
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request


def load_jar(path):
    if path and os.path.exists(path):
        with open(path) as fh:
            return json.load(fh)
    return {}


def keep(path, jar, set_cookies):
    """The cookies after `set_cookies`: a Max-Age of 0 is a removal."""
    for header in set_cookies:
        parsed = http.cookies.SimpleCookie()
        parsed.load(header)
        for name, morsel in parsed.items():
            if morsel["max-age"] == "0":
                jar.pop(name, None)
            else:
                jar[name] = morsel.value
    if path:
        with open(path, "w") as fh:
            json.dump(jar, fh)


def main():
    asked = json.load(sys.stdin)
    parts = urllib.parse.urlsplit(asked["url"])
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    if asked.get("method") == "cert":
        with socket.create_connection((parts.hostname, parts.port), timeout=5) as raw, context.wrap_socket(raw) as tls:
            der = tls.getpeercert(binary_form=True)
        print(json.dumps({"cert": ssl.DER_cert_to_PEM_cert(der)}))
        return
    jar = load_jar(asked.get("jar"))
    headers = {}
    origin = asked.get("origin", f"https://{parts.netloc}")
    if origin is not None:
        headers["Origin"] = origin
    if jar:
        headers["Cookie"] = "; ".join(f"{name}={value}" for name, value in jar.items())
    data = None
    if "json" in asked:
        data = json.dumps(asked["json"]).encode()
    elif "filler" in asked:
        data = b'{"proof": "' + b"x" * asked["filler"] + b'"}'
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(asked["url"], data=data, headers=headers, method=asked.get("method", "GET"))
    try:
        with urllib.request.urlopen(request, timeout=20, context=context) as answer:
            status, body, set_cookies = answer.status, answer.read().decode(errors="replace"), answer.headers.get_all("Set-Cookie") or []
            headers = answer.headers
    except urllib.error.HTTPError as exc:
        status, body, set_cookies = exc.code, exc.read().decode(errors="replace"), exc.headers.get_all("Set-Cookie") or []
        headers = exc.headers
    except (OSError, urllib.error.URLError) as exc:
        print(json.dumps({"status": 0, "body": repr(exc), "set_cookie": []}))
        return
    keep(asked.get("jar"), jar, set_cookies)
    print(json.dumps({"status": status, "body": body, "set_cookie": set_cookies,
                      "headers": {name.lower(): value for name, value in headers.items()}}))


main()
