# Linux UI 与独立启动脚本

Linux 入口使用 Python 和浏览器界面，不需要 PowerShell 或 Windows 桌面。UI 先生成版本化的 Bash 脚本，再执行同一份脚本。脚本可以单独运行，也可以交给 systemd、Supervisor 等进程管理工具。

当前控制台预览包面向 Linux x86_64，包含 UI 和固定版本的 psutil，不包含 Python、推理 worker 或模型。需要系统 Python 3.10+、Bash、coreutils、util-linux，以及与当前启动契约匹配的 Linux NInfer worker。Windows `.exe` 和不支持 `--kvmem-capabilities` 的历史 worker 不能用于此入口。其他后端尚未完成本轮 Linux GPU 验收，不在预览包中声明支持。

## 启动 UI

解压后在目录中运行：

```bash
bash scripts/linux/start-ui.sh --check
bash START-HERE-CN.sh
# English
bash START-HERE-EN.sh
# 无桌面服务器
bash scripts/linux/start-ui.sh --no-browser
```

控制台默认只监听 `127.0.0.1:18199`。远程机器可通过 SSH 转发访问：

```bash
ssh -L 18199:127.0.0.1:18199 user@server
```

然后在本机浏览器打开 `http://127.0.0.1:18199/monitor/`。页面中的路径指 Linux 服务器上的文件；Linux 使用路径输入和模型目录扫描。`--port` 可以更换控制台端口，推理服务必须使用不同端口。

源码运行时可显式安装 `scripts/requirements-console.txt` 到自己的 Python 环境，并通过 `KVMEM_PYTHON=/absolute/path/to/python` 选择解释器。入口不会自动安装软件。预览包将固定的 Linux psutil wheel 内容放入 `runtime/python-libs`，不分发不可迁移的开发机 venv。

## 三种运行方式

| 操作 | 管理者 | 关闭浏览器 | 关闭控制台 |
| --- | --- | --- | --- |
| 生成并启动／启动此版本 | 控制台临时进程管理器 | 继续运行 | 终止临时进程组 |
| 在终端执行生成的 `run.sh` | 当前终端或外部守护工具 | 无影响 | 无影响 |
| 后台服务管理中启动 | systemd | 继续运行 | 继续运行 |

脚本以前台方式直接 `exec` worker，不自行添加 `nohup`、`&` 或重启循环。systemd 负责异常恢复。相同配置名称的不同版本共用运行锁，重复启动退出码为 75；脚本预检失败退出码为 78。缺失 worker、模型、凭据或 worker 哈希变化会阻止执行。

每次生成创建独立 revision，不覆盖已有脚本。脚本、参数快照和服务文件保存摘要；手工修改后，UI 会拒绝自动执行或管理该版本。可以保留为自己的脚本，或在 UI 中生成新版本。安装包、worker 或模型路径移动后应重新生成。

## 用户后台服务

1. 点击“生成启动脚本”，选择“用户服务”并生成文件。
2. 点击“登记后台服务”，将固定版本登记到本账户的 systemd 用户目录。
3. 在“后台服务管理”中点击“启动”。“启用自启”是单独操作。

关闭控制台后服务仍运行；重开控制台可恢复选中的服务、日志、聊天连接和凭据引用。日志按该 unit 的单次运行标识读取；停止后保留同一版本最近一次已观察运行的日志，不与其他运行混合。

“解除当前连接（不停止服务）”只解除控制台与该服务的连接，服务及其登记仍保留。unit 已在终端移除或登记失效时也可解除连接，再选择其他服务或启动新的临时实例。端口占用仍会阻止重复启动。

用户服务通常在登录时启动。若需要无人登录也运行，由用户在终端明确执行：

```bash
loginctl enable-linger "$USER"
```

UI 不自动启用 linger。若提示用户会话不可用，确认发行版安装了用户 D-Bus 组件（Ubuntu 为 `dbus-user-session`），并具有有效的 systemd 用户会话。WSL 的会话及整机启动行为不能代替原生 Linux 开机验收。

## 系统后台服务

以实际运行推理的普通账户生成脚本，选择“系统服务”，将运行账户设置为该普通账户。不要用 root 运行推理；所选账户必须能遍历路径、读取模型和凭据并执行 worker。

生成结果会显示完整的 `sudo install` 和 `sudo systemctl daemon-reload` 命令。在终端完成安装后，点击“登记后台服务”。浏览器不收集 sudo 密码。系统服务的启停或自启需要系统授权；无授权时在管理员终端执行显示的固定 unit 命令。

```bash
sudo systemctl start kvmem-my-model.service
sudo systemctl enable kvmem-my-model.service
sudo systemctl stop kvmem-my-model.service
sudo systemctl disable kvmem-my-model.service
```

服务文件使用 `Type=exec`、`Restart=on-failure`、10 秒重试间隔和 300 秒内最多 3 次启动限制。主动停止不会重新启动；此策略不检测“进程存活但请求卡死”。系统服务会声明程序、脚本和模型的挂载路径依赖。

替换已登记版本前先停止服务，再生成并登记新版本，最后显式启动。旧版本保留，可以选择旧 revision 回退。用户服务须先停止并取消自启，才可移除登记；移除不会删除生成的脚本和凭据。系统服务文件由管理员在终端移除。

## API 密钥

默认密钥只保留在控制台内存。普通导出文件不写入明文密钥；独立脚本通过 `KVMEM_API_KEY` 获取它。后台服务不能依赖 UI 内存，必须明确勾选保存到本机私有文件，或使用 CLI 的 `--api-key-file` 指向既有文件。

保存的凭据位于配置目录下的 `credentials/<revision>.key`，权限为 0600；目录为 0700。分享脚本和配置时，不要分享凭据目录。worker 原生接口仍使用 `--api-key` 参数，密钥可能对有权限读取进程参数的本机账户可见；这与现有 worker 的接口一致。

Linux 控制台状态目录设为 0700，原始日志设为 0600；重开时会收紧既有状态目录及日志的宽松权限。状态目录应为本账户专用目录。UI 和 CLI 的后台日志都先恢复凭据引用、脱敏，再按完整行截取最近内容；凭据无法读取时不返回原始 journal。

## 无 UI 命令行

配置 JSON 使用控制台 schema 1。参数中的 GPU 应为 UUID，而不是不稳定的编号。

```bash
python3 scripts/launcher-cli.py plan --config config.json
python3 scripts/launcher-cli.py export --config config.json --profile my-model
python3 scripts/launcher-cli.py verify --profile my-model --revision <revision>
python3 scripts/launcher-cli.py service --action register --profile my-model --revision <revision>
python3 scripts/launcher-cli.py service --action start --profile my-model
python3 scripts/launcher-cli.py service --action logs --profile my-model
python3 scripts/launcher-cli.py service --action stop --profile my-model
```

系统服务在导出、登记和管理时均传 `--scope system`，导出还需 `--user <普通账户>`。CLI 全局选项 `--profiles-root` 放在子命令前，UI 也支持同名选项。默认版本目录为 `${XDG_CONFIG_HOME:-~/.config}/kvmem/profiles`；控制台状态在 `${XDG_STATE_HOME:-~/.local/state}/kvmem/console`。

## 打包与验证边界

`scripts/linux/package-console.py` 接收已构建聊天资源、固定版本 wheel 及预先确定的 SHA-256，生成新目录和 `.tar.gz`，拒绝覆盖已有包。产物包含来源信息、组件哈希、许可和 `SHA256SUMS`，不会编译引擎或在启动时下载内容。

```bash
python3 scripts/linux/package-console.py --output /path/to/new-package \
  --chat-ui /path/to/built-ui --psutil-wheel /path/to/psutil.whl \
  --psutil-sha256 <publisher-verified-sha256>
```

普通主机测试使用假 worker 验证 argv、信号、版本锁及退出码。`KVMEM_TEST_SYSTEMD=1` 显式启用真实用户服务测试，使用随机 unit 名并自动清理。无 GPU 的测试不证明模型正确性；WSL 测试不证明物理机重启后的自动启动。正式分发前必须单独记录发行版、CUDA ABI、真实 GPU 模型请求及原生开机验证结果。
