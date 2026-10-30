"""维护者机制演练，使用真实 Git 与进程锁；不是技能运行时的自动合并工具。"""

from pathlib import Path
import fcntl
import json
import subprocess
import sys
import tempfile
import time
import unittest


def finish_session(target, session):
    """在夹具中执行文档所述候选预验、串行快进和合并后核实。"""
    trace = []
    old_task = None
    rebased_task = None

    def command(cwd, *argv):
        result = subprocess.run(argv, cwd=cwd, text=True, capture_output=True)
        trace.append({"cwd": str(cwd), "argv": argv, "exit_code": result.returncode,
                      "stdout": result.stdout, "stderr": result.stderr})
        return result

    def answer(status):
        return {"status": status, "original_task_commit": old_task,
                "rebased_task_commit": rebased_task, "trace": trace}

    common = command(session, "git", "rev-parse", "--git-common-dir")
    if common.returncode:
        return answer("invalid_repository")
    gitdir = Path(common.stdout.strip())
    if not gitdir.is_absolute():
        gitdir = (session / gitdir).resolve()
    with (gitdir / "coder-assistant-merge.lock").open("a") as lock:
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    return answer("lock_timeout")
                time.sleep(0.02)
        branch = command(target, "git", "symbolic-ref", "--short", "HEAD")
        if branch.returncode or branch.stdout.strip() != "delivery-target":
            return answer("wrong_target")
        if command(target, "git", "status", "--porcelain").stdout.strip():
            return answer("dirty_target")
        target_head = command(target, "git", "rev-parse", "HEAD").stdout.strip()
        candidate = command(session, "git", "rev-parse", "HEAD").stdout.strip()
        old_task = candidate
        if command(session, "git", "merge-base", "--is-ancestor", candidate, target_head).returncode == 0:
            checked = command(target, sys.executable, "-B", "verify.py")
            return answer("already_merged" if checked.returncode == 0 else "postcheck_failed")
        combined = command(session, "git", "rebase", "--no-autostash", target_head)
        if combined.returncode:
            return answer("conflict")
        rebased_task = command(session, "git", "rev-parse", "HEAD").stdout.strip()
        checked = command(session, sys.executable, "-B", "verify.py")
        if checked.returncode:
            return answer("candidate_failed")
        candidate = command(session, "git", "rev-parse", "HEAD").stdout.strip()
        if command(target, "git", "symbolic-ref", "--short", "HEAD").stdout.strip() != "delivery-target":
            return answer("wrong_target")
        if command(target, "git", "rev-parse", "HEAD").stdout.strip() != target_head:
            return answer("target_changed")
        if command(target, "git", "status", "--porcelain").stdout.strip():
            return answer("dirty_target")
        merged = command(target, "git", "merge", "--ff-only", "--no-autostash", candidate)
        if merged.returncode:
            return answer("merge_rejected")
        checked = command(target, sys.executable, "-B", "verify.py")
        return answer("merged" if checked.returncode == 0 else "postcheck_failed")


class ReturnMergeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="coder-return-merge-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / "source"
        self.target.mkdir()
        self.git(self.target, "init", "-b", "delivery-target")
        self.git(self.target, "config", "user.name", "Fixture")
        self.git(self.target, "config", "user.email", "fixture@example.invalid")
        (self.target / "data.py").write_text("a = 0\n" + "# independent boundary\n" * 10 + "b = 0\n")
        (self.target / "verify.py").write_text("from data import a, b\nassert a >= 0 and b >= 0\n")
        (self.target / "notes.txt").write_text("user original\n")
        self.git(self.target, "add", ".")
        self.git(self.target, "commit", "-m", "baseline")
        self.baseline = self.head(self.target)
        self.a, self.b = self.root / "a", self.root / "b"
        for path in (self.a, self.b):
            self.git(self.target, "worktree", "add", "--detach", str(path), "HEAD")

    def git(self, cwd, *argv):
        return subprocess.check_output(["git", *argv], cwd=cwd, text=True, stderr=subprocess.PIPE)

    def head(self, cwd):
        return self.git(cwd, "rev-parse", "HEAD").strip()

    def change(self, session, name, value):
        path = session / "data.py"
        path.write_text(path.read_text().replace(f"{name} = 0", f"{name} = {value}"))
        self.git(session, "add", "data.py")
        self.git(session, "commit", "-m", f"implement {name}")
        return self.head(session)

    def test_clean_completion_updates_target_branch_and_working_files(self):
        commit = self.change(self.a, "a", 1)
        result = finish_session(self.target, self.a)
        self.assertEqual(result["status"], "merged", result)
        self.assertEqual(self.head(self.target), commit)
        self.assertIn("a = 1", (self.target / "data.py").read_text())
        self.assertEqual(self.git(self.target, "status", "--porcelain"), "")

    def test_two_processes_rebase_against_latest_target_without_losing_changes(self):
        commits = [self.change(self.a, "a", 1), self.change(self.b, "b", 2)]
        processes = [subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--finish", str(self.target), str(session)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ) for session in (self.a, self.b)]
        try:
            for process in processes:
                out, err = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, out + err)
                self.assertEqual(json.loads(out)["status"], "merged", out)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        delivered = [self.head(self.a), self.head(self.b)]
        self.assertNotEqual(delivered, commits, "后一个会话应在前一个结果之上重放")
        for commit in delivered:
            self.git(self.target, "merge-base", "--is-ancestor", commit, "HEAD")
        self.assertEqual(self.git(self.target, "rev-list", "--min-parents=2", self.baseline + "..HEAD"), "")
        source = (self.target / "data.py").read_text()
        self.assertIn("a = 1", source)
        self.assertIn("b = 2", source)

    def test_conflict_is_kept_in_session_and_target_is_preserved(self):
        self.change(self.a, "a", 1)
        self.change(self.b, "a", 2)
        self.assertEqual(finish_session(self.target, self.a)["status"], "merged")
        before = self.head(self.target), (self.target / "data.py").read_bytes()
        self.assertEqual(finish_session(self.target, self.b)["status"], "conflict")
        self.assertEqual((self.head(self.target), (self.target / "data.py").read_bytes()), before)
        self.assertIn("UU data.py", self.git(self.b, "status", "--porcelain"))

    def test_dirty_target_is_not_stashed_committed_or_overwritten(self):
        self.change(self.a, "a", 1)
        (self.target / "notes.txt").write_text("user unsaved\n")
        (self.target / "draft.txt").write_text("untracked draft\n")
        before = self.git(self.target, "status", "--porcelain")
        self.assertEqual(finish_session(self.target, self.a)["status"], "dirty_target")
        self.assertEqual(self.head(self.target), self.baseline)
        self.assertEqual(self.git(self.target, "status", "--porcelain"), before)
        self.assertEqual((self.target / "notes.txt").read_text(), "user unsaved\n")
        self.assertEqual((self.target / "draft.txt").read_text(), "untracked draft\n")
        self.assertEqual(self.git(self.target, "stash", "list"), "")

    def test_failed_candidate_does_not_advance_target(self):
        self.change(self.a, "a", -1)
        self.assertEqual(finish_session(self.target, self.a)["status"], "candidate_failed")
        self.assertEqual(self.head(self.target), self.baseline)
        self.assertIn("a = 0", (self.target / "data.py").read_text())

    def test_resuming_completed_merge_is_idempotent(self):
        self.change(self.a, "a", 1)
        original_b = self.change(self.b, "b", 2)
        self.assertEqual(finish_session(self.target, self.a)["status"], "merged")
        result = finish_session(self.target, self.b)
        self.assertEqual(result["status"], "merged")
        self.assertEqual(result["original_task_commit"], original_b)
        self.assertNotEqual(result["rebased_task_commit"], original_b)
        previous = self.head(self.target)
        self.assertEqual(finish_session(self.target, self.b)["status"], "already_merged")
        self.assertEqual(self.head(self.target), previous)

    def test_changed_target_branch_is_not_used_silently(self):
        self.change(self.a, "a", 1)
        self.git(self.target, "switch", "-c", "other-target")
        self.assertEqual(finish_session(self.target, self.a)["status"], "wrong_target")
        self.assertEqual(self.head(self.target), self.baseline)

    def test_postcheck_failure_is_reported_without_resetting_merged_target(self):
        self.change(self.a, "a", 1)
        (self.a / "verify.py").write_text("from pathlib import Path\nassert Path.cwd().name != 'source'\n")
        self.git(self.a, "add", "verify.py")
        self.git(self.a, "commit", "-m", "simulate target-only check failure")
        candidate = self.head(self.a)
        self.assertEqual(finish_session(self.target, self.a)["status"], "postcheck_failed")
        self.assertEqual(self.head(self.target), candidate)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--finish":
        print(json.dumps(finish_session(Path(sys.argv[2]), Path(sys.argv[3])), ensure_ascii=False))
    else:
        unittest.main(verbosity=2)
