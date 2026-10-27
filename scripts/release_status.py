"""Authenticated origin commit-status receipts; no SSH or new server endpoint.

Credentials stay in memory and come from the existing Git credential helper
(server: its installed askpass). Neither credentials nor HTTP bodies are logged.
Python 3.9+, standard library only.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


ORIGIN = "https://git.lu9.com/lu9/RachelCode.git"
API = "https://git.lu9.com/api/v1/repos/lu9/RachelCode"
PROTOCOL = "isolated-v1"
READY = "isolated-release/dispatcher"
FILES = ("isolated_release.py", "release_scope.py", "migrate_isolated_app.py", "release_status.py")


class StatusError(RuntimeError):
    pass


def manifest(directory):
    return {name: hashlib.sha256((Path(directory) / name).read_bytes()).hexdigest() for name in FILES}


def manifest_digest(hashes):
    if set(hashes) != set(FILES) or any(not re.fullmatch(r"[0-9a-f]{64}", v) for v in hashes.values()):
        raise StatusError("invalid installed dispatcher manifest")
    return hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def exact_sha(sha):
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise StatusError("invalid status commit")
    return sha


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise StatusError("origin status API redirect refused")


class OriginStatus:
    def __init__(self, *, repository=None, askpass="/usr/bin/false"):
        env = os.environ.copy()
        env.update(GIT_TERMINAL_PROMPT="0", GIT_ASKPASS=str(askpass))
        command = ["git"]
        if repository:
            command += ["--git-dir", str(repository)]
        command += ["-c", "credential.interactive=false", "credential", "fill"]
        try:
            result = subprocess.run(command, input="url=" + ORIGIN + "\n\n", text=True,
                                    capture_output=True, env=env, timeout=15)
        except (OSError, subprocess.SubprocessError):
            raise StatusError("origin status credential helper unavailable") from None
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if result.returncode or not values.get("username") or not values.get("password"):
            raise StatusError("origin status credentials unavailable")
        self._authorization = "Basic " + base64.b64encode(
            (values["username"] + ":" + values["password"]).encode()).decode()
        self._opener = build_opener(NoRedirect())

    def request(self, path, data=None):
        body = None if data is None else json.dumps(data).encode()
        request = Request(API + path, data=body, headers={
            "Authorization": self._authorization, "Accept": "application/json",
            "Content-Type": "application/json", "Cache-Control": "no-cache"})
        try:
            with self._opener.open(request, timeout=15) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise StatusError("origin status response too large")
                return json.loads(raw)
        except HTTPError as exc:
            raise StatusError("origin status API HTTP " + str(exc.code)) from None
        except (URLError, OSError, ValueError):
            raise StatusError("origin status API unavailable or invalid") from None

    def latest(self, sha, context):
        # Scan all bounded pages, then choose newest ID. Never fall back to an
        # older success after a newer pending/failure receipt.
        matches = []
        for page in range(1, 21):
            items = self.request("/statuses/" + exact_sha(sha) + "?limit=50&page=" + str(page))
            if not isinstance(items, list) or any(not isinstance(x, dict) for x in items):
                raise StatusError("invalid origin status list")
            matches.extend(x for x in items if x.get("context") == context)
            if len(items) < 50:
                return max(matches, key=lambda x: int(x["id"]), default=None)
        raise StatusError("origin status pagination limit exceeded")

    def post(self, sha, context, state, description):
        if state not in {"pending", "success", "failure", "error"} or len(description) > 255:
            raise StatusError("invalid release status")
        return self.request("/statuses/" + exact_sha(sha), {
            "context": context, "state": state, "description": description})


def fields(status):
    if not status or status.get("status") != "success":
        raise StatusError("server release proof is missing or not successful")
    parts = status.get("description", "").split(";")
    if not parts or parts[0] != PROTOCOL:
        raise StatusError("server release proof protocol mismatch")
    try:
        pairs = [part.split("=", 1) for part in parts[1:]]
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError()
    except ValueError:
        raise StatusError("invalid server release proof") from None
    return result


def readiness(client, sha, digest, app):
    proof = fields(client.latest(sha, READY))
    if proof.get("dispatcher") != digest or app not in proof.get("apps", "").split(","):
        raise StatusError("server dispatcher not installed or reviewed manifest differs")
    return proof


def post_ready(client, sha, proof):
    client.post(sha, READY, "success", PROTOCOL + ";dispatcher=" + manifest_digest(proof["installed_hashes"])
                + ";apps=" + ",".join(sorted(proof["enabled_apps"])))


def post_accepted(client, app, sha, expected, proof, evidence, protected):
    if (len(protected) != 2 or app in protected or evidence.get("protected_after") != protected
            or len(evidence.get("workers", [])) != expected["workers"]):
        raise StatusError("incomplete server isolation acceptance evidence")
    client.post(sha, "isolated-release/" + app, "success", PROTOCOL + ";sha=" + exact_sha(sha)
                + ";gen=" + str(expected["release_generation"]) + ";fp=" + expected["source_fingerprint"]
                + ";isolation=2/2;workers=" + str(expected["workers"])
                + ";proof=" + manifest_digest(proof["installed_hashes"]))


def verify_receipt(status, sha, expected, digest):
    proof = fields(status)
    required = {"sha": exact_sha(sha), "gen": str(expected["release_generation"]),
                "fp": expected["source_fingerprint"], "isolation": "2/2", "proof": digest}
    if any(proof.get(key) != value for key, value in required.items()):
        raise StatusError("server target acceptance receipt mismatch")
    if not re.fullmatch(r"[1-9][0-9]*", proof.get("workers", "")):
        raise StatusError("server worker acceptance missing")
    return proof


def wait_receipt(client, app, sha, expected, digest, *, timeout=600):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.latest(sha, "isolated-release/" + app)
        if status and status.get("status") in {"failure", "error"}:
            raise StatusError("server rejected target release; GitHub unchanged")
        if status and status.get("status") == "success":
            return verify_receipt(status, sha, expected, digest)
        time.sleep(10)
    raise StatusError("server target acceptance timed out; GitHub unchanged")
