"""维护者演练：隔离复制订单夹具，观察自动检查与需求验证的差别。

用法：python3 场景演练.py 技能包目录 尚不存在的演练目录
不会修改技能包或原始夹具。只使用标准库，不调用模型或远端服务。
"""

from datetime import datetime, timezone
import difflib
import json
from pathlib import Path
import shutil
import subprocess
import sys


EXTRA_TESTS = '''from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from order_app.api import OrderAPI
from order_app.domain import Order
from order_app.services import OrderService
from order_app.store import MemoryOrderStore


class CancelledOrderContractTests(unittest.TestCase):
    def setUp(self):
        self.order = Order("C1", "alice", "cancelled")
        self.store = MemoryOrderStore([self.order])
        self.api = OrderAPI(OrderService(self.store))

    def test_stranger_cannot_cancel_already_cancelled_order(self):
        with self.assertRaises(PermissionError):
            self.api.cancel_order("C1", "bob")
        self.assertEqual(self.store.get("C1"), self.order)
        self.assertEqual(self.store.save_count, 0)

    def test_owner_repeat_is_allowed_without_write(self):
        self.assertEqual(self.api.cancel_order("C1", "alice"),
                         {"id": "C1", "status": "cancelled"})
        self.assertEqual(self.store.get("C1"), self.order)
        self.assertEqual(self.store.save_count, 0)
'''


def exercise(package, output):
    output.mkdir(parents=True, exist_ok=False)
    project = output / "project"
    shutil.copytree(package / "tests/fixtures/order_project", project,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
    shutil.copyfile(package / "tests/验证证据/接口用例.py", project / "tests/test_api.py")
    api = project / "order_app/api.py"
    api.write_text(api.read_text().replace(
        'raise NotImplementedError("取消入口尚未接入")',
        'order = self.service.cancel(order_id, actor_id)\n'
        '        return {"id": order.id, "status": order.status}'), encoding="utf-8")
    config = output / "checks.json"
    shutil.copyfile(package / "assets/自动检查配置示例.json", config)
    original_config = config.read_bytes()
    runner = package / "scripts/run_review_checks.py"
    records = []

    def command(name, arguments, expected_code, expected_status):
        argv = [sys.executable, "-B", str(runner), "--project", str(project)] + arguments
        result = subprocess.run(argv, capture_output=True, text=True, timeout=90)
        (output / f"{name}.stdout.json").write_text(result.stdout, encoding="utf-8")
        (output / f"{name}.stderr.txt").write_text(result.stderr, encoding="utf-8")
        report = json.loads(result.stdout)
        records.append({"name": name, "argv": argv, "exit_code": result.returncode,
                        "status": report["status"]})
        assert result.returncode == expected_code, records[-1]
        assert report["status"] == expected_status, records[-1]
        return report

    def run(name, code=0, status="passed"):
        return command(name, ["--config", str(config), "--output", str(output / name)], code, status)

    def verify(name, source, code=0, status="passed"):
        return command(name, ["--verify", str(output / source / "result.json")], code, status)

    run("clean-13")
    verify("clean-current", "clean-13")

    service = project / "order_app/services.py"
    correct = service.read_text(encoding="utf-8")
    guard = '        if order.owner != actor_id:\n            raise PermissionError("只有订单所有者可以取消")\n'
    repeat = '        if order.status == "cancelled":\n            return order\n'
    assert guard + repeat in correct
    broken = correct.replace(guard + repeat, repeat + guard)
    (output / "injected-defect.patch").write_text("".join(difflib.unified_diff(
        correct.splitlines(keepends=True), broken.splitlines(keepends=True),
        fromfile="correct/order_app/services.py", tofile="broken/order_app/services.py")), encoding="utf-8")
    service.write_text(broken, encoding="utf-8")
    verify("old-code-stale", "clean-13", 3, "stale")
    run("missed-defect-13")

    (project / "tests/test_cancelled_contract.py").write_text(EXTRA_TESTS, encoding="utf-8")
    verify("added-test-stale", "missed-defect-13", 3, "stale")
    run("confirmed-defect-15", 1, "failed")
    service.write_text(correct, encoding="utf-8")
    verify("repair-stale", "confirmed-defect-15", 3, "stale")
    run("repaired-15")
    verify("repaired-current", "repaired-15")

    changed_config = json.loads(original_config)
    changed_config["checks"][0]["timeout_seconds"] += 1
    config.write_text(json.dumps(changed_config), encoding="utf-8")
    changed_report = verify("changed-config-stale", "repaired-15", 3, "stale")
    assert changed_report["config_changed"] is True
    config.write_bytes(original_config)
    verify("restored-config-current", "repaired-15")

    summary = {"method": "maintainer_exercise_not_independent_agent",
               "finished_at_utc": datetime.now(timezone.utc).isoformat(),
               "package": str(package), "output": str(output), "records": records}
    (output / "exercise.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    exercise(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
