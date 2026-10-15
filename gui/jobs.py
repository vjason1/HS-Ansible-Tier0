"""Background job runner for the GUI.

Each job is one subprocess (argv list, never a shell) whose combined output is
appended to <repo>/logs/gui-jobs/<id>.log and streamed to the browser by offset.
Secrets (vault password, Hammerspace password) are handed to the child via a
0600 temp file or the environment, never on the command line, and the temp
files are removed when the job ends.
"""

import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid


class Job(object):
    def __init__(self, jobs_dir, title, argv, cwd, env, cleanup_paths, meta, on_finish=None):
        self.id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.title = title
        self.argv = argv
        self.cwd = cwd
        self.env = env
        self.meta = meta or {}
        self.cleanup_paths = cleanup_paths or []
        self.log_path = os.path.join(jobs_dir, self.id + ".log")
        self.meta_path = os.path.join(jobs_dir, self.id + ".json")
        self.status = "queued"
        self.returncode = None
        self.started = None
        self.finished = None
        self.proc = None
        self.on_finish = on_finish
        self._lock = threading.Lock()

    def to_dict(self):
        return {"id": self.id, "title": self.title, "argv": self.display_argv(),
                "status": self.status, "returncode": self.returncode,
                "started": self.started, "finished": self.finished, "meta": self.meta}

    def display_argv(self):
        return list(self.argv)

    def _save_meta(self):
        try:
            with open(self.meta_path, "w") as fh:
                json.dump(self.to_dict(), fh, indent=2)
        except OSError:
            pass

    def start(self):
        self.started = time.time()
        self.status = "running"
        self._save_meta()
        t = threading.Thread(target=self._run, name="job-" + self.id)
        t.daemon = True
        t.start()

    def _run(self):
        with open(self.log_path, "ab", buffering=0) as log:
            log.write(("$ %s\n\n" % " ".join(_quote(a) for a in self.argv)).encode())
            try:
                self.proc = subprocess.Popen(
                    self.argv, cwd=self.cwd, env=self.env, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                self.returncode = self.proc.wait()
                if self.status == "stopping":
                    self.status = "stopped"
                else:
                    self.status = "succeeded" if self.returncode == 0 else "failed"
            except OSError as exc:
                log.write(("\n[gui] failed to start: %s\n" % exc).encode())
                self.status = "failed"
                self.returncode = -1
            finally:
                self.finished = time.time()
                log.write(("\n[gui] finished: %s (rc=%s)\n" % (self.status, self.returncode)).encode())
                for p in self.cleanup_paths:
                    shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else _unlink(p)
                self._save_meta()
                if self.on_finish:
                    try:
                        self.on_finish(self)
                    except Exception:  # noqa: BLE001  (auditing must never break a job)
                        pass

    def stop(self):
        with self._lock:
            if self.proc and self.status == "running":
                self.status = "stopping"
                try:
                    os.killpg(self.proc.pid, signal.SIGTERM)
                except OSError:
                    pass
                threading.Timer(10, self._kill).start()

    def _kill(self):
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except OSError:
                pass

    def read(self, offset):
        try:
            with open(self.log_path, "rb") as fh:
                fh.seek(offset)
                data = fh.read(512 * 1024)
        except OSError:
            data = b""
        return data.decode("utf-8", errors="replace"), offset + len(data)


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _quote(a):
    if a and all(c.isalnum() or c in "-_./=:,@%+" for c in a):
        return a
    return "'" + a.replace("'", "'\\''") + "'"


class JobManager(object):
    def __init__(self, repo, jobs_dir, on_finish=None):
        self.repo = repo
        self.jobs_dir = jobs_dir
        self.on_finish = on_finish
        os.makedirs(jobs_dir, mode=0o700, exist_ok=True)
        self.jobs = {}
        self._load_history()

    def _load_history(self):
        for name in sorted(os.listdir(self.jobs_dir)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.jobs_dir, name)) as fh:
                    d = json.load(fh)
            except (OSError, ValueError):
                continue
            j = Job(self.jobs_dir, d["title"], d["argv"], self.repo, None, [], d.get("meta"))
            j.id = d["id"]
            j.log_path = os.path.join(self.jobs_dir, j.id + ".log")
            j.meta_path = os.path.join(self.jobs_dir, j.id + ".json")
            j.status = d["status"] if d["status"] not in ("running", "stopping", "queued") else "lost"
            j.returncode, j.started, j.finished = d.get("returncode"), d.get("started"), d.get("finished")
            self.jobs[j.id] = j

    def secret_dir(self):
        return tempfile.mkdtemp(prefix="tier0gui-secret-")

    def start(self, title, argv, env_extra=None, cleanup_paths=None, meta=None):
        env = dict(os.environ)
        env.update({"ANSIBLE_NOCOLOR": "1", "ANSIBLE_FORCE_COLOR": "0", "NO_COLOR": "1",
                    "PYTHONUNBUFFERED": "1", "ANSIBLE_LOCALHOST_WARNING": "False"})
        env.update(env_extra or {})
        job = Job(self.jobs_dir, title, argv, self.repo, env, cleanup_paths, meta, self.on_finish)
        self.jobs[job.id] = job
        job.start()
        return job

    def list(self):
        return [j.to_dict() for j in sorted(self.jobs.values(), key=lambda j: j.started or 0,
                                            reverse=True)]

    def get(self, job_id):
        return self.jobs.get(job_id)

    def drop_missing(self):
        """Forget finished jobs whose log was deleted (log retention)."""
        for jid in [j.id for j in self.jobs.values()
                    if j.status not in ("running", "stopping", "queued") and not os.path.exists(j.log_path)]:
            del self.jobs[jid]

    def running(self):
        return [j for j in self.jobs.values() if j.status in ("running", "stopping")]
