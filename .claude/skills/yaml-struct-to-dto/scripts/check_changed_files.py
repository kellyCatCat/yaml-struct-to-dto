#!/usr/bin/env python3
"""改造后的 clean code 自检：检查改动过的 Java 文件。

用法:
    python3 check_changed_files.py --old IpranSummaryRsp,SummaryReq [--files a.java b.java] [--base HEAD] [--max-len 120]

不传 --files 时，自动取 `git diff --name-only <base>` 中的 .java 文件（含未暂存/已暂存改动）。
检查项：
  1. 残留的旧类型（代码、import、Javadoc 中按单词边界匹配）
  2. 使用了新 Dto 但缺少 import（改了名字漏改 import）
  3. 未使用的 import
  4. 重复的 import
  5. 新引入的通配符 import
  6. 本次新增行超长
任一检查不通过，退出码为 1。
"""
import argparse
import os
import re
import subprocess
import sys

IMPORT_RE = re.compile(r"^\s*import\s+(static\s+)?([\w.]+?)(\.\*)?\s*;\s*$")
COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False).stdout


def changed_java_files(base):
    names = set(git("diff", "--name-only", base).split()) | set(git("diff", "--name-only", "--cached", base).split())
    return sorted(n for n in names if n.endswith(".java"))


def added_lines(path, base):
    lines = []
    for line in git("diff", "-U0", base, "--", path).splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            lines.append(line[1:])
    return lines


def check_file(path, old_types, base, max_len, suffix):
    problems = []
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            src = f.read()
    except OSError:
        return problems  # 文件被删除

    lines = src.splitlines()
    seen = {}
    imports = []
    for no, line in enumerate(lines, 1):
        m = IMPORT_RE.match(line)
        if not m:
            continue
        key = line.strip()
        if key in seen:
            problems.append(f"{path}:{no}: 重复 import（首次出现在第 {seen[key]} 行）: {key}")
        seen[key] = no
        imports.append((no, m.group(1), m.group(2), m.group(3)))

    code = "\n".join(l for l in lines if not IMPORT_RE.match(l))
    code_no_comment = STRING_RE.sub('""', COMMENT_RE.sub(" ", code))

    added = set(l.strip() for l in added_lines(path, base))
    tag = lambda line: "（本次新增）" if line.strip() in added else "（存量）"

    for no, is_static, name, wildcard in imports:
        if wildcard:
            continue
        simple = name.rsplit(".", 1)[-1]
        # Javadoc 中的 {@link X} 也算使用，因此用含注释的 code 判断
        if not re.search(rf"\b{re.escape(simple)}\b", code):
            problems.append(f"{path}:{no}: 未使用的 import{tag(lines[no - 1])}: {name}")

    imported = {name.rsplit(".", 1)[-1] for _, s, name, w in imports if not s and not w}
    has_wildcard = any(w and not s for _, s, _, w in imports)
    same_dir = os.path.dirname(path)
    for t in old_types:
        new = t + suffix
        if not re.search(rf"\b{re.escape(new)}\b", code_no_comment):
            continue
        if new in imported or os.path.isfile(os.path.join(same_dir, new + ".java")):
            continue
        if re.search(rf"\b[a-z]\w*(\.\w+)*\.{re.escape(new)}\b", code_no_comment):
            continue  # 使用了全限定名
        hint = "（文件有通配符 import，请确认其包含该类）" if has_wildcard else ""
        problems.append(f"{path}: 使用了 {new} 但缺少 import{hint}")

    for t in old_types:
        pat = re.compile(rf"\b{re.escape(t)}\b")
        for no, line in enumerate(lines, 1):
            stripped = STRING_RE.sub('""', line)
            if pat.search(stripped):
                problems.append(f"{path}:{no}: 残留旧类型 {t}: {line.strip()}")

    for line in added_lines(path, base):
        m = IMPORT_RE.match(line)
        if m and m.group(3) and not m.group(1):
            problems.append(f"{path}: 新增了通配符 import: {line.strip()}")
        if len(line) > max_len:
            problems.append(f"{path}: 新增行超过 {max_len} 字符（{len(line)}）: {line.strip()[:80]}...")
    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--old", default="", help="逗号分隔的旧类型名，如 IpranSummaryRsp,SummaryReq")
    ap.add_argument("--files", nargs="*", help="要检查的 Java 文件，默认取 git diff 中的 .java 文件")
    ap.add_argument("--base", default="HEAD", help="对比基线，默认 HEAD")
    ap.add_argument("--suffix", default="Dto", help="DTO 后缀，默认 Dto")
    ap.add_argument("--max-len", type=int, default=120, help="行长度上限，默认 120")
    args = ap.parse_args()

    old_types = [t.strip() for t in args.old.split(",") if t.strip()]
    files = args.files if args.files else changed_java_files(args.base)
    if not files:
        print("没有需要检查的 Java 文件")
        return

    problems = []
    for f in files:
        problems.extend(check_file(f, old_types, args.base, args.max_len, args.suffix))

    print(f"检查了 {len(files)} 个文件")
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for p in problems:
            print(f"- {p}")
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
