#!/usr/bin/env python3
"""扫描 Java 文件中引用的类型，找出需要改成 DTO 的 yaml 生成结构体。

用法:
    python3 scan_dto_candidates.py <Java文件或类名> [...] [--root 仓库根目录] [--depth N] [--json]

对每个被引用的类型 T，判断：
  - 仓库里是否已经存在 T + "Dto" 的类           -> 需要替换（READY）
  - T 在 yaml 中定义但还没有对应的 Dto          -> 需要确认/新建（MISSING_DTO）
同时列出被引用的“项目内业务类”（Service/Helper/Converter 等），
用于沿调用链继续排查；--depth N 会自动向下追 N 层。
"""
import argparse
import json
import os
import re
import sys
from collections import defaultdict, deque

SKIP_DIRS = {".git", "target", "build", "out", "node_modules", ".idea", ".gradle", "bin"}
YAML_SCHEMA_RE = re.compile(r"^\s{2,}([A-Z][A-Za-z0-9_]*):\s*$")
TYPE_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]*)\b")
IMPORT_RE = re.compile(r"^\s*import\s+(static\s+)?([\w.]+)(\.\*)?\s*;", re.M)
PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)
COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
CONST_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def strip_code(src):
    """去掉注释和字符串字面量，避免误判。Javadoc 里的 {@link X} 由人工 grep 兜底。"""
    src = COMMENT_RE.sub(" ", src)
    return STRING_RE.sub('""', src)


def build_index(root, suffix):
    java_defs = defaultdict(list)
    yaml_defs = defaultdict(set)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            path = os.path.join(dirpath, name)
            if name.endswith(".java"):
                java_defs[name[:-5]].append(path)
            elif name.endswith((".yaml", ".yml")):
                try:
                    with open(path, encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            m = YAML_SCHEMA_RE.match(line)
                            if m and not m.group(1).endswith(suffix):
                                yaml_defs[m.group(1)].add(path)
                except OSError:
                    pass
    return java_defs, yaml_defs


def resolve_inputs(items, java_defs):
    files = []
    for item in items:
        if os.path.isfile(item):
            files.append(os.path.abspath(item))
            continue
        name = item[:-5] if item.endswith(".java") else item
        paths = java_defs.get(name)
        if not paths:
            sys.exit(f"[ERROR] 找不到 Java 文件或类: {item}")
        if len(paths) > 1:
            print(f"[WARN] {name} 有多个同名文件，全部纳入扫描: {paths}", file=sys.stderr)
        files.extend(os.path.abspath(p) for p in paths)
    return files


def scan_file(path):
    with open(path, encoding="utf-8", errors="ignore") as f:
        src = f.read()
    pkg = PACKAGE_RE.search(src)
    imports = {}
    wildcard = []
    for m in IMPORT_RE.finditer(src):
        if m.group(1):
            continue
        if m.group(3):
            wildcard.append(m.group(2))
        else:
            imports[m.group(2).rsplit(".", 1)[-1]] = m.group(2)
    body = IMPORT_RE.sub(" ", strip_code(src))
    body = PACKAGE_RE.sub(" ", body)
    # 排除常量（全大写）和单字母泛型参数
    types = {t for t in TYPE_RE.findall(body) if len(t) > 1 and not CONST_RE.match(t)}
    return {
        "package": pkg.group(1) if pkg else "",
        "imports": imports,
        "wildcard_imports": wildcard,
        "types": types,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("targets", nargs="+", help="Java 文件路径或类名（如 OpsIpranSummaryTunnelFacadeImpl）")
    ap.add_argument("--root", default=".", help="仓库根目录，默认当前目录")
    ap.add_argument("--suffix", default="Dto", help="DTO 后缀，默认 Dto")
    ap.add_argument("--depth", type=int, default=0, help="沿项目内业务类自动向下追踪的层数，默认 0")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    java_defs, yaml_defs = build_index(root, args.suffix)
    start = resolve_inputs(args.targets, java_defs)

    rel = lambda p: os.path.relpath(p, root)
    visited = {}
    queue = deque((p, 0) for p in start)
    ready = defaultdict(set)        # 旧类型 -> 引用它的文件
    missing = defaultdict(set)      # yaml 有定义但无 Dto
    done = defaultdict(set)         # 已经是 Dto 的类型
    internal = defaultdict(set)     # 项目内业务类 -> 引用它的文件
    wildcard_warn = {}

    while queue:
        path, level = queue.popleft()
        if path in visited:
            continue
        info = scan_file(path)
        visited[path] = level
        if info["wildcard_imports"]:
            wildcard_warn[rel(path)] = info["wildcard_imports"]
        for t in sorted(info["types"]):
            if t.endswith(args.suffix) and t in java_defs:
                done[t].add(rel(path))
                continue
            dto = t + args.suffix
            if dto in java_defs:
                ready[t].add(rel(path))
            elif t in yaml_defs:
                missing[t].add(rel(path))
            elif t in java_defs and os.path.abspath(java_defs[t][0]) != path:
                for p in java_defs[t]:
                    internal[rel(p)].add(rel(path))
                    if level < args.depth:
                        queue.append((os.path.abspath(p), level + 1))

    result = {
        "scanned_files": {rel(p): lvl for p, lvl in visited.items()},
        "ready": {t: {"dto": t + args.suffix,
                      "dto_paths": [rel(p) for p in java_defs[t + args.suffix]],
                      "old_paths": [rel(p) for p in java_defs.get(t, [])],
                      "yaml": sorted(rel(p) for p in yaml_defs.get(t, [])),
                      "used_in": sorted(f)} for t, f in sorted(ready.items())},
        "missing_dto": {t: {"yaml": sorted(rel(p) for p in yaml_defs[t]), "used_in": sorted(f)}
                        for t, f in sorted(missing.items())},
        "already_dto": {t: sorted(f) for t, f in sorted(done.items())},
        "internal_classes": {p: sorted(f) for p, f in sorted(internal.items())},
        "wildcard_imports": wildcard_warn,
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    print(f"## 已扫描文件（{len(visited)} 个，数字为调用层级）")
    for p, lvl in sorted(result["scanned_files"].items(), key=lambda x: (x[1], x[0])):
        print(f"- [{lvl}] {p}")
    print(f"\n## READY：已存在 Dto，需要替换（{len(result['ready'])} 个）")
    print("| 旧类型 | 新类型 | Dto 位置 | 引用文件 |")
    print("|---|---|---|---|")
    for t, v in result["ready"].items():
        print(f"| {t} | {v['dto']} | {'<br>'.join(v['dto_paths'])} | {'<br>'.join(v['used_in'])} |")
    print(f"\n## MISSING_DTO：yaml 中有定义但没有 Dto（{len(result['missing_dto'])} 个，需确认是否新建）")
    for t, v in result["missing_dto"].items():
        print(f"- {t}  yaml={v['yaml']}  used_in={v['used_in']}")
    print(f"\n## 已经是 Dto 的类型（{len(result['already_dto'])} 个）")
    print(", ".join(result["already_dto"]) or "-")
    print(f"\n## 项目内被引用的业务类（{len(result['internal_classes'])} 个，需沿调用链判断是否继续排查）")
    for p, f in result["internal_classes"].items():
        print(f"- {p}  <- {', '.join(f)}")
    if wildcard_warn:
        print("\n## 注意：以下文件存在通配符 import，替换后需人工确认 import")
        for p, w in wildcard_warn.items():
            print(f"- {p}: {', '.join(w)}")


if __name__ == "__main__":
    main()
