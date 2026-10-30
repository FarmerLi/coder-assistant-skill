"""维护者机制演练：真实 Git、SQLite 和进程锁；不证明模型会自动选择这些机制。"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest


class IsolationScenarios(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="coder-isolation-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.git(self.source, "init", "-b", "original")
        self.git(self.source, "config", "user.name", "Fixture")
        self.git(self.source, "config", "user.email", "fixture@example.invalid")
        (self.source / "value.txt").write_text("base\n")
        self.git(self.source, "add", ".")
        self.git(self.source, "commit", "-m", "baseline")

    def git(self, cwd, *args, check=True):
        return subprocess.run(
            ["git", *args], cwd=cwd, text=True, capture_output=True, check=check
        )

    def worktree(self, name):
        target = self.root / name
        self.git(self.source, "worktree", "add", "--detach", str(target), "HEAD")
        return target

    def test_dirty_input_and_review_snapshot_stay_independent(self):
        (self.source / "value.txt").write_text("user dirty\n")
        (self.source / "draft.txt").write_text("user untracked\n")
        before = self.git(self.source, "status", "--porcelain").stdout
        session = self.worktree("session")
        self.assertEqual((session / "value.txt").read_text(), "base\n")
        self.assertFalse((session / "draft.txt").exists())
        # 明确带入所需未提交现状，不在源工作区执行 add 或 commit。
        for name in ("value.txt", "draft.txt"):
            shutil.copy2(self.source / name, session / name)
        self.git(session, "add", ".")
        self.git(session, "commit", "-m", "task input snapshot")
        child = self.worktree("child")
        self.git(child, "checkout", "--detach", self.git(session, "rev-parse", "HEAD").stdout.strip())
        review = self.root / "review"
        shutil.copytree(child, review, ignore=shutil.ignore_patterns(".git"))
        (child / "value.txt").write_text("child edit\n")
        (session / "value.txt").write_text("session edit\n")
        self.assertEqual((review / "value.txt").read_text(), "user dirty\n")
        self.assertEqual((review / "draft.txt").read_text(), "user untracked\n")
        self.assertEqual((self.source / "value.txt").read_text(), "user dirty\n")
        self.assertEqual(self.git(self.source, "status", "--porcelain").stdout, before)

    def test_overlapping_changes_require_explicit_resolution(self):
        a, b = self.worktree("a"), self.worktree("b")
        for path, value in ((a, "A\n"), (b, "B\n")):
            (path / "value.txt").write_text(value)
            self.git(path, "add", ".")
            self.git(path, "commit", "-m", value.strip())
        b_head = self.git(b, "rev-parse", "HEAD").stdout.strip()
        result = self.git(a, "cherry-pick", b_head, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("UU value.txt", self.git(a, "status", "--porcelain").stdout)
        self.assertEqual((b / "value.txt").read_text(), "B\n")
        self.assertEqual(self.git(b, "rev-parse", "HEAD").stdout.strip(), b_head)
        (a / "value.txt").write_text("A+B\n")
        self.git(a, "add", ".")
        self.git(a, "cherry-pick", "--continue")
        self.assertEqual((a / "value.txt").read_text(), "A+B\n")
        self.assertEqual((self.source / "value.txt").read_text(), "base\n")

    def test_same_branch_is_not_force_checked_out_twice(self):
        result = self.git(
            self.source, "worktree", "add", str(self.root / "duplicate"), "original", check=False
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "duplicate" / "value.txt").exists())

    def test_shared_sqlite_database_separates_test_namespaces(self):
        database = self.root / "shared.sqlite"
        with sqlite3.connect(database) as conn:
            conn.execute("CREATE TABLE records (namespace TEXT, value TEXT)")

        def write_namespace(namespace):
            with sqlite3.connect(database, timeout=5) as conn:
                conn.execute("INSERT INTO records VALUES (?, ?)", (namespace, namespace))
                conn.execute("DELETE FROM records WHERE namespace = ? AND value = ?", (namespace, "old"))

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(write_namespace, ("session-a", "session-b")))
        with sqlite3.connect(database) as conn:
            self.assertEqual(
                sorted(conn.execute("SELECT namespace, value FROM records").fetchall()),
                [("session-a", "session-a"), ("session-b", "session-b")],
            )

    @unittest.skipUnless(sys.platform != "win32", "演练使用 POSIX flock")
    def test_two_processes_queue_for_one_shared_mutable_resource(self):
        worker = self.root / "worker.py"
        worker.write_text('''from pathlib import Path
import fcntl, json, sys, time
root, owner = Path(sys.argv[1]), sys.argv[2]
with (root / "resource.lock").open("a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    busy = root / "resource.busy"
    with busy.open("x") as marker:
        marker.write(owner)
    # 资源在独占期间切换为当前待验版本，并核实该版本。
    version = root / "running-version.txt"
    version.write_text(owner)
    time.sleep(0.1)
    assert version.read_text() == owner
    with (root / "visits.jsonl").open("a") as log:
        log.write(json.dumps({"owner": owner, "tested_version": version.read_text()}) + "\\n")
    busy.unlink()
''')
        processes = [
            subprocess.Popen(
                [sys.executable, str(worker), str(self.root), name],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for name in ("version-a", "version-b")
        ]
        for process in processes:
            out, err = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, out + err)
        visits = [json.loads(line) for line in (self.root / "visits.jsonl").read_text().splitlines()]
        self.assertEqual({v["owner"] for v in visits}, {"version-a", "version-b"})
        self.assertTrue(all(v["owner"] == v["tested_version"] for v in visits))
        self.assertFalse((self.root / "resource.busy").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
