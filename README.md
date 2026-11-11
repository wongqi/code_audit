# Code Audit

通过 SSH 采集主机上的 Git 状态，将结果保存为本地快照，再离线比较代码版本。命令行和网页共用采集、比较和导出逻辑，无需安装本项目。

主要用于确认：仓库是否存在、分支是否一致、提交是否一致，以及各主机是否有本地更改。采集与比较严格分离，不执行代码拉取、切换分支或同步操作。

## 1. 首次使用

本机需要 Linux/POSIX 环境及 Python 3.9+；被采集主机需要 Python 3 和 Git，不依赖 ROS。

进入项目目录，创建虚拟环境并安装依赖：

```bash
cd ~/code_audit                    # 改为项目的实际路径
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

准备本地配置。已有 `configs/config.yaml` 时跳过复制：

```bash
cp -n configs/config.example.yaml configs/config.yaml
```

编辑 `configs/config.yaml`，填写主机地址、SSH 认证信息、代码路径及 GitHub 组织。示例中的地址、账号和组织均为占位值，需要替换。

查看命令帮助：

```bash
python audit.py --help
python audit.py collect --help
python audit.py compare --help
python audit.py serve --help
```

## 2. 每次打开终端

虚拟环境和依赖只需创建、安装一次。以后进入项目并激活环境即可：

```bash
cd ~/code_audit                    # 改为项目的实际路径
source .venv/bin/activate
```

然后选择命令行操作或启动网页：

```bash
python audit.py collect github
python audit.py collect X30-A
python audit.py compare X30-A
# 或：
python audit.py serve
```

`compare X30-A` 表示比较同一主机最近两次完整快照，因此需要至少采集过两次。

使用完毕后，可运行 `deactivate` 退出虚拟环境。

## 3. 配置主机与 GitHub

完整示例见 [configs/config.example.yaml](configs/config.example.yaml)。配置采用“设备 → 主机 → 代码路径”三层结构：

```yaml
devices:
  X30-1:
    hosts:
      X30-A:
        ssh:
          host: 192.0.2.20
          username: robot
          password: "替换为 SSH 密码"
        code_paths:
          - /home/robot/projects

github:
  organization: example-organization
  token: "替换为 GitHub 令牌"
```

- **设备**只用于名称分组和快照目录分层，命令不接受设备名。
- **主机**是采集和比较的选择对象。名称唯一时可写 `X30-A`；重名或与设备名冲突时写 `X30-1/X30-A`。
- **代码路径**可以是包含多个仓库的目录，也可以直接指向一个 Git 仓库。支持绝对路径和 `~/`，同一主机下不能重复或嵌套。
- github 是保留选择名称。GitHub 当前仅支持组织账户，采集组织内当前令牌可访问的所有仓库、所有分支；不支持个人账户的仓库列表。

### SSH 认证与跳板

`ssh.port` 默认为 22。可以直接填写密码，也可以使用 SSH 密钥：

```yaml
ssh:
  host: 192.0.2.20
  username: robot
  key_filename: ~/.ssh/id_ed25519
  # 密钥有口令时填写：
  # key_password: {env: AUDIT_SSH_KEY_PASSWORD}
```

相对密钥路径以配置文件所在目录为基准。`allow_agent` 和 `look_for_keys` 默认均为 true，可使用本机 SSH agent 或常规密钥文件。

需要跳板时，在目标主机的 ssh 下填写 `jump: FHZN-A`。跳板也必须在配置中定义；同设备内可用简称，跨设备可写完整的“设备/主机”。仅选择目标主机采集时，不会顺带扫描跳板上的代码。

首次连接会要求确认主机密钥。CLI 输入 yes 后回车；网页点击接受。确认结果保存到 `~/.ssh/known_hosts`，已有主机密钥发生变化时直接报错。

### 使用环境变量

密码、密钥口令和 GitHub 令牌均支持环境变量引用：

```yaml
password: {env: AUDIT_SSH_PASSWORD}
token: {env: GITHUB_TOKEN}
```

使用引用时，在启动程序的同一终端中设置对应变量：

```bash
export AUDIT_SSH_PASSWORD='替换为 SSH 密码'
export GITHUB_TOKEN='替换为 GitHub 令牌'
```

也可以直接在仅供本地使用的 `config.yaml` 中填写凭据。Git 只提交示例配置；实际配置不提交。

### 可选设置

`settings` 可整体省略。以下为默认值：

| 字段 | 默认值 | 含义 |
|---|---|---|
| snapshots_dir | ../snapshots | 相对配置文件目录的快照保存位置 |
| concurrency | 3 | 并发采集主机数，最多 3 台 |
| connect_timeout | 10 | SSH 连接超时，秒 |
| host_timeout | 180 | 单台主机采集超时，秒 |
| github_timeout | 20 | 单次 GitHub API 请求超时，秒 |

`GitHub Enterprise` 可额外配置 `github.api_url` 和 `github.web_host`。

程序默认读取项目中的 `configs/config.yaml`，与当前终端目录无关。也可以指定其他配置：

```bash
python audit.py --config configs/custom.yaml collect X30-A
```

修改配置后，需要重启已运行的网页服务。

## 4. 命令行采集

```bash
# 一台主机：可用唯一简称或完整名称
python audit.py collect X30-A
python audit.py collect X30-1/X30-A

# 多台主机
python audit.py collect X30-A FHZN-A CYVET-A

# 所有配置主机，不包含 GitHub
python audit.py collect --all

# GitHub 的所有仓库、所有分支
python audit.py collect github

# 同一任务中采集主机和 GitHub
python audit.py collect X30-A github

# 隐藏常规进度，保留结果和错误
python audit.py collect github --quiet
```

`--all` 不能与名称混用。采集结束会打印完整性、仓库数量和快照保存路径，不会自动比较。

主机采集当前分支、完整提交 SHA、提交时间与摘要、远端、跟踪目标和 Git 状态。自动发现普通仓库、Git worktree 和嵌套仓库，不采集其他分支的状态。扫描不跟随子目录符号链接，跳过 `.git`、build、install、log、`__pycache__` 和 `.venv`；需要扫描这些名字的目录时，可直接配置为代码路径。

采集通过 SSH 标准输入执行 Python 程序，不在远端安装脚本，不读取源码正文、计算文件内容哈希或扫描 ROS 包。被 Git 忽略的文件不进入更改清单。

CLI 按 **Ctrl+C** 停止采集。已保存的快照保留。单个对象失败不阻止其他对象保存结果，不完整结果也会保存并注明错误。

## 5. 命令行比较与导出

所有比较只读取本地快照，不连接 SSH 或 GitHub。按主机名称选择时，默认采用最近的完整快照，跳过不完整快照并给出提示。

```bash
# 两台主机最近的完整快照
python audit.py compare X30-A CYVET-A

# 同一主机最近两次完整快照：旧 → 新
python audit.py compare X30-A

# 主机与本地 GitHub 快照；左右顺序可交换
python audit.py compare X30-A github

# 精确指定两份文件，也可显式选择不完整快照
python audit.py compare ./old.json ./new.json

# 名称和文件可以混用
python audit.py compare X30-A ./baseline.json

# 显式导出：同时生成 HTML 和 JSON
python audit.py compare X30-A CYVET-A --export
```

`--export` 是开关，后面不接文件名。导出到项目的 `exports/<时间>_<随机标识>/` 下，每次创建新目录；不加此参数只打印终端结果，不生成报告文件。

只传一个参数时必须是主机名称，用于历史比较。传两个参数时可使用主机名称、github 或快照文件；文件用 `.json` 后缀或明确的路径识别。不支持 GitHub 与 GitHub 比较。

## 6. 使用网页

```bash
python audit.py serve
# 换端口：
python audit.py serve --port 8766
```

默认访问 **http://127.0.0.1:8765**。通过远程 SSH 使用时，在 VS Code 的“端口”面板转发 8765，再在本机浏览器中打开转发地址。服务仅绑定回环地址，不自动打开浏览器。

### 采集状态

1. 勾选主机或 GitHub，点击“开始采集”。
2. 查看进度，首次连接时确认主机密钥。
3. 如需中断，点击“停止采集”。
4. 完成后，快照列表自动更新；不会自动替换当前比较结果。

“全部配置主机”不包含 GitHub，需要另外勾选。CLI 与网页共用采集锁，同一时间只能运行一个采集任务。关闭页面不会停止任务，重新打开可查看服务内当前或最近一次任务；重启服务后只保留已保存快照。

### 比较快照

1. 左右分别选择主机或 GitHub，再选快照。选择或更换快照后自动比较，也可点击“比较所选快照”。
2. 快照按时间正序排列，最新在最后，默认选择最近的完整快照。同一主机默认选最近两份完整快照。
3. 可手动载入电脑上的 JSON 文件，仅用于本次比较，不上传保存为快照。
4. 使用搜索、代码路径和比较结论筛选结果；展开“查看详情”查看路径、提交时间、完整 SHA、更改文件及错误。
5. 点击“导出 HTML + JSON”，下载包含两份文件的 ZIP。下载保存在浏览器所在电脑，不额外写入服务端报告目录。

网页与 CLI 支持相同的主机选择、快照比较和导出范围；网页仅能读取配置快照目录中的服务端文件，目录外文件需要手动载入。

## 7. 如何理解比较结果

| 结论 | 颜色 | 含义 |
|---|---|---|
| 版本一致 | 绿 | 分支与提交一致；GitHub 比较按目标分支头提交判断 |
| 版本不同 | 红 | 分支或提交不同，详情显示具体原因 |
| 单侧缺失 | 黄 | 完整快照中仓库只存在于一侧 |
| 无法判断 | 黄 | 采集失败、信息不足或仓库匹配仍有歧义 |

仓库优先按标准化远端地址匹配。多个副本先匹配相同绝对路径，剩余双方各一份时再配对；没有可识别远端时只按相同绝对路径匹配。游离 HEAD 不比较分支，仍比较提交。部分采集失败时，信息完整且能唯一配对的仓库仍独立判定。

GitHub 比较使用主机快照记录的跟踪远端和分支；没有跟踪目标时使用 `origin` 和本地同名分支。提交不同只表示 SHA 不同，不推断领先、落后或分叉；结果仅代表两份快照各自的采样时刻。

**本地更改单独显示，不影响“版本一致”结论，也不表示双方更改内容相同。** 文件按以下状态分组：

- **暂存的更改**：暂存区相对 HEAD 的更改。
- **未暂存的更改**：工作树相对暂存区的更改。
- **未跟踪的文件**：尚未纳入 Git 跟踪的文件。
- **合并冲突**：尚未解决的冲突。

同一文件既有暂存的更改，又有未暂存的更改时，分别列在两组。例如先暂存新增文件，再继续编辑，该文件会在第一组显示“新增”，在第二组显示“修改”。

退出码可用于脚本：

| 退出码 | 采集 | 比较 |
|---|---|---|
| 0 | 全部成功 | 完整且版本一致；允许存在本地更改 |
| 1 | 不使用 | 完整但存在版本差异或单侧缺失 |
| 2 | 存在失败或取消 | 有无法判断项或执行错误 |

## 8. 文件保存与项目结构

```text
code_audit/
├── audit.py                    # 命令行入口
├── requirements.txt            # Python 依赖
├── configs/
│   ├── config.example.yaml     # 可提交的示例配置
│   └── config.yaml             # 本地实际配置
├── code_audit/                 # 配置、采集、比较、存储、展示和 Web 服务
│   └── web/                    # HTML、CSS、JavaScript，无需前端构建
├── tests/                      # 临时仓库与模拟连接测试
├── snapshots/
│   ├── hosts/<设备>/<主机>/
│   │   └── 20261111_093103_b989b033.json
│   └── github/
│       └── 20261111_093103_b989b033.json
└── exports/
    └── 20261111_093500_a1b2c3d4/
        ├── result.html
        └── result.json
```

快照和导出目录按需创建。文件名为“年月日_时分秒_8位随机标识”，时间统一使用 UTC+08:00，页面显示为 2026-11-11 09:31:03。快照完整写入后才发布，不覆盖已有文件。

Git 提交保留示例配置、源码、依赖文件、测试和文档。实际配置、虚拟环境、快照、导出文件与运行时锁不属于源码；快照及导出仍包含实际仓库路径、分支、提交摘要等项目信息。程序会对已配置的凭据和常见令牌形式脱敏，但归档并非匿名数据。

## 9. 运行测试

```bash
cd ~/code_audit                    # 改为项目的实际路径
source .venv/bin/activate
python -m unittest discover -s tests -v
```

测试使用临时 Git 仓库和模拟 SSH/GitHub 连接，不连接实际主机或 GitHub，不修改本地实际配置和已有快照。
