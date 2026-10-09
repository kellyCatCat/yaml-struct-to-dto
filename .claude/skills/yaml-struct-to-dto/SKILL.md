---
name: yaml-struct-to-dto
description: 推进 Java 代码中 “yaml 生成的结构体 → XxxDto” 的重构改造（如 IpranSummaryRsp -> IpranSummaryRspDto、SummaryReq -> SummaryReqDto）。从指定的 Java 文件（如 OpsIpranSummaryTunnelFacadeImpl）出发，沿调用链全量排查并替换类型与 import，自检 clean code 后按 “[DTS单号][fix][26.1]<模块>中<类名>中的DTO改造” 格式提交 git commit。当用户提到 DTO 改造、yaml 结构体改 Dto、给某个 FacadeImpl/Service 做 Dto 整改时使用。
argument-hint: <目标Java类名或路径> [DTS单号]
---

# yaml-struct-to-dto：yaml 结构体 → DTO 改造

## 输入

- **目标文件**（必填）：Java 类名（如 `OpsIpranSummaryTunnelFacadeImpl`）或文件路径。
- **DTS 单号**（必填）：如 `DTS2026092702383`。用户没给时先问，不要自己编造，也不要直接复用示例单号。
  可以先用 `git log --oneline --grep "DTO改造" -5 -- <模块路径>` 找同模块最近使用的单号，作为建议值供用户确认。

以下是固定约定：

| 项 | 值 |
|---|---|
| 基线分支 | `br_NCEV1R26C10_Master` |
| 版本号 | `26.1` |
| DTO 后缀 | `Dto`（`Xxx` → `XxxDto`） |
| commit msg | `[<DTS单号>][fix][26.1]<模块名>中<类名>中的DTO改造` |

commit msg 示例：`[DTS2026092702383][fix][26.1]ipran-app-api-summary中OpsIpranSummaryTunnelFacadeImpl中的DTO改造`

本 skill 自带两个脚本，下文用 `<skill>` 表示本 SKILL.md 所在目录：

- `<skill>/scripts/scan_dto_candidates.py`：扫描引用的类型，找出要替换的类型和调用链上的业务类
- `<skill>/scripts/check_changed_files.py`：改完后的 clean code 自检

## 步骤

### 1. 拉取 `br_NCEV1R26C10_Master` 最新代码

```bash
git status --porcelain            # 必须为空
git fetch origin br_NCEV1R26C10_Master
git checkout br_NCEV1R26C10_Master   # 本地没有该分支时：git checkout -b br_NCEV1R26C10_Master origin/br_NCEV1R26C10_Master
git pull --ff-only origin br_NCEV1R26C10_Master
```

- 工作区不干净时**停下来问用户**，不要擅自 stash、reset 或丢弃改动。
- `--ff-only` 失败（本地分支和远端分叉）时停下来问用户，不要强行 merge、rebase 或 reset。

### 2. 定位目标文件和模块

```bash
find . -name "<类名>.java" -not -path "*/target/*"
```

- 模块名取目标文件**最近一层带 `pom.xml` 的目录名**（例如 `ipran-app-api-summary`），并和该 `pom.xml` 中的 `<artifactId>` 对照。两者不一致时，以历史 DTO 改造 commit msg 里的写法为准。
- 有多个同名文件时，向用户确认是哪一个。

### 3. 参考历史改造（只挑 1~3 个样例）

项目里 “DTO改造” 的提交会有很多，**不要逐个去看**。这一步只是为了学习写法，挑几个最相关的样例就够了。

**3.1 先查目标类是否已经改过**

```bash
git log --oneline --grep "DTO改造" | grep -F "<类名>"
```

如果有结果，说明这个类之前已经做过（或者部分做过）改造。先看那次提交改了什么，告诉用户，再确认是补齐剩下的部分，还是停止。

**3.2 挑样例**

按下面的优先级挑，挑到 1~3 个就停：

```bash
# ① 同模块的改造（最相关）
git log --oneline --grep "DTO改造" -10 -- <模块路径>
# ② 同模块没有时，再看全仓库最近的
git log --oneline --grep "DTO改造" -10
```

- 优先选：同模块 > 时间最近 > 类型相近（同样是 FacadeImpl 的改造）。
- 先用 `git show --stat <commit>` 看改了哪些文件，挑文件数适中、包含接口和 Impl 的提交。不要上来就看完整 diff。
- 看 diff 时只看 Java 文件，文件太多就只看接口、Impl、Converter 这几个关键文件：
  ```bash
  git show <commit> -- '*.java'
  git show <commit> -- <具体文件路径>
  ```

**3.3 要学什么**

- Dto 类放在哪个包，命名、注解（Lombok、`@JsonProperty` 等）是怎么写的；
- Facade 接口是否一起改了签名；
- 对外仍然必须使用 yaml 结构体的边界（例如由 yaml 生成的 REST 接口或 Controller）是怎么处理的，有没有转换方法（Converter、Assembler、BeanUtils 等）；
- 测试代码是怎么同步修改的。

**注意**：
- 历史提交只用来学**写法**，**不要**拿来当替换清单。要改哪些类型，只以第 4 步的调用链排查结果为准。
- 几个样例的写法不一致时，以同模块最近的提交为准；分歧较大、拿不准的时候问用户。

### 4. 沿调用链全量排查（核心）

**判定逻辑**：从最顶层的调用（指定的 Java 文件）出发，沿调用层级向下排查所有涉及到的内部调用，凡是 yaml 结构体流经的地方都在改造范围内。

先用脚本跑出初始清单（在仓库根目录执行）：

```bash
python3 <skill>/scripts/scan_dto_candidates.py <类名或路径> --depth 3
```

输出说明：
- **READY**：已存在 `XxxDto` 的旧类型，需要替换。
- **MISSING_DTO**：yaml 里定义了，但还没有对应的 Dto，需要处理（见第 5 步）。
- **项目内被引用的业务类**：调用链上的 Service、Manager、Helper、Converter 等，需要逐个判断。
- **通配符 import**：替换后要手工核对 import。

脚本只是辅助，**必须再人工逐层确认**，按以下顺序排查，不要遗漏：

1. **目标文件本身**：字段、方法签名、局部变量、泛型（`List<X>`、`Map<String, X>`、`Optional<X>`）、Lambda 和方法引用（`X::new`、`X::getXxx`）、`new X()`、`X.class`、`X.builder()`、强转、`instanceof`、内部类和内部枚举（`X.StatusEnum`）、Javadoc（`{@link X}`、`@param`、`@return`）。
2. **它实现的接口**（如 `OpsIpranSummaryTunnelFacade`）：签名改了，接口要同步改，并且要找出该接口的**所有其他实现类**和**所有调用方**：
   ```bash
   grep -rn --include=*.java -E "implements .*\b<接口名>\b" .
   grep -rn --include=*.java -w "<接口名>" .
   ```
3. **它调用的项目内方法**：逐个进入被调用的 Service、Manager、Helper、Converter、Util，看入参、返回值和内部是否用到了 yaml 结构体。用到了就纳入改造，再对这个文件重复本步骤，直到调用链末端。
4. **签名变化的扩散**：任何方法签名发生变化，都要找出它在全仓库的调用方（包括 `src/test`），保证都能编译通过：
   ```bash
   grep -rn --include=*.java -w "<方法名>" .
   ```
   调用方如果是改造范围外的其他 Facade，只做最小适配让它能编译，并在最终报告里单独列出来。
5. **非 Java 文件**：用旧类的全限定名搜索 Spring XML、MyBatis mapper、配置文件、反射字符串，确认没有其他引用：
   ```bash
   grep -rn "<旧类全限定名>" --include=*.xml --include=*.properties --include=*.yaml --include=*.yml --include=*.json .
   ```

排查完要整理出一张**替换清单**，在动手修改前展示给用户：

| 旧类型 | 旧包 | 新类型 | 新包 | 涉及文件 |
|---|---|---|---|---|

### 5. 核对 Dto 是否可以直接替换

对清单里的每一对 `X` / `XDto`：
- **包名要对**：同名 Dto 可能不止一个，选和历史改造一致、和当前模块对应的那个。
- **字段兼容**：对比字段、getter/setter、builder、构造方法、内部枚举。如果 Dto 的字段名或类型不一样，调用处要相应调整，不能只改类名。
- **嵌套类型**：`X` 里如果嵌套了其他 yaml 结构体（如 `List<TunnelInfo>`），对应的 Dto 里应该是 `List<TunnelInfoDto>`，嵌套类型也要加入替换清单。
- **MISSING_DTO**：大部分 Dto 已经创建好了。确实缺少时，照着已有 Dto 的风格（包、注解、字段写法）新建，并在报告里列出来；如果不确定要不要新建，先问用户。
- **边界处**：如果某处必须继续使用 yaml 结构体（例如 yaml 生成的接口签名），不要改生成代码，而是按历史改造的方式在边界做转换。

### 6. 执行替换

- 按**单词边界**替换（`\bSummaryReq\b`），避免误改 `IpranSummaryReq`，也避免出现 `SummaryReqDtoDto`。每个旧类型要单独确认，不要一条正则全量替换后就不管了。
- **import 必须同步改**（这是最容易遗漏的地方）：
  - 删掉不再使用的旧 import，加上新 Dto 的 import。
  - Dto 和当前类在同一个包时不需要 import。
  - 不要新引入通配符 import（`import xxx.*;`）。原来是通配符 import 的，确认改完后它是否还被其他类型使用，没用了就删掉，并显式 import Dto。
  - 保持文件原有的 import 分组和排序规则，不要重复 import。
  - 能用 import 的地方不要写全限定名。
- **不要改的东西**：
  - 字符串字面量和日志内容；
  - 变量名、方法名（例如 `summaryReq` 保持不变），除非项目历史改造里有统一改名的先例；
  - yaml 文件、yaml 生成的旧类本身；
  - 与本次改造无关的格式、空行和代码。
- 类名变长后，如果行超过项目行宽（默认 120），按项目原有风格换行。
- `src/test` 下引用了这些类型或变动签名的测试代码也要同步修改。

### 7. 自检

1. **clean code 脚本自检**（必须全部通过）：
   ```bash
   python3 <skill>/scripts/check_changed_files.py --old <旧类型1>,<旧类型2>,...
   ```
   它会检查改动过的 Java 文件里是否有：残留的旧类型、用了新 Dto 但缺少 import、未使用的 import、重复的 import、新增的通配符 import、新增的超长行。标为“（存量）”的未使用 import 如果和本次改造无关，可以不动。
2. **全仓库残留检查**：在改造范围内的文件里，不应该再出现旧类型：
   ```bash
   grep -rn --include=*.java -wE "<旧类型1>|<旧类型2>" <改造涉及的目录>
   ```
   剩下的每一处都要能说清楚为什么保留（例如边界转换处）。
3. **编译验证**（以用户的做法为准：在 IDEA 的 Maven 面板里，对根工程 `NetChatOpsIPExtServiceRoot` 先执行 Lifecycle 的 clean，再执行 install）：
   - 根工程是 `<project>` 下直接写着 `<artifactId>NetChatOpsIPExtServiceRoot</artifactId>` 的 pom.xml，一般就在仓库根目录。注意子模块的 `<parent>` 里也会出现这个名字，不要找错。
   - 命令行能用 `mvn` 时，在根工程目录执行：
     ```bash
     mvn clean install
     ```
     这和在 IDEA 里先点 clean、再点 install 是一样的：全部模块都会构建，单元测试会运行，产物会安装到本地仓库。**不要**为了省时间加 `-pl`、`-am`、`-o`、`-DskipTests` 这类缩小范围的参数，否则依赖本模块的下游模块和单元测试都验证不到。只有用户说过在 IDEA 里打开了 Skip Tests，才加 `-DskipTests`。
   - 命令行没有 `mvn`（IDEA 自带的 Maven 通常不在 PATH 里），或者依赖拉不下来（IDEA 可能配置了自己的 settings.xml 和内网仓库），就**停下来**，请用户在 IDEA 里执行 clean → install，并把结果告诉你（`BUILD SUCCESS`，或者报错信息）。不要自己换别的命令凑合。
   - 构建失败时，按报错修改，再重新验证，直到 `BUILD SUCCESS`。如果失败和本次改造无关（构建前就已经失败），告诉用户，由用户决定怎么处理。
4. **复读 diff**：`git diff` 逐个文件过一遍，确认没有误改、漏改，也没有无关改动。

### 8. 提交 commit

```bash
git add <本次改动的文件...>        # 显式列出文件，不要用 git add -A / git add .
git diff --cached --stat
git commit -m "[<DTS单号>][fix][26.1]<模块名>中<类名>中的DTO改造"
```

- **第 7 步的编译验证通过（自己执行通过，或者用户在 IDEA 中确认通过）之前，不要提交。**
- commit msg 严格使用上面的格式，只有这一行，不要追加其他内容。
- 使用仓库已有的 git 用户配置，不要修改 `git config`。
- **只提交到本地，不要 push**。提交后告诉用户 commit hash，由用户决定怎么推送；用户明确要求时才 push。

### 9. 输出报告

最后给用户一份简洁的报告：
- commit hash 和 commit msg；
- 替换清单（旧类型 → 新类型）；
- 改动文件列表，按调用层级排列（目标文件 → 接口 → Service → …）；
- 新建的 Dto（如果有）；
- 为了编译而做了最小适配的范围外调用方（如果有）；
- 保留了旧类型的地方及原因（如果有）；
- 编译和自检的结果，并注明编译是自己执行的 `mvn clean install`，还是用户在 IDEA 中确认的。
