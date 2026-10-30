"""维护者机制验证：不同会话分开，同会话的两个进程共用 worktree。"""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class SessionWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="coder-session-mechanism-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.git(self.source, "init", "-b", "source")
        self.git(self.source, "config", "user.name", "Fixture")
        self.git(self.source, "config", "user.email", "fixture@example.invalid")
        (self.source / "main.py").write_text("owner = 'source'\n")
        (self.source / "helper.py").write_text("owner = 'source'\n")
        self.git(self.source, "add", ".")
        self.git(self.source, "commit", "-m", "baseline")
        self.sessions = []
        for name in ("session-a", "session-b"):
            path = self.root / name
            self.git(self.source, "worktree", "add", "--detach", str(path), "HEAD")
            self.sessions.append(path)

    def git(self, path, *args):
        return subprocess.run(
            ["git", *args], cwd=path, text=True, capture_output=True, check=True
        ).stdout

    def test_same_session_parallel_writers_share_one_worktree(self):
        session, other = self.sessions
        head = self.git(session, "rev-parse", "HEAD")
        worker = '''from pathlib import Path
import sys, time
name, peer, target, content = sys.argv[1:]
Path(name + ".ready").touch()
deadline = time.monotonic() + 5
while not Path(peer + ".ready").exists():
    if time.monotonic() > deadline:
        raise RuntimeError("另一执行者未就绪")
    time.sleep(0.01)
Path(target).write_text(content)
'''
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", worker, name, peer, filename, content],
                cwd=session, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            for name, peer, filename, content in (
                ("main", "child", "main.py", "from helper import fee\nresult = 90 + fee(90)\n"),
                ("child", "main", "helper.py", "def fee(amount):\n    return 0 if amount >= 100 else 10\n"),
            )
        ]
        try:
            for process in processes:
                out, err = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, out + err)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        result = subprocess.run(
            [sys.executable, "-B", "-c", "import main; assert main.result == 100"],
            cwd=session, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(session, "rev-parse", "HEAD"), head)
        self.assertEqual(self.git(session, "diff", "--cached"), "")
        self.assertEqual((other / "main.py").read_text(), "owner = 'source'\n")
        worktrees = self.git(self.source, "worktree", "list", "--porcelain")
        self.assertEqual(sum(line.startswith("worktree ") for line in worktrees.splitlines()), 3)

    def test_different_sessions_can_edit_same_file_without_overwrite(self):
        a, b = self.sessions
        (a / "main.py").write_text("owner = 'a'\n")
        (b / "main.py").write_text("owner = 'b'\n")
        self.assertEqual((a / "main.py").read_text(), "owner = 'a'\n")
        self.assertEqual((b / "main.py").read_text(), "owner = 'b'\n")
        self.assertEqual((self.source / "main.py").read_text(), "owner = 'source'\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
