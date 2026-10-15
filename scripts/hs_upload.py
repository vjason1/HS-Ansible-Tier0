#!/usr/bin/env python3
"""Bundle logs and reports and upload them to Hammerspace.

Same intake and request as HS_PS_Toolkit.sh's hs_upload():

    POST https://v1proto.sc.hammerspace.com/upload/   (multipart/form-data)
      payload=prosvcs  cluster_id=manual  payload_type=prosvcs  file=@<bundle>.tar.gz

and the same bundle naming: <prefix>_<host>_<YYYYmmdd_HHMMSS>.tar.gz, deleted after
a successful upload and kept (path printed) when the upload fails.

Additions over the toolkit:
  * Secrets are masked in every text file before it is bundled (passwords,
    tokens, Authorization headers, user:password pairs, credentials in URLs,
    private-key blocks). Counts are recorded in MANIFEST.txt. --no-redact turns
    this off.
  * MANIFEST.txt lists every file with its size and SHA-256, plus who/where/when.
  * --bundle-only builds the bundle without uploading (air-gapped sites: copy
    it out by other means).
  * TLS certificates are verified unless --insecure is given (the toolkit's
    curl uses --insecure).

    python3 scripts/hs_upload.py --initials JV --customer Acme \\
        logs/ansible-20261015_*.log snapshots/2026-10-rocky-9.5/

    # directories are added recursively; --include-config adds vars/main.yml,
    # inventory*.yml and ansible.cfg (masked; vars/vault.yml is never included)

Exit codes: 0 uploaded (or bundle built with --bundle-only), 1 upload failed
(bundle kept), 2 usage error. Python 3.6+ standard library only.
"""

import argparse
import datetime
import getpass
import hashlib
import io
import os
import re
import socket
import ssl
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import uuid

UPLOAD_URL = "https://v1proto.sc.hammerspace.com/upload/"
FORM_FIELDS = (("payload", "prosvcs"), ("cluster_id", "manual"), ("payload_type", "prosvcs"))
DEFAULT_PREFIX = "tier0_bundle"
NAME_RE = re.compile(r"^[A-Za-z0-9]+$")
CONFIG_FILES = ("vars/main.yml", "ansible.cfg", "inventory.yml", "inventory.az.yml",
                "inventory.oci.yml", "inventory.aws.yml", "inventory.gcp.yml", "inventory.azure.yml")
NEVER_INCLUDE = re.compile(r"(^|/)(vault\.yml|\.vault_pass[^/]*|\.gui-state\.json|id_[a-z0-9]+|[^/]*\.pem|"
                           r"[^/]*\.key)$")

# ----------------------------------------------------------------------------
# Redaction
# ----------------------------------------------------------------------------
# A key counts as secret only when it ENDS in one of these words, so names such as
# net.ipv4.ipfrag_secret_interval or bridge-nf-pass-vlan-input-dev (sysctl) and
# password_policy_url are left alone.
_SECRET_KEY = (r"[\w.\-]*?(?:pass|password|passwd|passphrase|secret|token|api_?key|access_?key|secret_?key|"
               r"private_?key|access_key_id|credentials?)(?![\w.\-])")
REDACTIONS = [
    # -----BEGIN ... PRIVATE KEY----- ... -----END ... PRIVATE KEY-----
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
     lambda m: "-----BEGIN PRIVATE KEY-----\n********\n-----END PRIVATE KEY-----"),
    # Authorization: Basic xxx / Bearer xxx  (also inside JSON / escaped strings)
    (re.compile(r"(?i)(authorization\\?[\"']?\s*[:=]\s*\\?[\"']?\s*(?:basic|bearer|token)\s+)[A-Za-z0-9+/=._\-]+"),
     lambda m: m.group(1) + "********"),
    # scheme://user:password@host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^\s:/@\"']+:)[^\s@/\"']+(@)"),
     lambda m: m.group(1) + "********" + m.group(2)),
    # curl -u user:password / --user user:password
    (re.compile(r"((?:^|\s)(?:-u|--user)\s+['\"]?[^\s:'\"]+:)[^\s'\"]+"),
     lambda m: m.group(1) + "********"),
    # key=value, key: value, "key": "value"  where key looks secret
    (re.compile(r"(?i)(\b" + _SECRET_KEY + r"\\?[\"']?\s*(?::|=)\s*\\?[\"']?)([^\s\"',}\\]+)"),
     None),   # handled by _mask_kv (keeps {{ jinja }} references and *_file paths)
]


# Keys whose names match the secret pattern but whose values are not secrets
# (Kubernetes Secret object names in this repo's DI settings).
NOT_SECRET_KEYS = {"di_k8s_image_pull_secret", "di_k8s_api_credentials_secret"}


def _mask_kv(m):
    key_and_sep, value = m.group(1), m.group(2)
    key = re.match(r"[\w.\-]+", key_and_sep).group(0).lower()
    if (value.startswith(("{{", "********", "<", "/", "~/", "$"))      # jinja refs, placeholders, paths, $VARS
            or key in NOT_SECRET_KEYS
            or key.endswith(("_file", "_path", "_uri", "_url", "_name"))):
        return m.group(0)
    return key_and_sep + "********"


def redact_text(text):
    """Return (masked_text, number_of_values_masked)."""
    total = 0
    for rx, repl in REDACTIONS:
        fn = repl or _mask_kv
        count = [0]

        def wrap(m, fn=fn, count=count):
            out = fn(m)
            if out != m.group(0):
                count[0] += 1
            return out
        text = rx.sub(wrap, text)
        total += count[0]
    return text, total


def is_text(data):
    if b"\x00" in data[:8192]:
        return False
    try:
        data[:65536].decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


# ----------------------------------------------------------------------------
# Bundle
# ----------------------------------------------------------------------------
def git_commit(repo):
    try:
        return subprocess.check_output(["git", "-C", repo, "rev-parse", "--short", "HEAD"],
                                       stderr=subprocess.DEVNULL, timeout=5).decode().strip()
    except Exception:  # noqa: BLE001
        return ""


def collect(paths, repo):
    """Expand files/dirs into [(abs_path, arcname)] with arcnames relative to the repo."""
    out, seen = [], set()
    for p in paths:
        ap = os.path.realpath(os.path.expanduser(p))
        if not os.path.exists(ap):
            raise SystemExit("error: %s does not exist" % p)
        walk = [ap] if os.path.isfile(ap) else [
            os.path.join(d, f) for d, _, fs in os.walk(ap) for f in sorted(fs)]
        for f in walk:
            rel = os.path.relpath(f, repo) if f.startswith(repo + os.sep) else \
                os.path.join("external", os.path.basename(os.path.dirname(f)), os.path.basename(f))
            if NEVER_INCLUDE.search(rel.replace(os.sep, "/")):
                print("skipped (never bundled): %s" % rel, file=sys.stderr)
                continue
            if f not in seen:
                seen.add(f)
                out.append((f, rel))
    return out


def build_bundle(items, args, repo):
    host = socket.gethostname().split(".")[0] or "controller"
    stamp = time.strftime("%Y%m%d_%H%M%S")
    tag = "%s-%s-" % (args.initials, args.customer) if args.initials and args.customer else ""
    top = "%stier0_%s_%s" % (tag, host, stamp)
    os.makedirs(args.out_dir, exist_ok=True)
    # Same naming as the toolkit; a numeric suffix if a bundle from the same
    # second exists, so a bundle kept after a failed upload is never overwritten.
    base, n = "%s_%s_%s" % (args.prefix, host, stamp), 1
    while True:
        name = base + ("" if n == 1 else "-%d" % n) + ".tar.gz"
        path = os.path.join(args.out_dir, name)
        if not os.path.exists(path) and not os.path.exists(path[:-len(".tar.gz")] + ".manifest.txt"):
            break
        n += 1

    manifest = io.StringIO()
    w = manifest.write
    w("Hammerspace Tier 0 support bundle\n")
    w("bundle      : %s\n" % name)
    w("created     : %s\n" % datetime.datetime.now().astimezone().isoformat(timespec="seconds"))
    w("controller  : %s (user %s)\n" % (socket.gethostname(), getpass.getuser()))
    w("repository  : %s @ %s\n" % (repo, git_commit(repo) or "?"))
    if args.initials or args.customer:
        w("engineer    : %s   customer: %s\n" % (args.initials or "-", args.customer or "-"))
    if args.note:
        w("note        : %s\n" % args.note.replace("\n", " "))
    w("redaction   : %s\n\n" % ("ON (secrets masked as ********)" if args.redact else "OFF"))
    w("%-12s %-10s %-64s %s\n" % ("size", "masked", "sha256 (as bundled)", "file"))

    total_masked = 0
    with tarfile.open(path, "w:gz") as tar:
        for src, rel in items:
            with open(src, "rb") as fh:
                data = fh.read()
            masked = 0
            if args.redact and is_text(data):
                text, masked = redact_text(data.decode("utf-8", errors="surrogateescape"))
                data = text.encode("utf-8", errors="surrogateescape")
            total_masked += masked
            info = tarfile.TarInfo("%s/%s" % (top, rel.replace(os.sep, "/")))
            info.size = len(data)
            info.mtime = int(os.path.getmtime(src))
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
            w("%-12d %-10s %-64s %s\n" % (len(data), masked or "", hashlib.sha256(data).hexdigest(), rel))
        w("\n%d file(s), %d secret value(s) masked\n" % (len(items), total_masked))
        mdata = manifest.getvalue().encode()
        info = tarfile.TarInfo("%s/MANIFEST.txt" % top)
        info.size, info.mtime, info.mode = len(mdata), int(time.time()), 0o644
        tar.addfile(info, io.BytesIO(mdata))
    with open(path[:-len(".tar.gz")] + ".manifest.txt", "w") as fh:
        fh.write(manifest.getvalue())
    return path, total_masked


# ----------------------------------------------------------------------------
# Upload (streaming multipart, same fields as the toolkit's curl -F ...)
# ----------------------------------------------------------------------------
class MultipartStream(io.RawIOBase):
    def __init__(self, fields, file_field, file_path):
        self.boundary = "----tier0-%s" % uuid.uuid4().hex
        b = self.boundary.encode()
        parts = []
        for k, v in fields:
            parts.append(b"--" + b + b"\r\nContent-Disposition: form-data; name=\"" + k.encode() +
                         b"\"\r\n\r\n" + v.encode() + b"\r\n")
        head = (b"--" + b + b"\r\nContent-Disposition: form-data; name=\"" + file_field.encode() +
                b"\"; filename=\"" + os.path.basename(file_path).encode() +
                b"\"\r\nContent-Type: application/gzip\r\n\r\n")
        self._chunks = [io.BytesIO(b"".join(parts) + head), open(file_path, "rb"),
                        io.BytesIO(b"\r\n--" + b + b"--\r\n")]
        self.length = (len(b"".join(parts)) + len(head) + os.path.getsize(file_path) +
                       len(b"\r\n--" + b + b"--\r\n"))
        self.sent = 0
        self._last_report = 0

    def readable(self):
        return True

    def readinto(self, buf):
        while self._chunks:
            n = self._chunks[0].readinto(buf)
            if n:
                self.sent += n
                if self.sent - self._last_report >= 8 * 1024 * 1024 or self.sent == self.length:
                    self._last_report = self.sent
                    print("  sent %.1f / %.1f MB" % (self.sent / 1e6, self.length / 1e6), flush=True)
                return n
            self._chunks.pop(0).close()
        return 0


def upload(path, url, verify_tls, timeout):
    body = MultipartStream(FORM_FIELDS, "file", path)
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "multipart/form-data; boundary=%s" % body.boundary,
        "Content-Length": str(body.length),
        "User-Agent": "tier0-gui-hs-upload/1.0"})
    ctx = None
    if url.lower().startswith("https"):
        ctx = ssl.create_default_context() if verify_tls else ssl._create_unverified_context()  # noqa: SLF001
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            status, headers, text = resp.status, resp.headers, resp.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        status, headers, text = exc.code, exc.headers, exc.read(65536).decode("utf-8", "replace")
    print("HTTP %s  (%.1f s)" % (status, time.time() - start))
    for k, v in (headers or {}).items():
        print("%s: %s" % (k, v))
    if text.strip():
        print("\n" + text.strip()[:4000])
    return 200 <= int(status) < 300


def main(argv=None):
    ap = argparse.ArgumentParser(description="Bundle logs/reports and upload them to Hammerspace "
                                             "(same intake as HS_PS_Toolkit.sh).")
    ap.add_argument("paths", nargs="+", help="files and/or directories to include")
    ap.add_argument("--initials", default="", help="engineer initials (letters/numbers), names the bundle folder")
    ap.add_argument("--customer", default="", help="customer short name (letters/numbers)")
    ap.add_argument("--note", default="", help="free text recorded in MANIFEST.txt (e.g. case number)")
    ap.add_argument("--prefix", default=DEFAULT_PREFIX, help="bundle name prefix (default %(default)s)")
    ap.add_argument("--url", default=os.environ.get("TIER0_UPLOAD_URL", UPLOAD_URL),
                    help="intake URL (default %(default)s)")
    ap.add_argument("--insecure", action="store_true",
                    help="do not verify the server's TLS certificate (what the toolkit's curl --insecure does)")
    ap.add_argument("--include-config", action="store_true",
                    help="add vars/main.yml, ansible.cfg and inventory*.yml (masked; never vars/vault.yml)")
    ap.add_argument("--no-redact", dest="redact", action="store_false",
                    help="do NOT mask secrets in text files")
    ap.add_argument("--bundle-only", action="store_true", help="build the bundle, do not upload")
    ap.add_argument("--keep", action="store_true", help="keep the bundle after a successful upload")
    ap.add_argument("--repo", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--out-dir", default=None, help="where bundles/manifests go (default <repo>/logs/uploads)")
    ap.add_argument("--timeout", type=int, default=600, help="upload timeout in seconds")
    args = ap.parse_args(argv)

    args.repo = os.path.realpath(args.repo)
    args.out_dir = args.out_dir or os.path.join(args.repo, "logs", "uploads")
    for label, val in (("--initials", args.initials), ("--customer", args.customer)):
        if val and not NAME_RE.match(val):
            print("error: %s must be letters/numbers only (got %r)" % (label, val), file=sys.stderr)
            return 2
    if not re.match(r"^[A-Za-z0-9_\-]+$", args.prefix):
        print("error: --prefix must be letters, numbers, - or _", file=sys.stderr)
        return 2

    paths = list(args.paths)
    if args.include_config:
        paths += [os.path.join(args.repo, c) for c in CONFIG_FILES if os.path.isfile(os.path.join(args.repo, c))]
    items = collect(paths, args.repo)
    if not items:
        print("error: nothing to bundle", file=sys.stderr)
        return 2

    print("Bundling %d file(s)%s..." % (len(items), " with secrets masked" if args.redact else ""), flush=True)
    path, masked = build_bundle(items, args, args.repo)
    print("Bundle   : %s (%.1f MB, %d secret value(s) masked)" % (path, os.path.getsize(path) / 1e6, masked))
    print("Manifest : %s" % (path[:-len(".tar.gz")] + ".manifest.txt"), flush=True)
    if args.bundle_only:
        print("Not uploaded (--bundle-only). Send the bundle by other means.")
        return 0

    print("Uploading to %s%s..." % (args.url, "  (TLS not verified)" if args.insecure else ""), flush=True)
    try:
        ok = upload(path, args.url, not args.insecure, args.timeout)
    except ssl.SSLCertVerificationError as exc:
        print("\nUpload failed: the server's TLS certificate could not be verified (%s).\n"
              "HS_PS_Toolkit.sh uploads with curl --insecure; to do the same, re-run with --insecure\n"
              "(GUI: Logs & Reports -> Upload settings -> Don't verify the TLS certificate)." % exc.verify_message)
        ok = False
    except (urllib.error.URLError, OSError) as exc:
        print("\nUpload failed: %s\nThe controller needs HTTPS access to %s (a proxy is used when "
              "HTTPS_PROXY is set)." % (getattr(exc, "reason", exc), args.url))
        ok = False
    if ok:
        print("Upload complete.")
        if not args.keep:
            os.remove(path)
            print("Bundle removed (manifest kept).")
        return 0
    print("Upload failed — bundle preserved at: %s" % path)
    return 1


if __name__ == "__main__":
    sys.exit(main())
