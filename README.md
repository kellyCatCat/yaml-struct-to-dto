# yaml-struct-to-dto

Claude Code skill：推进 Java 代码中 “yaml 生成的结构体 → XxxDto” 的重构改造，并按规范提交 commit。

## 安装

把 `.claude/skills/yaml-struct-to-dto` 目录复制到以下任一位置：

- 业务项目根目录下的 `.claude/skills/`（只对该项目生效）
- `~/.claude/skills/`（对所有项目生效）

```bash
cp -r .claude/skills/yaml-struct-to-dto ~/.claude/skills/
```

## 使用

在业务项目中打开 Claude Code：

```
/yaml-struct-to-dto OpsIpranSummaryTunnelFacadeImpl DTS2026092702383
```

也可以直接用自然语言描述，例如“帮我把 OpsIpranSummaryTunnelFacadeImpl 做一下 DTO 改造，单号 DTS2026092702383”。

## 流程

1. 拉取 `br_NCEV1R26C10_Master` 最新代码
2. 定位目标文件和所在模块，检查目标类是否已经改造过，从同模块的历史 DTO 改造中挑 1~3 个样例学习写法
3. 从指定 Java 文件出发，沿调用链全量排查 yaml 结构体（接口、其他实现类、Service、Converter、调用方、测试）
4. 替换为 `XxxDto`，同步修改 import
5. 跑 clean code 自检脚本，并进行编译验证
6. 本地提交：`[DTS单号][fix][26.1]<模块名>中<类名>中的DTO改造`（不自动 push）

## 辅助脚本

| 脚本 | 作用 |
|---|---|
| `scripts/scan_dto_candidates.py <类名> --depth 3` | 扫描调用链上引用的类型，列出可替换的 Dto、缺失的 Dto、调用链上的业务类 |
| `scripts/check_changed_files.py --old A,B` | 改完后自检：残留旧类型、缺失 import、未使用 import、重复 import、通配符 import、超长行 |
