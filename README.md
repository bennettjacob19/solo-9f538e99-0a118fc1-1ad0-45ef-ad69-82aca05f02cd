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
（OpenAPI 文档在 `/swagger-ui`）。解包顺序演练的请求体同样可以在
`/swagger-ui` 的 `POST /api/rehearse` 表单里填写，见下文。

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
| POST | `/api/rehearse` | 解包顺序演练（只读，见下节） |
| GET | `/api/status` | 当前配置、最新发布、发布历史 |
| GET | `/api/publications` | 发布历史（含 `aborted`） |
| GET | `/docs` | 上传表单 |
| GET | `/swagger-ui` | OpenAPI 文档（含 `/api/rehearse` 的请求表单） |
| GET/HEAD | `/dists/...`、`/pool/...` | 仓库文件（只读） |

错误语义：包非法 / 重复的同名同版本 / 批次超限返回 `400`/`413`，且
仓库状态完全不变。

## 解包顺序演练（`POST /api/rehearse`）

判断「按给定顺序解包这一批 `.deb`，哪一步会覆盖别的包的文件」。演练只读
发布中的包，**不复制 `.deb`、不改发布状态**，可随时重复执行。

请求体（在 `/swagger-ui` 里可直接填表）：

```json
{
  "release": 3,
  "packages": ["<sha256-of-first-unpacked.deb>", "<sha256-of-second.deb>"],
  "installed": [
    {"name": "alpha", "version": "1.0", "files": ["/usr/share/x/shared"]}
  ]
}
```

- `release`：发布号（`/api/publish` 返回的 `id`，即 `/api/status` 中的发布记录号）。
  必须是仍可下载的已提交发布；被淘汰或不存在时单独报错。
- `packages`：**2～8 个** `.deb` 的 SHA-256，数组顺序即解包顺序；同名包只能选一个。
  SHA-256 必须属于所选发布。
- `installed`：目标机已安装的包，含名称、Debian 版本，以及各自拥有的
  **普通文件绝对路径**。目录、符号链接不算文件归属。

规则：

- 包覆盖自己拥有的文件总是允许，与版本无关。
- 覆盖别的包的文件，必须由本包的 `Replaces` 命中**当时的属主包名**，且
  `Replaces` 的版本约束要匹配**当时的属主版本**（Debian 版本序，含 epoch/~）。
- 每一步通过后，模拟状态更新：该包版本变为新版本，文件归属改到该包名下；
  旧版本拥有、新版本不再提供的路径被释放（无属主），后续包可直接安装。
- 首个受阻步骤即结束，返回该步骤的包名/版本、冲突路径、当时的属主包名/版本。
  全部通过则报告 `result: "ok"`（无冲突）。

成功响应（HTTP 200）示例：

```json
{
  "release": 3,
  "result": "conflict",
  "conflict": {
    "step": 2, "package": "gamma", "version": "2.0",
    "path": "/usr/share/x/shared",
    "owner_package": "delta", "owner_version": "2.0"
  },
  "steps": [{"step": 1, "package": "delta", "overwritten": 1, "released": []}],
  "first_blocked_step": 2,
  "conflict_path": "/usr/share/x/shared",
  "owner_package": "delta",
  "owner_version": "2.0"
}
```

错误是分开的，统一为 `{"error": "rehearsal_rejected", "release": N, "reason": {...}}`：

| 状态码 | `reason.code` | 含义 |
| --- | --- | --- |
| 400 | `invalid_manifest` | 清单有误：包数不在 2～8、重复 SHA-256、同名包重复、版本或路径非法等 |
| 404 | `release_not_available` | 发布号不存在、未成功提交，或已被保留策略淘汰 |
| 409 | `package_not_in_release` | 所选 SHA-256 不属于该发布（`details` 列出缺失项） |
| 422 | `package_unreadable` | 发布索引里列有该包，但 `.deb` 读不出或内容与索引不符 |

接收演练时会在持有发布锁的情况下打开所有所选包的描述符，之后才开始模拟；
因此**即使演练期间有新发布淘汰了所选发布，本次演练仍用原包完成**。新版照常
发布，过期包照常从公共下载地址移除，两者互不影响。

命令行示例：

```bash
curl -fsS -X POST http://127.0.0.1:8080/api/rehearse \
  -H 'content-type: application/json' \
  -d @drill.json | python3 -m json.tool
# 或直接从仓库里的 .deb 生成 SHA-256：
sha256sum *.deb
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
pytest -q                              # 55 项测试
```

测试覆盖：发布/回读、旧 Release 按哈希取旧索引与旧包、同名同版本重发、
两版保留与第三版淘汰、坏包整批拒绝、错误架构拒绝、提交前中断回滚、
提交后崩溃恢复、下载中被 GC、Release 格式与 by-hash 服务、路径穿越；
以及解包演练：无冲突、首个受阻步骤、`Replaces` 命中/版本不匹配、
同包自覆盖、版本随步骤推进、旧文件释放、符号链接不属文件、包数/重复
SHA-256/同名包/路径校验、失效发布与包不在发布中、包不可读、演练不改
仓库、演练期间发布被淘汰仍用原包完成、Debian 版本序与 `Replaces` 解析。
