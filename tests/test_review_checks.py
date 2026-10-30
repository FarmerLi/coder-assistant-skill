"""检查自动化执行器的实际进程、证据与失效行为。"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_review_checks.py"


class ReviewCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.project = self.base / "project"
        self.project.mkdir()
        (self.project / "src").mkdir()
        (self.project / "src/app.py").write_text("value = 1\n")
        self.config = self.base / "checks.json"
        self.output = self.base / "evidence"

    def command(self, code, **options):
        return {"id": "check", "argv": [sys.executable, "-c", code], **options}

    def run_checks(self, checks, inputs=None, raw=None):
        config = {"inputs": ["src"] if inputs is None else inputs, "checks": checks}
        self.config.write_text(json.dumps(config if raw is None else raw))
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--project", str(self.project),
             "--config", str(self.config), "--output", str(self.output)],
            capture_output=True, text=True, timeout=10,
        )
        report = json.loads(result.stdout)
        return result, report

    def verify(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--project", str(self.project),
             "--verify", str(self.output / "result.json")],
            capture_output=True, text=True, timeout=10,
        )
        return result, json.loads(result.stdout)

    def test_success_keeps_real_output_and_input_manifest(self):
        result, report = self.run_checks([self.command("print('actual output')")])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(report["status"], "passed")
        self.assertIn("src/app.py", report["inputs_before"])
        self.assertEqual(report["inputs_before"], report["inputs_after"])
        check = report["checks"][0]
        self.assertEqual(check["exit_code"], 0)
        self.assertIn("actual output", (self.output / check["log"]).read_text())
        self.assertEqual(json.loads((self.output / "result.json").read_text()), report)

    def test_failure_does_not_hide_later_check_results(self):
        failed = self.command("import sys; print('broken'); sys.exit(7)")
        next_check = self.command("print('second ran')", id="second")
        result, report = self.run_checks([failed, next_check])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["status"], "failed")
        self.assertEqual([c["status"] for c in report["checks"]], ["failed", "passed"])
        self.assertEqual(report["checks"][0]["exit_code"], 7)

    def test_optional_failure_is_visible_as_warning(self):
        result, report = self.run_checks([self.command("raise SystemExit(4)", required=False)])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(report["status"], "passed_with_warnings")
        self.assertEqual(report["checks"][0]["status"], "failed")

    def test_missing_executable_is_error_and_does_not_abort_batch(self):
        missing = {"id": "missing", "argv": [str(self.base / "missing-executable")]}
        result, report = self.run_checks([missing, self.command("print('ok')")])
        self.assertEqual(result.returncode, 1)
        self.assertEqual([c["status"] for c in report["checks"]], ["error", "passed"])

    def test_timeout_is_not_a_pass(self):
        result, report = self.run_checks([self.command("import time; time.sleep(2)", timeout_seconds=0.1)])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["checks"][0]["status"], "timeout")

    @unittest.skipUnless(os.name == "posix", "进程组回收在 POSIX 上验证")
    def test_timeout_stops_child_process_writes(self):
        marker = self.base / "orphan-marker"
        child = f"import time; from pathlib import Path; time.sleep(0.5); Path({str(marker)!r}).write_text('orphan')"
        parent = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(2)"
        result, report = self.run_checks([self.command(parent, timeout_seconds=0.15)])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report["checks"][0]["status"], "timeout")
        time.sleep(0.6)
        self.assertFalse(marker.exists())

    def test_source_mutation_invalidates_success(self):
        code = "from pathlib import Path; Path('src/app.py').write_text('value = 2\\n')"
        result, report = self.run_checks([self.command(code)])
        self.assertEqual(result.returncode, 3)
        self.assertEqual(report["status"], "stale")
        self.assertEqual(report["checks"][0]["status"], "passed")
        self.assertIn("src/app.py", report["changed_inputs"])

    def test_added_input_file_is_detected(self):
        result, report = self.run_checks([self.command("from pathlib import Path; Path('src/new.py').write_text('new')")])
        self.assertEqual(result.returncode, 3)
        self.assertIn("src/new.py", report["changed_inputs"])

    def test_config_mutation_also_invalidates_success(self):
        code = f"from pathlib import Path; Path({str(self.config)!r}).write_text('{{}}')"
        result, report = self.run_checks([self.command(code)])
        self.assertEqual(result.returncode, 3)
        self.assertTrue(report["config_changed"])

    def test_empty_checks_cannot_produce_green_result(self):
        result, report = self.run_checks([])
        self.assertEqual(result.returncode, 2)
        self.assertEqual(report["status"], "configuration_error")

    def test_string_shell_command_is_rejected_before_execution(self):
        marker = self.base / "should-not-exist"
        valid = self.command(f"from pathlib import Path; Path({str(marker)!r}).touch()")
        result, report = self.run_checks([valid, {"id": "bad", "argv": "echo unsafe"}])
        self.assertEqual(result.returncode, 2)
        self.assertFalse(marker.exists())

    def test_arguments_are_not_interpreted_by_shell(self):
        marker = self.base / "shell-marker"
        literal = f"$(touch {marker})"
        command = {"id": "literal", "argv": [sys.executable, "-c", "import sys; print(sys.argv[1])", literal]}
        result, report = self.run_checks([command])
        self.assertEqual(result.returncode, 0)
        self.assertIn(literal, (self.output / report["checks"][0]["log"]).read_text())
        self.assertFalse(marker.exists())

    def test_path_escape_and_missing_inputs_are_rejected(self):
        for inputs in [["../outside"], ["missing"], []]:
            with self.subTest(inputs=inputs):
                result, report = self.run_checks([self.command("print('ok')")], inputs=inputs)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(report["status"], "configuration_error")

    def test_cwd_cannot_escape_project(self):
        result, report = self.run_checks([self.command("print('ok')", cwd="..")])
        self.assertEqual(result.returncode, 2)

    def test_invalid_required_flag_and_duplicate_ids_are_rejected(self):
        for checks in [[self.command("print('ok')", required="false")],
                       [self.command("print('one')"), self.command("print('two')")]]:
            with self.subTest(checks=checks):
                result, report = self.run_checks(checks)
                self.assertEqual(result.returncode, 2)

    def test_existing_evidence_is_not_overwritten(self):
        self.output.mkdir()
        sentinel = self.output / "result.json"
        sentinel.write_text('preserve old evidence')
        result, report = self.run_checks([self.command("print('ok')")])
        self.assertEqual(result.returncode, 2)
        self.assertEqual(sentinel.read_text(), 'preserve old evidence')

    def test_own_evidence_and_python_cache_do_not_stale_project(self):
        self.output = self.project / ".review-run"
        result, report = self.run_checks([self.command("from pathlib import Path; p=Path('src/__pycache__'); p.mkdir(); (p/'app.pyc').write_bytes(b'cache')")], inputs=["."])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(report["status"], "passed")

    def test_later_edits_make_saved_pass_stale_without_rerunning_command(self):
        counter = self.base / "counter"
        code = f"from pathlib import Path; p=Path({str(counter)!r}); p.write_text(str(int(p.read_text())+1) if p.exists() else '1')"
        result, _ = self.run_checks([self.command(code)])
        self.assertEqual(result.returncode, 0)
        (self.project / "src/app.py").write_text('value = 3\n')
        verified, report = self.verify()
        self.assertEqual(verified.returncode, 3)
        self.assertEqual(report["status"], "stale")
        self.assertEqual(counter.read_text(), '1')

    def test_verify_preserves_failure_and_original_report(self):
        result, _ = self.run_checks([self.command("raise SystemExit(1)")])
        self.assertEqual(result.returncode, 1)
        original = (self.output / "result.json").read_bytes()
        verified, report = self.verify()
        self.assertEqual(verified.returncode, 1)
        self.assertEqual(report["status"], "failed")
        self.assertEqual((self.output / "result.json").read_bytes(), original)

    def test_unchanged_saved_pass_is_current(self):
        result, _ = self.run_checks([self.command("print('ok')")])
        self.assertEqual(result.returncode, 0)
        verified, report = self.verify()
        self.assertEqual(verified.returncode, 0)
        self.assertEqual(report["status"], "passed")

    @unittest.skipUnless(os.name == "posix", "可执行权限在 POSIX 上验证")
    def test_executable_permission_change_invalidates_saved_pass(self):
        check = self.project / "check.sh"
        check.write_text("#!/bin/sh\nexit 0\n")
        check.chmod(0o755)
        checks = [{"id": "script", "argv": ["./check.sh"]}]
        result, _ = self.run_checks(checks, inputs=["check.sh"])
        self.assertEqual(result.returncode, 0)
        check.chmod(0o644)
        verified, report = self.verify()
        self.assertEqual(verified.returncode, 3)
        self.assertIn("check.sh", report["changed_inputs"])
        self.output = self.base / "rerun"
        rerun, _ = self.run_checks(checks, inputs=["check.sh"])
        self.assertEqual(rerun.returncode, 1)

    def test_deleted_empty_input_directory_invalidates_saved_pass(self):
        directory = self.project / "migrations"
        directory.mkdir()
        check = self.command("from pathlib import Path; assert Path('migrations').is_dir()")
        result, _ = self.run_checks([check], inputs=["migrations"])
        self.assertEqual(result.returncode, 0)
        directory.rmdir()
        verified, report = self.verify()
        self.assertEqual(verified.returncode, 3)
        self.assertIn("migrations", report["changed_inputs"])

    def test_created_evidence_ancestors_do_not_invalidate_project(self):
        self.output = self.project / "artifacts" / "review" / "run"
        result, report = self.run_checks([self.command("print('ok')")], inputs=["."])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(report["inputs_before"], report["inputs_after"])
        verified, _ = self.verify()
        self.assertEqual(verified.returncode, 0)
        (self.project / "artifacts/other.py").write_text("value = 1\n")
        changed, report = self.verify()
        self.assertEqual(changed.returncode, 3)
        self.assertIn("artifacts/other.py", report["changed_inputs"])

    @unittest.skipUnless(os.name == "posix", "文件符号链接在 POSIX 上验证")
    def test_retargeted_file_link_invalidates_recursive_and_explicit_inputs(self):
        script = "from pathlib import Path; assert Path(__file__).resolve().with_name('value.txt').read_text() == 'expected'\n"
        for name, value in [("one", "expected"), ("two", "different")]:
            directory = self.project / name
            directory.mkdir()
            (directory / "entry.py").write_text(script)
            (directory / "value.txt").write_text(value)
        link = self.project / "entry.py"
        checks = [{"id": "linked", "argv": [sys.executable, "-B", "entry.py"]}]
        for number, inputs in enumerate([["."], ["entry.py", "one", "two"]]):
            with self.subTest(inputs=inputs):
                if link.is_symlink():
                    link.unlink()
                link.symlink_to("one/entry.py")
                self.output = self.base / f"link-{number}"
                result, _ = self.run_checks(checks, inputs=inputs)
                self.assertEqual(result.returncode, 0)
                link.unlink()
                link.symlink_to("two/entry.py")
                verified, report = self.verify()
                self.assertEqual(verified.returncode, 3)
                self.assertIn("entry.py", report["changed_inputs"])
                self.output = self.base / f"link-rerun-{number}"
                rerun, _ = self.run_checks(checks, inputs=inputs)
                self.assertEqual(rerun.returncode, 1)


if __name__ == "__main__":
    unittest.main()
