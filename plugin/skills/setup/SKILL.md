---
name: setup
description: "安装、修复、升级或卸载 TaskSteward（任务管家）：在会话中探测前提、取得产品检出、按问题清单提问，再运行 tsw setup。只在用户要求安装、修复、升级或卸载 TaskSteward 时使用。"
---

# 在会话中安装 TaskSteward

按 DN009 §4.2 与 CC009 §4 的会话路径工作。确定的步骤都由脚本完成：你只解释、提问、运行下面的命令并转告结果，不自己拼路径、不判断某一步该不该做、不改脚本的输出。用户要做的只是回答问题、批准 Claude 提示的命令，最后重启 Claude app。

**谁可以运行**：还没有绑定任何数据目录的会话（第一次安装）与主会话（修复、升级、卸载）。`whoami` 的角色是 worker 或例行会话时不要使用本 skill：告诉用户请在主会话中或会话外的终端中做。

以下命令都在 Bash 中运行，路径用正斜杠写。`<python>` 是第 1 步找到的解释器的完整路径，`<checkout>` 是第 2 步的检出。

## 1. 探测前提

```bash
powershell -NoProfile -ExecutionPolicy Bypass -File "${CLAUDE_PLUGIN_ROOT}/setup/prereq.ps1" -Json
```

输出一行 JSON：`python`、`git`、`claude`（各为 `{path, version}` 或 null）、`winget`、`missing`、`notes`。

- `missing` 中有 `python` 或 `git`：问用户是否用 winget 安装（一项一问）。同意后运行 `... prereq.ps1 -Json -Install python`（或 `git`），再看新的输出；不同意或没有 winget 时，告诉用户自己安装后再来，停下。
- `missing` 中有 `claude`：把 `notes` 转告用户，停下（Claude Code 的命令行要先装好）。
- `notes` 都转告用户。之后一律用 `python.path` 与 `git.path` 的完整路径（新装的解释器与 git 不在当前进程的 PATH 中）：下文的 `<git>` 是 `git.path`。

## 2. 取得产品检出

先问用户产品检出放在哪里：默认 `%LOCALAPPDATA%\TaskSteward\task-steward`，已有检出时复用。然后：

```bash
"<python>" -X utf8 "${CLAUDE_PLUGIN_ROOT}/setup/fetch_checkout.py" --git "<git>" [--dest "<用户给的位置>"]
```

它把产品仓库克隆到该位置并检出插件当前安装的那个提交；已有检出时只校验是同一仓库、工作区干净。`ok` 为 false 时把 `problem` 转告用户，停下。`checkout` 就是之后的 `<checkout>`，`launcher` 是 `<checkout>/plugin/bin/tsw_launch.py`。

## 3. 按问题清单提问

```bash
"<python>" -X utf8 "<checkout>/plugin/bin/tsw_launch.py" setup check --questions --git "<git>" --json
```

- `problems` 中除 `home：…`（还没有数据目录位置）以外的问题先转告用户，解决后重新运行。
- `questions` 是唯一的问题清单。`asked_by` 为 `entry` 的（产品检出）已在第 2 步问过，不再问。按顺序在对话中一项一项问，每项说明 `note` 与默认值：
  - `home`（数据目录位置）**必填，没有默认值**：用户没有给出时再问，不要替用户选一个路径。用户给出已有的数据目录时复用它。提问时说明：在数据目录（那个文件夹本身）中打开的会话是主会话，建议之后在数据目录中新建会话工作；本会话即使就在那个文件夹里也可以安装，安装结束时它会记为让出租约，不会自动领取（DN009 §15.1）。
  - `host` 只有一个选项时直接选定，不必问。
  - 其他项用户说“默认”时取 `default`。
- 不问、也不记录任何令牌或密码；要管理的仓库不在这里问（安装后在主会话中逐个加入）。

把答案写成 JSON 对象（键为问题的 `id`，`home` 与 `checkout` 写完整路径，布尔写 true / false，端口写整数），用 Write 写到 `check` 输出的 `answers_file`。

## 4. 计划与确认

```bash
"<python>" -X utf8 "<checkout>/plugin/bin/tsw_launch.py" setup plan --answers "<answers_file>" --python "<python>" --claude "<claude.path>" --git "<git>" --json
```

把 `steps` 中要做的（`state` 为 `todo`）与用户设置的差异（`user_settings` 一步的 `diff`）简要告诉用户：会写哪些文件、改哪些用户设置、在用户级 PATH 加哪一项、是否登记 Dashboard 计划任务。`problems` 非空时先解决。然后请用户确认（Claude app 中用 `wrap_up` 的提问与按钮，`blocked: true`）。

## 5. 安装

用户确认后：

```bash
"<python>" -X utf8 "<checkout>/plugin/bin/tsw_launch.py" setup install --answers "<answers_file>" --python "<python>" --claude "<claude.path>" --git "<git>" --json
```

单独一条命令运行。Claude 可能请用户批准这条命令（它写用户设置与 PATH、登记计划任务）：这是用户本人的授权，不要设法绕过。把结果简要告诉用户：每一步做了什么、`verify` 的 `failed`（失败的核对项及原因）与 `pending_reload`（重启后才能确认，不算失败）。失败时按原因修正后重新运行同一条命令（它只做缺少或不一致的部分）。

## 6. 重启、主会话与示例任务

安装结果的 `next` 就是要告诉用户的三步，原样转告：

1. 完全退出并重新打开 Claude app：新的用户设置与插件在重新打开后才生效（试装实测），本会话不能替用户创建主会话；
2. 在数据目录（答案中的 `home`）中新建一个会话，它是主会话；
3. 在主会话中输入“开始示例任务”（`demo_prompt`），看 TaskSteward 怎样执行一个任务：主会话按 `tsw-demo` skill 登记一个示例任务，派发给 worker，最后引导用户在 Dashboard 中看报告并验收；可以跳过，之后随时说这句话。要管理的仓库也在主会话中告诉它，由它逐个加入配置。

结果中有 `install_session` 时，告诉用户：本会话记为让出租约，之后不会自动成为编排的主会话。

需要时，重启之后可以在主会话中运行（先 `whoami` 取 `runtime.commit`）：

```bash
tsw setup verify --after-reload --running <runtime.commit> --json
```

## 卸载

用户要求卸载时，先说明会删除什么（Dashboard 常驻服务、插件与市场、安装脚本加入的用户设置、`tsw` 垫片与 PATH 项）、保留什么（数据目录永远不删；产品检出由用户自己删），请用户确认，再运行：

```bash
"<python>" -X utf8 "<checkout>/plugin/bin/tsw_launch.py" setup uninstall --yes --git "<git>" --json
```

转告结果中的 `kept`、`note` 与 `removed.plugin.notes`：卸载把插件恢复到安装前的状态，插件在运行本 skill 之前就已安装（例如用 `/plugin install` 装的）时它会保留，用户要完全移除时按 notes 运行那两条命令（会话中为 `/plugin uninstall task-steward@task-steward` 与 `/plugin marketplace remove task-steward`）。会话路径中由本 skill 克隆的检出，用户确认后可以删除。
