#!/usr/bin/env python3
"""Install the controller's SSH PUBLIC key on hosts, using a one-time password login.

For hosts where key-based SSH was never set up: log in once with a user and
password, append the controller's public key to that user's
~/.ssh/authorized_keys, then confirm that key-based login now works. From then
on Ansible uses the controller's own private key — which never leaves the
controller, so there is no private key file to copy around.

    export TIER0_SSH_PASSWORD='...'          # never pass the password as an argument
    python3 scripts/push_ssh_key.py --user ubuntu \\
        --pubkey ~/.ssh/id_ed25519.pub \\
        --target 10.200.101.216 --target node102=10.200.101.182:2222

    # create a key pair first if the controller has none
    python3 scripts/push_ssh_key.py --generate ~/.ssh/id_ed25519 --user ubuntu --target ...

How the password is supplied without sshpass or a terminal: OpenSSH asks an
SSH_ASKPASS helper for the password when it has no TTY. A throwaway 0700
helper prints $TIER0_SSH_PASSWORD; the password is never on a command line,
never in a file, and the helper is deleted on exit.

What runs on each host (as --user, no sudo): mkdir -p ~/.ssh (0700); append the
key to ~/.ssh/authorized_keys (0600) ONLY if that exact line is not already
there; restorecon on SELinux systems. Nothing else is changed. The key travels
over stdin, not in the remote command.

Host keys: unknown hosts are accepted and recorded (StrictHostKeyChecking=
accept-new); a host whose key CHANGED is refused, because sending a password to
it could hand the password to an impostor. For reimaged nodes that reuse IPs,
remove the stale entry (ssh-keygen -R <ip>) or pass --ignore-host-keys.

Exit codes: 0 = every host OK, 1 = at least one host failed, 2 = usage error.
Python 3.6+ standard library + the OpenSSH client.
"""

import argparse
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

PASSWORD_ENV = "TIER0_SSH_PASSWORD"
USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]{0,63}$")
HOST_RE = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9.\-]*|\[[0-9A-Fa-f:.]+\]|[0-9A-Fa-f:]+:[0-9A-Fa-f:.]+)$")
NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:\-\[\]]*$")
KEY_RE = re.compile(r"^(ssh-(ed25519|rsa|dss)|ecdsa-sha2-nistp\d+|sk-[a-z0-9\-@.]+) [A-Za-z0-9+/=]+( .*)?$")

# Runs on the remote host. Reads the key from stdin; idempotent.
REMOTE_SCRIPT = (
    'umask 077; IFS= read -r key || exit 3; '
    'mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh" && '
    'touch "$HOME/.ssh/authorized_keys" && chmod 600 "$HOME/.ssh/authorized_keys" || exit 4; '
    'if grep -qxF "$key" "$HOME/.ssh/authorized_keys"; then echo TIER0_KEY_PRESENT; '
    'else printf "%s\\n" "$key" >> "$HOME/.ssh/authorized_keys" && echo TIER0_KEY_ADDED || exit 5; fi; '
    'command -v restorecon >/dev/null 2>&1 && restorecon -R "$HOME/.ssh" >/dev/null 2>&1; true'
)


class Target(object):
    def __init__(self, spec):
        """spec: [name=]host[:port]"""
        name, _, addr = spec.rpartition("=")
        port = 22
        if addr.startswith("["):                      # [v6]:port or [v6]
            host, _, rest = addr[1:].partition("]")
            if rest.startswith(":") and rest[1:].isdigit():
                port = int(rest[1:])
        elif addr.count(":") == 1:                    # host:port
            host, _, p = addr.partition(":")
            if not p.isdigit():
                raise ValueError("bad port in %r" % spec)
            port = int(p)
        else:                                         # host, or bare IPv6
            host = addr
        if not host or host.startswith("-") or not HOST_RE.match(host):
            raise ValueError("bad host %r" % addr)
        if name and not NAME_RE.match(name):
            raise ValueError("bad host name %r" % name)
        if not 0 < port < 65536:
            raise ValueError("bad port in %r" % spec)
        self.name = name or host
        self.host = host
        self.port = port


def find_private_key(pub_path):
    priv = pub_path[:-4] if pub_path.endswith(".pub") else None
    return priv if priv and os.path.isfile(priv) else None


def make_askpass(tmpdir):
    path = os.path.join(tmpdir, "askpass.sh")
    with open(path, "w") as fh:
        fh.write('#!/bin/sh\nprintf \'%s\\n\' "$' + PASSWORD_ENV + '"\n')
    os.chmod(path, stat.S_IRWXU)
    return path


def ssh_base(user, target, ignore_host_keys, timeout):
    opts = ["-o", "ConnectTimeout=%d" % timeout, "-o", "ServerAliveInterval=10",
            "-o", "LogLevel=ERROR", "-p", str(target.port)]
    if ignore_host_keys:
        opts += ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                 "-o", "GlobalKnownHostsFile=/dev/null"]
    else:
        opts += ["-o", "StrictHostKeyChecking=accept-new"]
    return ["ssh"] + opts + ["-l", user, target.host]


def explain(stderr, rc):
    e = stderr.lower()
    if "permission denied" in e:
        return ("authentication failed — wrong user/password, or the host does not allow "
                "password logins for this user (sshd PasswordAuthentication / "
                "PermitRootLogin prohibit-password)")
    if "host key verification failed" in e or "remote host identification has changed" in e:
        return ("host key changed since it was recorded — if the node was reimaged, run "
                "'ssh-keygen -R <host>' or use --ignore-host-keys")
    if "connection refused" in e:
        return "connection refused — sshd not running or wrong port"
    if "timed out" in e or "no route to host" in e:
        return "host unreachable (timeout / no route)"
    if "could not resolve" in e:
        return "host name does not resolve"
    last = [l for l in stderr.strip().splitlines() if l.strip()]
    return last[-1] if last else "ssh exited with %s" % rc


def push_one(user, target, key_line, private_key, args, env):
    res = {"host": target.name, "address": "%s@%s:%d" % (user, target.host, target.port)}
    cmd = ssh_base(user, target, args.ignore_host_keys, args.timeout) + [
        "-o", "PreferredAuthentications=keyboard-interactive,password",
        "-o", "PubkeyAuthentication=no", "-o", "NumberOfPasswordPrompts=1",
        "sh", "-c", "'%s'" % REMOTE_SCRIPT.replace("'", "'\\''")]
    try:
        p = subprocess.run(cmd, input=(key_line + "\n").encode(), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, env=env, timeout=args.timeout + 60,
                           start_new_session=True)
    except subprocess.TimeoutExpired:
        res.update(ok=False, status="failed", detail="timed out")
        return res
    out, err = p.stdout.decode(errors="replace"), p.stderr.decode(errors="replace")
    if p.returncode != 0 or "TIER0_KEY_" not in out:
        res.update(ok=False, status="failed", detail=explain(err, p.returncode))
        return res
    res["status"] = "already present" if "TIER0_KEY_PRESENT" in out else "added"

    if private_key and not args.no_verify:
        vcmd = ssh_base(user, target, args.ignore_host_keys, args.timeout) + [
            "-o", "BatchMode=yes", "-o", "PasswordAuthentication=no",
            "-o", "KbdInteractiveAuthentication=no", "-o", "IdentitiesOnly=yes",
            "-i", private_key, "true"]
        venv = {k: v for k, v in env.items() if k not in (PASSWORD_ENV, "SSH_ASKPASS",
                                                           "SSH_ASKPASS_REQUIRE")}
        v = subprocess.run(vcmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, env=venv, timeout=args.timeout + 30,
                           start_new_session=True)
        if v.returncode != 0:
            res.update(ok=False, status="key installed, key login FAILED",
                       detail=explain(v.stderr.decode(errors="replace"), v.returncode)
                       + " (check sshd PubkeyAuthentication and home-directory permissions)")
            return res
        res["verified"] = True
    res["ok"] = True
    return res


def generate_key(path, comment):
    path = os.path.expanduser(path)
    if os.path.exists(path):
        print("Key %s already exists — using it (not overwritten)." % path)
        return path + ".pub"
    os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", path],
                   check=True, stdin=subprocess.DEVNULL)
    print("Generated new key pair %s (+ .pub)" % path)
    return path + ".pub"


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Install the controller's SSH public key on hosts (password login once).",
        epilog="The password is read from $%s." % PASSWORD_ENV)
    ap.add_argument("--user", required=True, help="remote user to log in as and authorize the key for")
    ap.add_argument("--target", action="append", required=True, metavar="[NAME=]HOST[:PORT]",
                    help="host to set up (repeatable)")
    keysrc = ap.add_mutually_exclusive_group(required=True)
    keysrc.add_argument("--pubkey", help="public key file to install, e.g. ~/.ssh/id_ed25519.pub")
    keysrc.add_argument("--generate", metavar="PRIVATE_KEY_PATH",
                        help="create an ed25519 key pair at this path if it does not exist, then install it")
    ap.add_argument("--ignore-host-keys", action="store_true",
                    help="do not check or record host keys (reimaged nodes reusing IPs)")
    ap.add_argument("--no-verify", action="store_true", help="skip the key-login check afterwards")
    ap.add_argument("--parallel", type=int, default=8, help="hosts handled at once (default 8)")
    ap.add_argument("--timeout", type=int, default=15, help="SSH connect timeout in seconds (default 15)")
    args = ap.parse_args(argv)

    if not shutil.which("ssh"):
        print("error: the OpenSSH client (ssh) is not installed on this controller", file=sys.stderr)
        return 2
    if not USER_RE.match(args.user):
        print("error: invalid user name %r" % args.user, file=sys.stderr)
        return 2
    password = os.environ.get(PASSWORD_ENV, "")
    if not password:
        print("error: set the password in $%s" % PASSWORD_ENV, file=sys.stderr)
        return 2
    try:
        targets, seen = [], set()
        for spec in args.target:
            t = Target(spec)
            if (t.host, t.port) not in seen:   # same address twice would race on authorized_keys
                seen.add((t.host, t.port))
                targets.append(t)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    if args.generate:
        pub = generate_key(args.generate, "tier0-controller@%s" % os.uname()[1])
    else:
        pub = os.path.expanduser(args.pubkey)
    try:
        with open(pub) as fh:
            key_line = fh.read().strip()
    except OSError as exc:
        print("error: cannot read public key: %s" % exc, file=sys.stderr)
        return 2
    if "PRIVATE KEY" in key_line or not KEY_RE.match(key_line) or "\n" in key_line:
        print("error: %s is not a single OpenSSH public key line — refusing (never send a "
              "private key)" % pub, file=sys.stderr)
        return 2
    private_key = find_private_key(pub)

    print("Public key : %s" % pub)
    print("            %s" % (key_line[:40] + "..." + key_line[-30:]))
    print("Private key: %s (stays on this controller)" % (private_key or "not found — login check skipped"))
    print("Remote user: %s" % args.user)
    print("Hosts      : %d%s" % (len(targets), "  (host keys NOT checked)" if args.ignore_host_keys else ""))
    print()

    tmp = tempfile.mkdtemp(prefix="tier0-askpass-")
    try:
        env = dict(os.environ)
        env.update({"SSH_ASKPASS": make_askpass(tmp), "SSH_ASKPASS_REQUIRE": "force",
                    "DISPLAY": env.get("DISPLAY") or "tier0:0"})
        with ThreadPoolExecutor(max_workers=max(1, min(args.parallel, 32))) as pool:
            results = list(pool.map(lambda t: push_one(args.user, t, key_line, private_key, args, env),
                                    targets))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    w = max(len(r["host"]) for r in results)
    for r in results:
        mark = "OK  " if r["ok"] else "FAIL"
        extra = " + key login verified" if r.get("verified") else ""
        print("%s %-*s %-28s %s%s%s" % (mark, w, r["host"], r["address"], r["status"], extra,
                                        (" — " + r["detail"]) if r.get("detail") else ""))
    ok = sum(1 for r in results if r["ok"])
    print("\n%d of %d hosts ready for key-based SSH as %s." % (ok, len(results), args.user))
    if ok and private_key:
        print("Ansible: set ansible_user=%s and ansible_ssh_private_key_file=%s for these hosts "
              "(the GUI can write both into the inventory)." % (args.user, private_key))
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
