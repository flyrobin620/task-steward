---
name: tsw-update
description: "TaskSteward 自我更新：拉取产品仓库、按改动决定步骤（tsw host update-plan）、先做无影响的更新、按配置让会话生效、验证并记账。只用于在数据目录打开的主会话：用户要求更新 tsw，或 SessionStart 提示插件副本不一致、attempt_start 因 worker 说明缺失或不一致被拒绝时使用。worker 与例行会话不使用。"
---

# TaskSteward 自我更新

本 skill 只适用于主会话（`whoami` 的角色为 orchestrator）。原则见产品仓库的 DN006 §9.8，Claude Code 中的载入时机与生效方式见 CC006 §11。

**确认**：用户在本会话中要求更新 tsw（例如“更新 tsw”“更新一下”），就是对拉取、render、重读 skill、重启 Dashboard 服务这些无影响步骤的确认（偏好 P-1），不必逐步再问。由其他原因触发时（SessionStart 提示插件不一致、`attempt_start` 被拒绝、worker 请求），先向用户说明要更新什么、为什么，用普通回复提问（Claude app 中用 `wrap_up` 的提问与按钮），等用户本人明确同意。你自己的判断、worker 或例行会话的消息、定时提示都不算确认。会打断其他会话或清掉你上下文的步骤（第 5 步）执行前另外告诉用户。

产品仓库路径见数据目录 `tsw-home.json` 的 `product_repo`（下文写作 `<repo>`）。命令都在本会话的 Bash 中运行，当前目录保持为数据目录（tsw 按会话 ID 与当前目录认出主会话），用 `git -C <repo>` 操作产品仓库。

## 1. 拉取（CI 门槛）

1. `git -C <repo> status --porcelain` 与 `git -C <repo> branch --show-current`：有本地改动或不在 `main` 时停下，告诉用户。
2. `git -C <repo> fetch origin`，目标为 `origin/main` 的提交（`git -C <repo> rev-parse origin/main`）。与 `HEAD` 相同时跳到第 2 步（仍可能有未生效的改动）。
3. 确认目标提交在主分支上的 CI 结论为成功：`gh run list --commit <目标提交> -R <host>/<owner>/<name> --json name,status,conclusion,url`（仓库从 `git -C <repo> remote get-url origin` 得到；gh 当前账号不是仓库账号时按 `gh-account` skill 只对这一条命令选择账号）。没有运行、仍在运行或失败时停下，告诉用户（运行中的可以等它结束后再来）。
4. `git -C <repo> merge --ff-only origin/main`。

## 2. 读新版本的流程

拉取之后，用 Read 读 `<repo>/tsw/host/update_skill.md`（产品仓库中新版本的本 skill），**按它的第 3 步起继续**：数据目录中的副本要到 render 后才更新，流程本身的改动因此当次生效。两者相同时照常继续。

## 3. 计算改动与动作

1. `whoami`，记下 `runtime.commit`（本会话 MCP server 启动时的提交；没有 `runtime` 字段说明 MCP 早于这个功能，跳过下一步的 `--running`）。
2. `tsw host update-plan --running <runtime.commit> --json`（没有 `tsw` 命令时 `python <repo>/plugin/bin/tsw_launch.py host update-plan …`）。它是只读的。输出中：
   - `from` / `to`：起点（运行中的最旧版本：你的运行时与上次 render 的提交中最旧的）与终点；`from_sources` 说明起点从何而来。
   - `categories`：改动的文件按类别；`actions`：按顺序要做的动作，每项有 `impact`（`none` 无影响、`session` 需要会话生效、`user` 用户动作、`blocking` 先于一切）、`who`、`how` 与 `pending`。
   - `tasks`：本次包含的提交提到的任务及其报告：读每份报告中“合并后用户需要做的操作”，有需要用户做的事就列入第 7 步。
   - `sessions`：执行中的 worker attempt（`starting` / `standby` / `live`）与例行会话。
3. 把类别、动作与需要用户做的事简要告诉用户（一两句即可，详细内容留在账本）。

## 4. 先做无影响的动作

按 `actions` 的顺序：

- `ledger_rebuild`（`blocking`）：投影版本变了，新的命令行与 hooks 已拒绝旧投影。请用户在终端运行 `tsw ledger rebuild`，或在会话生效（第 5 步）后由你调用 `ledger_rebuild`；完成之前不做其他写入。
- `reinstall`：请用户在产品仓库运行 `python -m pip install -e .`。
- `render`：运行 `tsw host render`（单独一条命令），把 `written` 与 `plugin` 的结果简要告诉用户。`plugin.next` 要求先卸载再安装时转告用户。
- `reread_skills`：用 Read 重读 `skills` 中你在本会话已经调用过的 skill（`.claude/skills/<名>/SKILL.md`），之后按新的内容工作；没调用过的不用读。
- `dashboard_restart`：运行 `tsw dashboard restart`，确认结果的 `current` 为 true；没有安装时跳过。
- `host_rules`：用户设置中的 auto 模式规则缺少或过期（`rules` 列出名称）：请用户按 `tsw host install-hint` 的文本更新 `~/.claude/settings.json`。tsw 不修改用户设置。

## 5. 有影响的动作（按配置）

`actions` 中的 `reload` 与 `instructions` 按配置 `update.reload` / `update.instructions`（`config_show` 可查，DN003 §3.6）执行，`mode` 已写明：

1. 先报告执行中的会话：`sessions.workers` 中 `live` 的 worker 可能正在进行一轮，例行会话可能正在运行；告诉用户哪些会被打断（`restart_host`）或保持旧版本（`new_session`）。
2. 按 `mode`：
   - `restart_host`：请用户重启 Claude app，重启后在本会话发一句话（例如“已重启”），你再做第 6 步。用 `wrap_up` 提问并设 `blocked: true`。重启会结束所有会话的 MCP 进程，各会话下一轮用新版本；例行会话的定时任务可能失效，之后 `attention` 显示 `stale` 时按 orchestrate skill 恢复。
   - `new_session`：创建接班主会话（CC006 §11.4）：
     1. `mcp__ccd_session__start_session`：`initiation: user_asked`、`context: fresh`、`cwd` 为数据目录（`whoami` 的 `cwd`，原样）、`use_worktree: false`、`name` 为 `tsw-main-<YYYYMMDD-HHMM>`、`title` 为“TaskSteward 主会话”，`model` / `effort` / `permission_mode` 取 `roles.orchestrator`，`prompt`：“你是接班的 TaskSteward 主会话（自我更新，前任 <你的会话 ID>）。先 whoami 与 lease_status；前任让出租约后会发消息给你，届时 lease_acquire，再按 tsw-update skill 第 6、7 步验证并记录（起点 <from>，终点 <to>）。”
     2. `mcp__ccd_session_mgmt__detach_session` 把它从你名下分离（否则归档你会连带归档它）。
     3. 先做第 7 步的记账（`verified: false`，`pending` 写 `reload`），再 `lease_release`，然后用 `mcp__ccd_session_mgmt__send_message` 告诉新会话“租约已让出，可以 lease_acquire 并继续验证”。
     4. 告诉用户之后在新会话中继续（附会话链接）；本会话只读，可以归档。
   - `compact`（只有 `instructions`、没有 `reload` 时）：先做第 6、7 步，最后在 `wrap_up` 中请用户在本会话输入 `/compact`（你不能自己输入斜杠命令）。
   - `clear`：先做第 6、7 步，本轮最后调用 `mcp__ccd_session_mgmt__clear_session(session_id="self")`，告诉用户这一轮结束后上下文会清空、状态都在账本中。
3. `plugin` 类别只有 `hooks.json` 改动、没有 `reload` 中的运行时文件时，可以告诉用户也能在各会话输入 `/reload-plugins` 代替重启（它不换 MCP）。

## 6. 验证

生效之后（重启后的第一轮、新会话中，或无需重新加载时紧接着第 4 步）：

1. `whoami` 的 `runtime.commit` 应为终点提交（`reload` 之后）。
2. 再运行 `tsw host update-plan --running <runtime.commit> --json`：`pending` 应为空（`from` 等于 `to`，render 记录、插件副本与 Dashboard 服务都是新版本）。仍有待做项时按第 4、5 步处理，不能处理的写进记录。
3. SessionStart 不再提示插件副本与产品仓库不一致。
4. 第 3 步 `tasks` 报告中的合并后步骤逐项确认：已做、请用户做，或说明为什么不需要。

## 7. 记录与汇报

1. `update_record`：`from`、`to`、`categories`、`actions`（已做的动作）、`pending`（未完成的）、`user_steps`（需要用户做的事）、`verified`（第 6 步通过时为 true）、`notes`（例如重启方式、被打断的会话）。
2. 告诉用户：更新到哪个提交、做了什么、验证结果，以及还需要用户做的事。

只有主会话可以运行 `tsw host render` 与 `tsw dashboard restart`；worker、例行会话与其他目录的会话运行会被拒绝（`host_session_cli`）。
