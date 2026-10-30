"""批量运行项目已有检查，或核实旧证据是否仍对应当前输入。

仅使用标准库。配置格式见 references/workflows/自动检查.md。
退出码：0 自动检查通过（可能有非必需项警告），1 必需项失败，
2 配置／证据错误，3 输入已变化。结果不代表人工或模型审查通过。
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time


IGNORED_DIRS = {".git", "node_modules", ".venv", "__pycache__"}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def project_path(root, value):
    if not isinstance(value, str) or not value:
        raise ValueError("项目路径须为非空字符串")
    path = (root / value).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"路径超出项目：{value}")
    return path


def snapshot(root, inputs, output, output_created_parents=()):
    manifest = {}
    ignored_directory_metadata = {Path(value) for value in output_created_parents}

    def visit(path):
        if path == output or path.is_relative_to(output):
            return
        if path.name in IGNORED_DIRS or path.suffix in {".pyc", ".pyo"}:
            return
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(f"输入符号链接超出项目：{path}")
        name = path.relative_to(root).as_posix() or "."
        if path.is_symlink() and path.is_dir():
            raise ValueError(f"目录符号链接需在 inputs 中显式选择真实目录：{name}")
        if not path.exists():
            manifest[name] = {"type": "missing"}
        elif path.is_dir():
            if path not in ignored_directory_metadata:
                manifest[name] = {"type": "directory", "mode": stat.S_IMODE(path.stat().st_mode)}
            for child in sorted(path.iterdir()):
                visit(child)
        elif path.is_file():
            manifest[name] = {"type": "file", "sha256": digest(path.read_bytes()),
                              "mode": stat.S_IMODE(path.stat().st_mode)}
        else:
            raise ValueError(f"输入不是普通文件或目录：{name}")
        if path.is_symlink():
            manifest[name]["symlink_target"] = os.readlink(path)

    for value in inputs:
        project_path(root, value)
        path = root / value
        if not path.is_relative_to(root):
            raise ValueError(f"输入路径须位于项目目录内：{value}")
        for parent in path.parents:
            if parent == root or not parent.is_relative_to(root):
                break
            if parent.is_symlink():
                raise ValueError(f"输入经过目录符号链接，须选择真实目录：{value}")
        visit(Path(os.path.abspath(path)))
    return dict(sorted(manifest.items()))


def validate(config, root):
    if not root.is_dir():
        raise ValueError("项目目录不存在")
    if not isinstance(config, dict) or set(config) != {"inputs", "checks"}:
        raise ValueError("配置须包含且仅包含 inputs 和 checks")
    inputs, checks = config["inputs"], config["checks"]
    if not isinstance(inputs, list) or not inputs:
        raise ValueError("inputs 须为非空项目路径列表")
    for value in inputs:
        if not project_path(root, value).exists():
            raise ValueError(f"输入不存在：{value}")
    if not isinstance(checks, list) or not checks:
        raise ValueError("checks 不能为空；没有执行检查不能报告通过")
    seen = set()
    for check in checks:
        if not isinstance(check, dict) or set(check) - {"id", "argv", "cwd", "required", "timeout_seconds"}:
            raise ValueError("检查项包含不支持的字段")
        name, argv = check.get("id"), check.get("argv")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", name) or name in seen:
            raise ValueError("检查 id 须为不重复的小写英文标识")
        seen.add(name)
        if not isinstance(argv, list) or not argv or any(not isinstance(v, str) or not v or "\x00" in v for v in argv):
            raise ValueError(f"{name}: argv 须为非空参数数组，不接受 shell 命令字符串")
        if not isinstance(check.get("required", True), bool):
            raise ValueError(f"{name}: required 须为布尔值")
        timeout = check.get("timeout_seconds", 120)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError(f"{name}: timeout_seconds 须为正数")
        if not project_path(root, check.get("cwd", ".")).is_dir():
            raise ValueError(f"{name}: cwd 目录不存在")
    return inputs, checks


def run_one(check, root, output):
    started = time.monotonic()
    result = {"id": check["id"], "argv": check["argv"],
              "cwd": str(project_path(root, check.get("cwd", "."))),
              "required": check.get("required", True),
              "timeout_seconds": check.get("timeout_seconds", 120),
              "started_at_utc": timestamp(), "log": check["id"] + ".log"}
    with (output / result["log"]).open("wb") as log:
        try:
            process = subprocess.Popen(check["argv"], cwd=result["cwd"], stdout=log,
                                       stderr=subprocess.STDOUT, shell=False,
                                       start_new_session=os.name == "posix")
            try:
                result["exit_code"] = process.wait(timeout=result["timeout_seconds"])
                result["status"] = "passed" if process.returncode == 0 else "failed"
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                process.wait()
                result.update(status="timeout", exit_code=process.returncode)
                log.write(b"\n[runner] check timed out\n")
        except OSError as error:
            result.update(status="error", exit_code=None, error=str(error))
            log.write(str(error).encode("utf-8", errors="replace"))
    result.update(finished_at_utc=timestamp(), duration_seconds=round(time.monotonic() - started, 3))
    return result


def changed_paths(before, after):
    return sorted(key for key in before.keys() | after.keys()
                  if key not in before or key not in after or before[key] != after[key])


def state(root, inputs, output, config_path, config_hash, before, output_created_parents):
    after = snapshot(root, inputs, output, output_created_parents)
    config_changed = not config_path.is_file() or digest(config_path.read_bytes()) != config_hash
    changed = changed_paths(before, after)
    return after, config_changed, changed


def exit_code(status):
    return {"passed": 0, "passed_with_warnings": 0, "failed": 1, "configuration_error": 2, "stale": 3}[status]


def run_checks(root, config_path, output):
    data = config_path.read_bytes()
    config = json.loads(data)
    inputs, checks = validate(config, root)
    output_created_parents = []
    parent = output.parent
    while parent.is_relative_to(root) and not parent.exists():
        output_created_parents.append(str(parent))
        parent = parent.parent
    before = snapshot(root, inputs, output, output_created_parents)
    output.mkdir(parents=True, exist_ok=False)
    report = {"schema_version": 2, "scope": "automated_checks_only", "review": "not_assessed",
              "project": str(root), "config_path": str(config_path), "config_sha256": digest(data),
              "output": str(output), "output_created_parents": output_created_parents,
              "input_paths": inputs, "inputs_before": before,
              "started_at_utc": timestamp(), "python": sys.version,
              "checks": []}
    for check in checks:
        report["checks"].append(run_one(check, root, output))
    try:
        after, config_changed, changed = state(root, inputs, output, config_path, digest(data), before,
                                              output_created_parents)
        stale = bool(config_changed or changed)
        report.update(inputs_after=after, config_changed=config_changed, changed_inputs=changed)
    except (OSError, ValueError) as error:
        stale = True
        report.update(inputs_after=None, state_error=str(error))
    failed = any(c["required"] and c["status"] != "passed" for c in report["checks"])
    warning = any(c["status"] != "passed" for c in report["checks"])
    status = "failed" if failed else "passed_with_warnings" if warning else "passed"
    report.update(status="stale" if stale else status, finished_at_utc=timestamp())
    (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def verify_saved(root, path):
    report = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(report, dict) or report.get("schema_version") != 2
            or report.get("scope") != "automated_checks_only" or Path(report["project"]).resolve() != root
            or report.get("status") not in {"passed", "passed_with_warnings", "failed", "stale"}):
        raise ValueError("不是本项目支持的检查证据")
    inputs = report["input_paths"]
    if not isinstance(inputs, list) or not inputs or not isinstance(report["inputs_before"], dict):
        raise ValueError("证据缺少输入范围或摘要")
    if not isinstance(report["checks"], list) or not report["checks"]:
        raise ValueError("证据没有实际检查项")
    after, config_changed, changed = state(root, inputs, Path(report["output"]),
                                          Path(report["config_path"]), report["config_sha256"],
                                          report["inputs_before"], report["output_created_parents"])
    status = "stale" if config_changed or changed else report["status"]
    return {"schema_version": 2, "scope": "automated_checks_only", "review": "not_assessed",
            "status": status, "evidence": str(path), "checked_at_utc": timestamp(),
            "changed_inputs": changed, "config_changed": config_changed}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.verify and not args.config and not args.output:
            report = verify_saved(args.project.resolve(), args.verify.resolve())
        elif not args.verify and args.config and args.output:
            report = run_checks(args.project.resolve(), args.config.resolve(), args.output.resolve())
        else:
            raise ValueError("运行须提供 --config 与 --output；核实旧证据仅提供 --verify")
    except (OSError, ValueError, KeyError, TypeError) as error:
        report = {"status": "configuration_error", "error": str(error)}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return exit_code(report["status"])


if __name__ == "__main__":
    raise SystemExit(main())
