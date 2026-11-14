---
name: gh-account
description: "按仓库选用 gh 账号（Claude Code）：确定仓库应使用的 GitHub 账号、核对 gh 当前账号，以及只对一条命令生效的做法（GH_TOKEN 或 GH_CONFIG_DIR 前缀、git push 指定账号）。用 gh 建 PR、合并、查看 CI 或推送，而本机登录了多个账号、活动账号可能不是仓库账号时使用。主会话与 worker 都可用。"
---

# 按仓库选用 gh 账号

本机的 gh 可能在同一主机上登录了多个账号（例如 github.com 上的企业托管账号与个人账号）。活动账号只有一个，而且对所有会话生效；gh 没有按单条命令指定账号的参数。仓库账号不是活动账号时，命令会被拒（例如“As an Enterprise Managed User, you cannot access this content”或“Permission … denied to <账号>”）。设计见 CC006 §10.1。

## 1. 确定仓库的账号

按顺序取第一个有的：

1. 任务书写明的账号。
2. 配置 `repositories.<仓库>.auth_profile` 指向的 `accounts.<名>`：`host` 与 `account`（主会话用 `config_show` 查看；worker 没有配置工具，以任务书为准）。
3. 仓库 `owner`（`-R <host>/<owner>/<name>` 或远端 URL 中的 owner）与 `gh auth status --hostname <host>` 列出的某个已登录账号同名时，取该账号。

都没有时问用户（worker 用 `ask_user`），不要猜，也不要逐个账号去试写操作。

## 2. 核对当前账号

- `gh auth status --hostname <host>`：看 `Active account: true` 的那个账号。不要加 `--show-token`。
- 某条命令实际用的账号：`gh api --hostname <host> user --jq .login`（加上与该命令相同的前缀；`gh api` 不带 `--hostname` 时总是问 github.com）。
- 活动账号就是仓库账号时，直接运行，不加前缀。

## 3. 只对一条命令生效（推荐 A）

**A. `GH_TOKEN`（推荐）**

```bash
GH_TOKEN="$(gh auth token --hostname <host> --user <账号> || echo none)" gh <命令> … -R <host>/<owner>/<name>
```

- 不需要任何准备；令牌只在这一条命令的子进程环境中，不进入命令行参数与输出。
- `|| echo none` 不能省：账号没有登录时 `gh auth token` 失败、替换结果为空，而 gh 把空的 `GH_TOKEN` 当作没有设置，会回退到活动账号执行（T279 实测：`gh api user` 返回了活动账号）。有了它，取令牌失败时 gh 用无效令牌请求，以 “Bad credentials (HTTP 401)” 失败，不会换成别的账号。
- 命令替换必须放在双引号里；每条需要该账号的 gh 命令各自带前缀，不 `export`，不先存进变量。
- GitHub Enterprise Server 主机用 `GH_ENTERPRISE_TOKEN` 代替 `GH_TOKEN`，写法相同；github.com 与 `*.ghe.com` 用 `GH_TOKEN`。
- **写操作之前先核对一次**（建 PR、合并、推送之前，同一会话中做过一次即可）：

  ```bash
  GH_TOKEN="$(gh auth token --hostname <host> --user <账号> || echo none)" gh api --hostname <host> user --jq .login
  ```

  输出必须恰好是 `<账号>`。失败（401）或是别的账号时停下：不要换做法重试，告诉用户该账号需要由用户自己 `gh auth login`（worker 用 `ask_user`）。
- 带前缀时 `gh auth status` 显示的是环境令牌，不是账号名；核对用上面的命令。

**B. `GH_CONFIG_DIR`**（只在配置的 `accounts.<名>.auth.config_dir` 已由用户准备好时用）

```bash
GH_CONFIG_DIR="<config_dir>" gh <命令> … -R <host>/<owner>/<name>
```

- 该目录是用户为这个账号准备的 gh 配置：`config.yml` 与 `hosts.yml` 的副本，`hosts.yml` 中该主机的 `user` 改为这个账号，令牌仍在系统钥匙串中。命令里不取令牌。
- 目录在数据目录与执行目录之外，不由会话创建或修改；不存在或不能用时改用 A，并在报告中说明。
- 写操作之前同样先核对：`GH_CONFIG_DIR="<config_dir>" gh api --hostname <host> user --jq .login` 必须恰好是 `<账号>`。
- 令牌以明文存放在 `hosts.yml` 的机器上复制会多一份凭据，用户登录状态变化后副本可能过时，所以它不是默认做法。

## 4. git push 用哪个账号

git 不经 gh 认证，而是用自己的凭据助手（`git config --get-all credential.helper` 查看）。

- 凭据助手是 Git Credential Manager（`manager`）：用 `git -c credential.username=<账号> push …` 只对这一条命令指定账号。远端 URL 已带用户名时以它为准。
- 凭据助手是 `gh auth git-credential`：用 §3 A 的前缀，`GH_TOKEN="$(…)" git push …`。
- 推送被拒并显示别的账号名（“Permission to <owner>/<name>.git denied to <别的账号>”），就是账号不对；不要换成全局设置去“修”。

## 5. 合并

```bash
GH_TOKEN="$(gh auth token --hostname github.com --user <账号> || echo none)" gh pr merge <编号> -R github.com/<owner>/<name> --squash
```

- 单独运行：不与其他命令用 `;`、`&&`、`|` 连接，不加 `cd` 或别的环境变量前缀。§3 的一个前缀（A：`GH_TOKEN`，GitHub Enterprise Server 为 `GH_ENTERPRISE_TOKEN`；或 B：`GH_CONFIG_DIR`）是 worker 说明与用户设置中的 auto 模式规则“TaskSteward Worker Merge”认可的唯一前缀（CC008 §6）；前缀里命令替换中的 `|| echo none` 是这个前缀的一部分，不算连接其他命令。
- 前缀只选择账号，不改变合并的目标与授权：hooks 照常检查仓库的合并授权与 `-R` 目标。

## 6. 禁止

- `gh auth switch`、`gh auth login` / `logout`、`gh config set`：改的是全局状态，并行的其他会话会在不知情时换了账号。
- `git config --global` 改凭据设置；把用户名或令牌写进远端 URL（`git remote set-url`）。
- 输出、记录或保存令牌：单独运行 `gh auth token`、`--show-token`、`echo $GH_TOKEN`、`export GH_TOKEN`、把令牌写进文件、命令行参数（如 `-H "Authorization: …"`）、URL、日志、报告或提交。
- 在执行目录或 worktree 写 `.claude/settings*.json`、`.env` 一类设置或环境文件来固定账号：app 会对每个新任务再要求信任（CC001 §3、CC008 §6），hooks 也会拒绝编辑设置文件。
- 修改 tsw 配置中的账号：给仓库写 `auth_profile` 是权限类配置，由主会话按用户原话 `config_change`；worker 在报告中给出建议。
