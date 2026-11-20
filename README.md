# 内网 APT 源（FastAPI）

单发行版、单组件、单架构的 APT 软件源。每次发布先落盘、后切换
`Release`，启用 **Acquire-By-Hash**，并只保留最近两次成功发布所引用的
索引和安装包。拿到旧 `Release` 的客户端仍可按其中的 SHA-256 取回旧索引，
再按索引校验下载安装包；同名同版本重新构建也互不覆盖。

## 快速开始（docker compose）

```bash
docker compose up -d --build
curl -s http://127.0.0.1:8080/api/status | python3 -m json.tool
```

上传一批 `amd64` 的 `.deb`（整版发布，一次请求一批）：

```bash
curl -fsS -X POST http://127.0.0.1:8080/api/publish \
  -F files=@hello-internal_1.0.0_amd64.deb \
  -F files=@libfoo_2.1.0_amd64.deb | python3 -m json.tool
```

也可以直接用浏览器打开 <http://127.0.0.1:8080/docs> 的上传表单
（OpenAPI 文档在 `/swagger-ui`）。

## 客户端配置与实测

```bash
sudo ./clients/setup-client.sh http://127.0.0.1:8080 stable main
# 等价于写入 /etc/apt/sources.list.d/internal.list：
#   deb [trusted=yes arch=amd64] http://127.0.0.1:8080/ stable main

sudo apt-get update
apt-get download hello-internal          # 下载并校验
sudo apt-get install hello-internal      # 或直接安装
```

仓库未签名，因此用 `trusted=yes`。若要签名，增加 `Release.gpg`/`InRelease`
并把 `trusted=yes` 换成 `signed-by=/usr/share/keyrings/internal.gpg`。

一键端到端验证（发布两版、`apt-get update`/`download`、旧 Release 客户端
按哈希取包、第三版触发淘汰）：

```bash
sh scripts/e2e-apt.sh http://127.0.0.1:8080
```

实测输出示例：

```
== release 1 published (index 71440bc1...)
== apt-get download smoke-one -> smoke-one_1.0.0_amd64.deb
== release 2 published (index 6fc51fd9...)
== apt-get download smoke-two -> smoke-two_2.0.0_amd64.deb
== previous release index and packages verified via by-hash
== retention keeps exactly the two most recent releases
== OK
```

## 发布语义

| 需求 | 实现 |
| --- | --- |
| 新版编制期间继续供包 | 新索引/包先写入不可变路径（`by-hash/SHA256/<hash>`、内容寻址的 pool 文件），旧 `Release` 与旧索引在此期间保持不变 |
| 切换时索引与包同时可下载 | `Release` 用原子 `rename` 作为唯一提交点；在此之前所有被引用的文件都已 fsync 落盘 |
| 旧 Release 客户端仍可取旧索引 | `Acquire-By-Hash: yes` + `dists/stable/main/binary-amd64/by-hash/SHA256/<sha256>`，`dists/stable/by-hash/SHA256/<sha256>` 亦提供 Release 本体 |
| 同名同版本重发 | pool 文件名带内容哈希：`<pkg>_<ver>_<arch>_<sha16>.deb`，两版并存 |
| 只开放最近两次成功发布 | GC 以 journal 中最新 2 条 `committed` 记录为根，删除其余 by-hash 索引与 pool 文件 |
| 坏包 / 提交前中断两版照旧 | 上传先完整校验；任一文件非法则整批拒绝。提交中断时旧 `Release` 不变，重启后 `reconcile()` 回滚 |
| 重启后仍可取用 | 启动时对比磁盘上 `Release` 的哈希与 journal 中 `staged` 记录：一致则补记为 `committed`，否则标记 `aborted` 并清理 |

### 目录结构（数据卷内）

```
/data
├── export/                                  # 只读 HTTP 根目录
│   ├── dists/stable/Release
│   ├── dists/stable/by-hash/SHA256/<release-sha256>
│   ├── dists/stable/main/binary-amd64/Packages(.gz)
│   ├── dists/stable/main/binary-amd64/by-hash/SHA256/<index-sha256>
│   └── pool/main/<bucket>/<pkg>/<pkg>_<ver>_<arch>_<sha16>.deb
├── incoming/<token>/                        # 上传暂存，发布后删除
└── state.sqlite3                            # 发布 journal（GC 的唯一依据）
```

`Release` 同时列出 `Packages` 与 `Packages.gz`：APT 2.6 以未压缩名识别
索引目标，只列 `Packages.gz` 会被静默跳过（本项目最初即踩此坑）。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/publish` | multipart 字段 `files`，一次一批，返回 201 与发布记录 |
| POST | `/api/rehearsals` | 解包顺序演练：模拟文件归属与 `Replaces` 冲突，不改仓库状态 |
| GET | `/api/status` | 当前配置、最新发布、发布历史 |
| GET | `/api/publications` | 发布历史（含 `aborted`） |
| GET | `/docs` | 上传表单 |
| GET | `/swagger-ui` | OpenAPI 文档（演练接口的请求结构亦在此填写） |
| GET/HEAD | `/dists/...`、`/pool/...` | 仓库文件（只读） |

错误语义：包非法 / 重复的同名同版本 / 批次超限返回 `400`/`413`，且
仓库状态完全不变。

## 解包顺序演练（rehearsal）

排定一批 `.deb` 的解包顺序后，先在仓库上做一次纯模拟，确认哪一步会
覆盖别的包的文件。演练**不复制 `.deb`、不改任何发布状态**；受理后即使
所选发布被新发布淘汰，仍通过已打开的文件描述符用原包跑完整个流程
（POSIX inode 语义，GC 只删目录项）。

请求体（也可直接在 `/swagger-ui` 中填写）：

```json
{
  "publication_id": 7,
  "package_sha256": [
    "9f2c…（2～8 个，按解包顺序，同名包只选一个）",
    "1b8e…"
  ],
  "installed": [
    {
      "name": "libold",
      "version": "1.5-2",
      "files": ["/usr/lib/libold.so.1", "/usr/bin/oldtool"]
    }
  ]
}
```

- `publication_id` 必须是**仍可下载**的发布（保留窗口内的 `committed`
  记录）；未知或已淘汰发布返回 `404 publication_unavailable`。
- `package_sha256` 为 2～8 个小写十六进制 SHA-256，按计划解包顺序排列；
  同一摘要不得重复。包不在所选发布中返回
  `404 package_not_in_publication`；池文件读取/解析失败返回
  `422 package_read_failed`；清单结构非法返回 `400 invalid_manifest`。
- `installed` 是目标机现状：每个已装包给出名称、Debian 版本及其拥有的
  **普通文件绝对路径**；同一台机器上同一路径不能归两个包所有。

模拟规则（每步通过后才推进状态，遇首个受阻步骤即停）：

1. 从每个 `.deb` 的数据归档读取普通文件（目录、符号/硬链接、设备节点
   均不算），把归档内相对路径（如 `./usr/bin/x`）换算成安装后的绝对
   路径（`/usr/bin/x`），并读取控制字段 `Replaces`。
2. 本包已拥有的路径可直接覆盖。
3. 覆盖**别的包**的路径时，该包的 `Replaces` 必须匹配路径**当时**的属主
   包名与属主版本（无版本子句匹配任意版本；带版本子句按 Debian Policy
   5.6.3 的版本比较判定，支持 `<<`、`<=`、`=`、`>=`、`>>` 与 `:arch`
   限定）。
4. 通过后更新模拟的包版本与文件归属，并释放该包仍占有、但新版不再
   提供的路径；这些被释放的路径随后可被后续步骤的包取得。

响应始终回带 `publication_id`。全部通过：

```json
{
  "publication_id": 7,
  "result": "no_conflict",
  "steps": [
    {"step": 1, "package": "libnew", "version": "2.0",
     "sha256": "9f2c…", "status": "applied", "files": 12}
  ]
}
```

存在冲突时给出**首个受阻步骤**的包、冲突路径以及当时的属主包名和版本：

```json
{
  "publication_id": 7,
  "result": "conflict",
  "steps": [{"step": 1, "package": "libnew", "version": "2.0",
             "sha256": "9f2c…", "status": "blocked"}],
  "conflict": {
    "step": 1,
    "package": "libnew",
    "version": "2.0",
    "sha256": "9f2c…",
    "path": "/usr/lib/libold.so.1",
    "owner_package": "libold",
    "owner_version": "1.5-2"
  }
}
```

## 配置

| 环境变量 | 默认值 |
| --- | --- |
| `APT_REPO_DATA_DIR` | `./data`（容器内 `/data`） |
| `APT_REPO_DISTRIBUTION` | `stable` |
| `APT_REPO_COMPONENT` | `main` |
| `APT_REPO_ARCHITECTURE` | `amd64` |
| `APT_REPO_ORIGIN` / `APT_REPO_LABEL` | `Internal APT Repository` |
| `APT_REPO_RETENTION` | `2` |
| `APT_REPO_MAX_FILE_BYTES` | `536870912` |
| `APT_REPO_MAX_BATCH_FILES` / `_BYTES` | `500` / `4 GiB` |

## 操作约束

- **单写入进程**：发布通过进程内线程锁 + `flock` 串行化，`docker compose`
  固定 `--workers 1`。多副本部署时需改为共享存储上的分布式锁。
- **unsigned**：默认不签名；生产环境应补 `InRelease`/`Release.gpg`。
- **下载与 GC 竞态**：响应通过已打开的 fd 流式发送，即使文件随后被 GC
  删除，进行中的下载仍能读到完整内容（POSIX inode 语义）。
- 容器以只读根文件系统、非 root（uid 10001）、`no-new-privileges` 运行。

## 本地开发与测试

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest httpx
uvicorn app.main:app --reload          # 数据写入 ./data
pytest -q                              # 49 项测试
```

测试覆盖：发布/回读、旧 Release 按哈希取旧索引与旧包、同名同版本重发、
两版保留与第三版淘汰、坏包整批拒绝、错误架构拒绝、提交前中断回滚、
提交后崩溃恢复、下载中被 GC、Release 格式与 by-hash 服务、路径穿越；
以及解包演练的同包覆盖与路径释放、`Replaces` 属主/版本匹配（与 dpkg
版本算法对拍）、首个冲突步骤报告、清单/发布/读取三类独立报错、受理后
发布被淘汰仍能完成、演练对仓库零改动。
