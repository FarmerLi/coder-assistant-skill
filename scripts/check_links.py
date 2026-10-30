"""检查包内 Markdown 行内链接与 ATX 标题锚点，仅读取本地文件。

支持相对路径、百分号编码、尖括号目标及重复标题后缀；忽略代码和外部 URL。
不是完整 Markdown 解析器：不解析引用式链接、HTML 锚点或转义括号目标。
不验证远端可访问性、技能选择或代理行为。仅供维护者检查，不是运行依赖。
"""

import argparse
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


LINK = re.compile(r"!?\[[^\]\n]*\]\(\s*(?:<([^>\n]+)>|([^\s)]+))(?:\s+\"[^\"\n]*\")?\s*\)")
INLINE_CODE = re.compile(r"(`+).*?\1")
FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")


def prose_lines(content):
    """保留真实行号，排除围栏内容。"""
    fence = None
    for number, line in enumerate(content.splitlines(), 1):
        match = FENCE.match(line)
        if match:
            marker, suffix = match.groups()
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence) and not suffix.strip():
                fence = None
            continue
        if fence is None:
            yield number, line


def anchors(content):
    result = set()
    for _, line in prose_lines(content):
        match = re.match(r"^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if not match:
            continue
        heading = match.group(1).lower()
        base = "".join(c for c in heading if c.isalnum() or c in " _-").replace(" ", "-")
        slug = base
        suffix = 0
        while slug in result:
            suffix += 1
            slug = f"{base}-{suffix}"
        result.add(slug)
    return result


def check_package(root):
    root = Path(root).resolve()
    if not root.is_dir():
        return [f"技能包目录不存在：{root}"]
    errors = []
    cached = {}
    for source in sorted(root.rglob("*.md")):
        if source.is_symlink() or any(p in {".git", "node_modules", "__pycache__"} for p in source.relative_to(root).parts):
            continue
        content = source.read_text(encoding="utf-8")
        for number, line in prose_lines(content):
            for match in LINK.finditer(INLINE_CODE.sub("", line)):
                raw = match.group(1) or match.group(2)
                parts = urlsplit(raw)
                if parts.scheme or parts.netloc:
                    continue
                path = unquote(parts.path)
                target = (source.parent / path).resolve() if path else source.resolve()
                prefix = f"{source.relative_to(root)}:{number}: {raw}"
                if not target.is_relative_to(root):
                    errors.append(f"{prefix}：目标超出技能包")
                elif not target.is_file():
                    errors.append(f"{prefix}：目标文件不存在")
                elif parts.fragment and target.suffix.lower() == ".md":
                    if target not in cached:
                        cached[target] = anchors(target.read_text(encoding="utf-8"))
                    if unquote(parts.fragment) not in cached[target]:
                        errors.append(f"{prefix}：标题锚点不存在")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args()
    errors = check_package(args.root)
    for error in errors:
        print(error)
    if not errors:
        print("包内引用检查通过（限脚本支持的语法）。")
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())
