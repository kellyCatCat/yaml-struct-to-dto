#!/usr/bin/env python3
"""DTO 改造后的自检：检查改动过的 Java 文件。

用法:
    python3 check_changed_files.py --old IpranSummaryRsp,SummaryReq [--files a.java b.java]
                                   [--base HEAD] [--root 仓库根目录] [--max-len 120]

不传 --files 时，自动取 `git diff --name-only <base>` 中的 .java 文件（含未暂存/已暂存改动）。

检查分三大类：
  一、import
     - 未使用的 import
     - 使用了新 Dto 但缺少 import（改了名字漏改 import）
     - 重复的 import、新增的通配符 import
  二、残留的旧模型引用
     - 改动文件中残留的旧类型（代码、import、Javadoc 中按单词边界匹配）
  三、方法调用不匹配
     - 调用了 Dto 上不存在的方法（含 getter/setter、builder 链、方法引用 XxxDto::getYyy），
       会识别 Lombok 的 @Data/@Getter/@Setter/@Builder 等注解
     - 调用的方法签名仍在使用旧类型（例如 Service 方法没改，Facade 却传入了 Dto）
     - 调用了被调用类中不存在的方法
     - @Override 方法和接口/父类的签名不一致（例如只改了 Impl 没改接口，或者反过来）
     - 方法签名已改为 Dto，但未改动的调用方文件还在调用它
  另外检查本次新增的超长行。

输出分为【错误】和【警告】两级：
  - 错误：确定的问题，必须修复。存在错误时退出码为 1。
  - 警告：基于静态分析的疑似问题，需要逐条确认：是问题就修复，是误报就在报告中说明原因。
本脚本只做静态分析，不能替代编译；最终以 `mvn clean install` 的结果为准。
"""
import argparse
import os
import re
import subprocess
import sys
from collections import defaultdict

SKIP_DIRS = {".git", "target", "build", "out", "node_modules", ".idea", ".gradle", "bin"}
IMPORT_RE = re.compile(r"^\s*import\s+(static\s+)?([\w.$]+?)(\.\*)?\s*;\s*$")
KEYWORDS = {
    "abstract", "assert", "boolean", "break", "byte", "case", "catch", "char", "class", "const",
    "continue", "default", "do", "double", "else", "enum", "extends", "final", "finally", "float",
    "for", "goto", "if", "implements", "import", "instanceof", "int", "interface", "long", "native",
    "new", "package", "private", "protected", "public", "return", "short", "static", "strictfp",
    "super", "switch", "synchronized", "this", "throw", "throws", "transient", "try", "void",
    "volatile", "while", "yield", "var", "record", "sealed", "permits",
}
PRIMITIVES = {"boolean", "byte", "char", "short", "int", "long", "float", "double", "void"}
OBJECT_METHODS = {"equals", "hashCode", "toString", "getClass", "notify", "notifyAll", "wait", "clone", "finalize"}
ENUM_METHODS = {"values", "valueOf", "name", "ordinal", "compareTo", "getDeclaringClass", "describeConstable"}
KNOWN_EXTERNAL = {"Object", "Serializable", "Cloneable", "Comparable"}


# ---------------------------------------------------------------- 词法与解析

def strip_java(src, keep_comments=False):
    """把字符串/字符字面量的内容替换为空格；keep_comments=False 时注释也替换为空格。
    保持长度和换行不变，偏移量和行号都可以直接对应回源文件。"""
    out = []
    i, n = 0, len(src)
    blank = lambda s: re.sub(r"[^\n]", " ", s)
    while i < n:
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            out.append(src[i:j] if keep_comments else blank(src[i:j]))
            i = j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(src[i:j] if keep_comments else blank(src[i:j]))
            i = j
        elif src.startswith('"""', i):
            j = src.find('"""', i + 3)
            j = n if j < 0 else j + 3
            out.append('"' + blank(src[i + 1:j - 1]) + '"')
            i = j
        elif src[i] in "\"'":
            q, j = src[i], i + 1
            while j < n and src[j] != q and src[j] != "\n":
                j += 2 if src[j] == "\\" else 1
            j = min(j + 1, n)
            out.append(q + blank(src[i + 1:j - 1]) + q if j - i >= 2 else src[i:j])
            i = j
        else:
            out.append(src[i])
            i += 1
    return "".join(out)


def match_paren(code, i):
    """code[i] 是 '('，返回与之匹配的 ')' 的下标。"""
    depth = 0
    for j in range(i, len(code)):
        if code[j] == "(":
            depth += 1
        elif code[j] == ")":
            depth -= 1
            if depth == 0:
                return j
    return None


def split_top(s):
    parts, depth, cur = [], 0, []
    for ch in s:
        if ch in "<([{":
            depth += 1
        elif ch in ">)]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur))
    return parts


def norm_type(t):
    t = re.sub(r"\s+", "", t).replace("...", "[]")
    return re.sub(r"\b[a-z_][\w$]*\.", "", t)  # 去掉包名


def param_types(params):
    types = []
    for p in split_top(params):
        p = re.sub(r"@[\w$.]+(\s*\((?:[^()]|\([^()]*\))*\))?", " ", p)
        p = re.sub(r"\bfinal\b", " ", p).strip()
        m = re.search(r"([\w$]+)\s*(\[\s*\])*\s*$", p)
        if not m:
            continue
        types.append(norm_type(p[:m.start()]))
    return types


def scan_type_back(code, k):
    """code[k] 是方法名前面的最后一个非空白字符，向前扫出类型 token 的起始下标。"""
    j = k
    while j >= 1 and code[j] == "]":
        j -= 1
        while j >= 0 and code[j].isspace():
            j -= 1
        if j < 0 or code[j] != "[":
            return None
        j -= 1
        while j >= 0 and code[j].isspace():
            j -= 1
    if j >= 0 and code[j] == ">":
        if j >= 1 and code[j - 1] == "-":
            return None  # lambda 箭头
        depth = 0
        while j >= 0:
            if code[j] == ">":
                depth += 1
            elif code[j] == "<":
                depth -= 1
                if depth == 0:
                    break
            elif code[j] in ";{}()=":
                return None
            j -= 1
        j -= 1
        while j >= 0 and code[j].isspace():
            j -= 1
    end = j
    while j >= 0 and (code[j].isalnum() or code[j] in "_$."):
        j -= 1
    if j == end or code[j + 1].isdigit():
        return None
    return j + 1


NAME_PAREN_RE = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")


def find_method_decls(code):
    """返回 [(方法名, 返回类型, 参数类型列表, 方法名偏移, 类型起始偏移)]。"""
    decls = []
    for m in NAME_PAREN_RE.finditer(code):
        name, s = m.group(1), m.start(1)
        if name in KEYWORDS:
            continue
        k = s - 1
        while k >= 0 and code[k].isspace():
            k -= 1
        if k < 0 or code[k] in ".@":
            continue
        tstart = scan_type_back(code, k)
        if tstart is None:
            continue
        ttext = code[tstart:k + 1]
        base = re.match(r"[\w$.]+", ttext).group(0)
        if base in KEYWORDS - PRIMITIVES:
            continue
        p = tstart - 1
        while p >= 0 and code[p].isspace():
            p -= 1
        if p >= 0 and code[p] in ".=!+-*/%?:&|^~<":
            continue
        close = match_paren(code, m.end() - 1)
        if close is None:
            continue
        if not re.match(r"\s*(throws\s+[\w$.,\s<>]+?)?\s*[{;]", code[close + 1:close + 400]):
            continue
        decls.append((name, norm_type(ttext), param_types(code[m.end():close]), s, tstart))
    return decls


def depth_array(code):
    depth, arr = 0, []
    for ch in code:
        arr.append(depth)
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
    return arr


TYPE_DECL_RE = re.compile(r"\b(class|interface|enum|record)\s+([\w$]+)")
FIELD_RE = re.compile(
    r"(?:^|(?<=[;{}]))\s*((?:@[\w$.]+(?:\s*\((?:[^()]|\([^()]*\))*\))?\s*)*)"
    r"((?:(?:public|protected|private|static|final|transient|volatile)\s+)*)"
    r"([\w$.]+(?:\s*<[^;=(){}]*>)?(?:\s*\[\s*\])*)\s+([\w$]+)\s*(?:=[^;]*)?;")


class JClass:
    def __init__(self, path, src):
        self.path = path
        self.code = strip_java(src)
        code = self.code
        depth = depth_array(code)
        self.kind = self.name = None
        self.tvars, self.supers, self.extends = set(), [], []
        self.annos = ""
        self.methods = []   # (name, ret, ptypes, line, is_override)
        self.fields = []    # (type, name, is_static)
        self.body_text = ""
        for m in TYPE_DECL_RE.finditer(code):
            if depth[m.start()] != 0:
                continue
            self.kind, self.name = m.group(1), m.group(2)
            brace = code.find("{", m.end())
            if brace < 0:
                return
            header = code[m.end():brace]
            prev = max(code.rfind(";", 0, m.start()), 0)
            self.annos = code[prev:m.start()]
            tp = re.match(r"\s*<(.*?)>\s*(?=extends|implements|permits|\(|$)", header, re.S)
            if tp:
                self.tvars = {re.match(r"\s*([\w$]+)", x).group(1) for x in split_top(tp.group(1))}
                header = header[tp.end():]
            for kw, rest in re.findall(r"\b(extends|implements)\s+(.*?)(?=\bextends\b|\bimplements\b|\bpermits\b|$)",
                                       header, re.S):
                names = [re.sub(r"<.*", "", x).strip().split(".")[-1] for x in split_top(rest)]
                self.supers.extend(n for n in names if n)
                if kw == "extends" and self.kind == "class":
                    self.extends.extend(n for n in names if n)
            self.body_text = "".join(ch if depth[i] == 1 else " " for i, ch in enumerate(code))
            for name, ret, ptypes, s, tstart in find_method_decls(code):
                if depth[s] != 1:
                    continue
                lead = code[max(code.rfind(";", 0, tstart), code.rfind("}", 0, tstart),
                                code.rfind("{", 0, tstart)):tstart]
                self.methods.append((name, ret, ptypes, code.count("\n", 0, s) + 1, "@Override" in lead))
            if self.kind in ("class", "enum"):
                for f in FIELD_RE.finditer(self.body_text):
                    ftype, fname = f.group(3), f.group(4)
                    if ftype in KEYWORDS - PRIMITIVES or fname in KEYWORDS:
                        continue
                    self.fields.append((norm_type(ftype), fname, "static" in f.group(2)))
            return


class RepoIndex:
    def __init__(self, root):
        self.root = root
        self.paths = defaultdict(list)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                if fn.endswith(".java"):
                    self.paths[fn[:-5]].append(os.path.join(dirpath, fn))
        self._cache = {}
        self._text = {}

    def all_files(self):
        for paths in self.paths.values():
            yield from paths

    def text(self, path):
        if path not in self._text:
            try:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    self._text[path] = f.read()
            except OSError:
                self._text[path] = ""
        return self._text[path]

    def parse(self, path):
        path = os.path.abspath(path)
        if path not in self._cache:
            self._cache[path] = JClass(path, self.text(path))
        return self._cache[path]

    def get(self, name):
        """按简单类名取类；同名多个时返回列表中全部。"""
        return [self.parse(p) for p in self.paths.get(name, [])]


# ---------------------------------------------------------------- 方法集合

def lombok_methods(cls):
    annos = cls.annos + " " + cls.body_text
    names, complete = set(), True
    has = lambda a: re.search(rf"@(lombok\.)?{a}\b", annos) is not None
    getter = has("Data") or has("Getter") or has("Value")
    setter = has("Data") or has("Setter")
    fluent = re.search(r"@(lombok\.experimental\.)?Accessors\s*\([^)]*fluent\s*=\s*true", annos) is not None
    for ftype, fname, is_static in cls.fields:
        if is_static:
            continue
        cap = fname[:1].upper() + fname[1:]
        if fluent:
            names.add(fname)
            continue
        if getter:
            names.update({"get" + cap, "is" + cap})
            if re.match(r"is[A-Z]", fname):
                names.add(fname)
        if setter:
            names.add("set" + cap)
            if re.match(r"is[A-Z]", fname):
                names.add("set" + fname[2:])
    if has("Builder") or has("SuperBuilder"):
        names.update({"builder", "toBuilder"})
    if has("Data") or has("Value") or has("EqualsAndHashCode") or has("ToString"):
        names.update({"equals", "hashCode", "toString", "canEqual"})
    if has("Delegate") or has("ExtensionMethod") or re.search(r"@(lombok\.experimental\.)?Accessors\s*\([^)]*prefix", annos):
        complete = False
    return names, complete


def method_set(idx, name, _seen=None):
    """返回 (方法名集合, 方法声明列表[(所在类, decl)], 是否完整解析, 类型变量集合)。"""
    _seen = _seen or set()
    classes = idx.get(name)
    if not classes or name in _seen:
        return set(), [], name in KNOWN_EXTERNAL, set()
    _seen.add(name)
    names, decls, complete, tvars = set(OBJECT_METHODS), [], len(classes) == 1, set()
    for cls in classes:
        if cls.kind is None or cls.kind == "record":
            complete = False
            continue
        tvars |= cls.tvars
        names.update(m[0] for m in cls.methods)
        decls.extend((cls, m) for m in cls.methods)
        ln, lc = lombok_methods(cls)
        names |= ln
        complete &= lc
        if cls.kind == "enum":
            names |= ENUM_METHODS
        for sup in cls.supers:
            if sup in KNOWN_EXTERNAL:
                continue
            sn, sd, sc, st = method_set(idx, sup, _seen)
            names |= sn
            decls.extend(sd)
            tvars |= st
            if not sc and (sup in cls.extends or idx.get(sup)):
                complete = False
    return names, decls, complete, tvars


def builder_fields(idx, name, _seen=None):
    _seen = _seen or set()
    names, complete = {"build"}, True
    for cls in idx.get(name):
        if name in _seen:
            break
        _seen.add(name)
        if re.search(r"@(lombok\.)?Singular\b", cls.body_text) or re.search(rf"class\s+{name}Builder\b", cls.code):
            complete = False
        names.update(f[1] for f in cls.fields if not f[2])
        if re.search(r"@(lombok\.experimental\.)?SuperBuilder\b", cls.annos):
            for sup in cls.extends:
                sn, sc = builder_fields(idx, sup, _seen)
                names |= sn
                complete &= sc
    return names, complete


def sig_match(sub, sup, tvars):
    if len(sub) != len(sup):
        return False
    for a, b in zip(sub, sup):
        pat = re.escape(b)
        for tv in tvars:
            pat = re.sub(rf"\b{re.escape(tv)}\b", lambda _: r"[\w.<>\[\],?]+", pat)
        if not re.fullmatch(pat, a):
            return False
    return True


# ---------------------------------------------------------------- 检查

def git(*args, cwd=None):
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False, cwd=cwd).stdout


def changed_java_files(base):
    top = git("rev-parse", "--show-toplevel").strip() or "."
    names = set(git("diff", "--name-only", base, cwd=top).split())
    names |= set(git("diff", "--name-only", "--cached", base, cwd=top).split())
    return sorted(os.path.join(top, n) for n in names if n.endswith(".java"))


def added_lines(path, base):
    lines = []
    for line in git("diff", "-U0", base, "--", os.path.abspath(path), cwd=os.path.dirname(os.path.abspath(path))).splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            lines.append(line[1:])
    return lines


class Report:
    def __init__(self, root):
        self.root = root
        self.items = defaultdict(list)  # (级别, 类别) -> [消息]
        self._seen = set()

    def add(self, level, category, path, line, msg):
        loc = os.path.relpath(path, self.root) + (f":{line}" if line else "")
        text = f"{loc}: {msg}"
        if text not in self._seen:
            self._seen.add(text)
            self.items[(level, category)].append(text)

    def count(self, level):
        return sum(len(v) for (lv, _), v in self.items.items() if lv == level)


def line_of(code, pos):
    return code.count("\n", 0, pos) + 1


def check_imports_and_residual(path, src, old_types, suffix, base, max_len, rep):
    lines = src.splitlines()
    code_kc = strip_java(src, keep_comments=True)   # 保留注释（Javadoc 里的引用也算使用）
    code_nc = strip_java(src)
    kc_lines, nc_lines = code_kc.splitlines(), code_nc.splitlines()
    added = added_lines(path, base)
    added_set = {l.strip() for l in added}
    tag = lambda line: "（本次新增）" if line.strip() in added_set else "（存量）"

    seen, imports = {}, []
    for no, line in enumerate(lines, 1):
        m = IMPORT_RE.match(line)
        if not m:
            continue
        key = re.sub(r"\s+", " ", line.strip())
        if key in seen:
            rep.add("错误", "import", path, no, f"重复 import（首次出现在第 {seen[key]} 行）: {key}")
        seen[key] = no
        imports.append((no, bool(m.group(1)), m.group(2), bool(m.group(3))))

    body_kc = "\n".join(l for i, l in enumerate(kc_lines) if not IMPORT_RE.match(lines[i] if i < len(lines) else ""))
    body_nc = "\n".join(l for i, l in enumerate(nc_lines) if not IMPORT_RE.match(lines[i] if i < len(lines) else ""))

    for no, is_static, name, wildcard in imports:
        if wildcard:
            continue
        simple = name.rsplit(".", 1)[-1]
        if not re.search(rf"\b{re.escape(simple)}\b", body_kc):
            rep.add("错误", "import", path, no, f"未使用的 import{tag(lines[no - 1])}: {name}")

    imported = {name.rsplit(".", 1)[-1] for _, s, name, w in imports if not s and not w}
    has_wildcard = any(w and not s for _, s, _, w in imports)
    same_dir = os.path.dirname(os.path.abspath(path))
    for t in old_types:
        new = t + suffix
        if not re.search(rf"\b{re.escape(new)}\b", body_nc):
            continue
        if new in imported or os.path.isfile(os.path.join(same_dir, new + ".java")):
            continue
        if re.search(rf"\b[a-z_][\w$]*(\.[\w$]+)*\.{re.escape(new)}\b", body_nc):
            continue  # 使用了全限定名
        hint = "（文件有通配符 import，请确认其包含该类）" if has_wildcard else ""
        rep.add("错误", "import", path, None, f"使用了 {new} 但缺少 import{hint}")

    for line in added:
        m = IMPORT_RE.match(line)
        if m and m.group(3) and not m.group(1):
            rep.add("错误", "import", path, None, f"新增了通配符 import: {line.strip()}")
        if len(line) > max_len:
            rep.add("错误", "行长度", path, None, f"新增行超过 {max_len} 字符（{len(line)}）: {line.strip()[:80]}...")

    for t in old_types:
        pat = re.compile(rf"\b{re.escape(t)}\b")
        for no, line in enumerate(kc_lines, 1):
            if pat.search(line):
                rep.add("错误", "残留旧类型", path, no, f"残留旧类型 {t}: {lines[no - 1].strip()}")


VAR_RE = re.compile(r"\b([A-Z][\w$]*)\s*(?:<[^;(){}=]*?>)?\s*(?:\[\s*\]\s*)*\s+([a-z_$][\w$]*)\s*(?=[=;,):])")
VAR_INFER_RE = re.compile(r"\bvar\s+([\w$]+)\s*=\s*new\s+([A-Z][\w$]*)\b")
CALL_RE = re.compile(r"(?<![\w$.])(?:this\s*\.\s*)?([A-Za-z_$][\w$]*)\s*\.\s*([\w$]+)\s*\(")
METHOD_REF_RE = re.compile(r"\b([A-Z][\w$]*)\s*::\s*([\w$]+)")
BUILDER_RE = re.compile(r"\b([A-Z][\w$]*)\s*\.\s*builder\s*\(\s*\)")


def check_method_calls(path, idx, old_types, new_types, changed, rep):
    cls = idx.parse(path)
    code = cls.code
    old_re = re.compile(r"\b(" + "|".join(map(re.escape, old_types)) + r")\b") if old_types else None

    var_types = defaultdict(set)
    for m in VAR_RE.finditer(code):
        if m.group(1) not in KEYWORDS:
            var_types[m.group(2)].add(m.group(1))
    for m in VAR_INFER_RE.finditer(code):
        var_types[m.group(1)].add(m.group(2))

    def check_on_type(t, name, pos, ambiguous, how):
        line = line_of(code, pos)
        names, decls, complete, _ = method_set(idx, t)
        if t in new_types:
            if not idx.get(t):
                rep.add("警告", "方法调用不匹配", path, line, f"找不到类 {t} 的源码，无法校验 {how}")
                return
            if name not in names:
                level = "错误" if complete and not ambiguous else "警告"
                rep.add(level, "方法调用不匹配", path, line, f"{t} 中没有方法 {name}()：{how}"
                        + ("（变量名在文件中对应多个类型，请人工确认）" if ambiguous else "")
                        + ("" if complete else "（父类或 Lombok 配置未完全解析，请人工确认）"))
            return
        if not idx.get(t) or any(c.path in changed for c in idx.get(t)):
            return
        if name not in names:
            if complete and not ambiguous:
                rep.add("警告", "方法调用不匹配", path, line, f"{t} 中找不到方法 {name}()：{how}")
            return
        if old_re:
            for owner, (mname, ret, ptypes, mline, _) in decls:
                if mname != name:
                    continue
                sig = f"{ret} {mname}({', '.join(ptypes)})"
                if old_re.search(sig):
                    rep.add("警告", "方法调用不匹配", path, line,
                            f"调用的 {owner.name}.{sig} 签名仍在使用旧类型"
                            f"（{os.path.relpath(owner.path, idx.root)}:{mline}），请确认参数/返回值类型是否匹配：{how}")

    for m in CALL_RE.finditer(code):
        recv, name = m.group(1), m.group(2)
        if recv in ("this", "super") or recv in KEYWORDS:
            continue
        if recv in var_types:
            types = var_types[recv]
        elif recv[0].isupper():
            types = {recv}
        else:
            continue
        for t in types:
            check_on_type(t, name, m.start(2), len(types) > 1, f"{recv}.{name}(...)")

    for m in METHOD_REF_RE.finditer(code):
        t, name = m.group(1), m.group(2)
        if name != "new" and t in new_types:
            check_on_type(t, name, m.start(2), False, f"{t}::{name}")

    for m in BUILDER_RE.finditer(code):
        t = m.group(1)
        if t not in new_types or not idx.get(t):
            continue
        fields, complete = builder_fields(idx, t)
        pos = m.end()
        while True:
            c = re.match(r"\s*\.\s*([\w$]+)\s*\(", code[pos:])
            if not c:
                break
            name = c.group(1)
            close = match_paren(code, pos + c.end() - 1)
            if close is None:
                break
            if name not in fields:
                rep.add("错误" if complete else "警告", "方法调用不匹配", path, line_of(code, pos + c.start(1)),
                        f"{t}.builder() 中没有字段 {name}" + ("" if complete else "（请人工确认）"))
            if name == "build":
                break
            pos = close + 1


def check_overrides(path, idx, rep):
    cls = idx.parse(path)
    if not cls.kind or not cls.supers:
        return
    super_decls, all_resolved, tvars = [], True, set()
    for sup in cls.supers:
        if sup in KNOWN_EXTERNAL:
            continue
        _, decls, complete, tv = method_set(idx, sup)
        if not idx.get(sup):
            all_resolved = False
        all_resolved &= complete
        super_decls.extend(decls)
        tvars |= tv
    tvars |= {t for _, (_, _, ptypes, _, _) in super_decls for p in ptypes for t in re.findall(r"\b[A-Z]\d?\b", p)}
    for name, ret, ptypes, line, is_override in cls.methods:
        if not is_override or name in OBJECT_METHODS:
            continue
        same = [(o, d) for o, d in super_decls if d[0] == name]
        if not same:
            if all_resolved:
                rep.add("警告", "方法调用不匹配", path, line, f"@Override 方法 {name}() 在父类型中找不到")
            continue
        if not any(sig_match(ptypes, d[2], tvars) for _, d in same):
            cands = "; ".join(f"{o.name}.{d[0]}({', '.join(d[2])})" for o, d in same)
            rep.add("警告", "方法调用不匹配", path, line,
                    f"@Override 方法 {name}({', '.join(ptypes)}) 与父类型签名不一致：{cands}")


def find_subtypes(idx, names, changed):
    if not names:
        return set()
    pat = re.compile(r"\b(?:extends|implements)\b[^{;]*\b(" + "|".join(map(re.escape, names)) + r")\b")
    result = set()
    for p in idx.all_files():
        p = os.path.abspath(p)
        if p in changed:
            continue
        text = idx.text(p)
        if any(n in text for n in names) and pat.search(strip_java(text)):
            result.add(p)
    return result


def check_unchanged_callers(path, idx, new_types, changed, base, rep):
    cls = idx.parse(path)
    if not cls.kind or not new_types:
        return
    added = {re.sub(r"\s+", "", l) for l in added_lines(path, base)}
    new_re = re.compile(r"\b(" + "|".join(map(re.escape, new_types)) + r")\b")
    src_lines = idx.text(path).splitlines()
    targets = []
    for name, ret, ptypes, line, _ in cls.methods:
        sig = f"{ret} {name}({', '.join(ptypes)})"
        decl_line = re.sub(r"\s+", "", src_lines[line - 1]) if line <= len(src_lines) else ""
        if new_re.search(sig) and decl_line in added:
            targets.append((name, sig))
    if not targets:
        return
    owners = {cls.name, *cls.supers} - KNOWN_EXTERNAL
    owner_re = re.compile(r"\b(" + "|".join(map(re.escape, owners)) + r")\b")
    for p in idx.all_files():
        p = os.path.abspath(p)
        if p in changed:
            continue
        text = idx.text(p)
        if not any(name in text for name, _ in targets):
            continue
        code = strip_java(text)
        if not owner_re.search(code):
            continue
        for name, sig in targets:
            for m in re.finditer(rf"(?:\.\s*|::\s*){re.escape(name)}\b", code):
                rep.add("警告", "方法调用不匹配", p, line_of(code, m.start()),
                        f"调用了 {cls.name}.{sig}（签名已改为 Dto），但该文件未修改，请确认参数/返回值类型是否匹配")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--old", default="", help="逗号分隔的旧类型名，如 IpranSummaryRsp,SummaryReq")
    ap.add_argument("--files", nargs="*", help="要检查的 Java 文件，默认取 git diff 中的 .java 文件")
    ap.add_argument("--base", default="HEAD", help="对比基线，默认 HEAD")
    ap.add_argument("--root", default=None, help="仓库根目录，默认取 git 仓库根目录")
    ap.add_argument("--suffix", default="Dto", help="DTO 后缀，默认 Dto")
    ap.add_argument("--max-len", type=int, default=120, help="行长度上限，默认 120")
    args = ap.parse_args()

    root = os.path.abspath(args.root or git("rev-parse", "--show-toplevel").strip() or ".")
    old_types = [t.strip() for t in args.old.split(",") if t.strip()]
    new_types = {t + args.suffix for t in old_types}
    files = [os.path.abspath(f) for f in (args.files if args.files else changed_java_files(args.base))]
    files = [f for f in files if os.path.isfile(f)]  # 跳过已删除的文件
    if not files:
        print("没有需要检查的 Java 文件")
        return

    idx = RepoIndex(root)
    changed = set(files)
    rep = Report(root)
    for f in files:
        with open(f, encoding="utf-8", errors="ignore") as fh:
            src = fh.read()
        check_imports_and_residual(f, src, old_types, args.suffix, args.base, args.max_len, rep)
        check_method_calls(f, idx, old_types, new_types, changed, rep)
        check_unchanged_callers(f, idx, new_types, changed, args.base, rep)

    # @Override 签名：改动文件 + 继承/实现了改动类的未改动文件
    changed_names = {idx.parse(f).name for f in files if idx.parse(f).name}
    for f in sorted(changed | find_subtypes(idx, changed_names, changed)):
        check_overrides(f, idx, rep)

    print(f"检查了 {len(files)} 个改动文件（仓库根目录：{root}）")
    for level in ("错误", "警告"):
        groups = [(cat, msgs) for (lv, cat), msgs in sorted(rep.items.items()) if lv == level]
        if not groups:
            continue
        print(f"\n【{level}】共 {rep.count(level)} 个" + ("，必须修复" if level == "错误" else "，需逐条确认（修复，或在报告中说明误报原因）"))
        for cat, msgs in groups:
            print(f"  [{cat}]")
            for msg in msgs:
                print(f"  - {msg}")
    if rep.count("错误"):
        sys.exit(1)
    print("\n没有错误" + ("，请逐条确认上面的警告" if rep.count("警告") else "，全部通过"))


if __name__ == "__main__":
    main()
