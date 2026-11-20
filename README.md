# Offline OCI Image Importer

单实例 FastAPI 服务，用于隔离机房（air-gapped）通过移动盘更新容器镜像。
移动盘上只带**新版索引 + 新增 blob**；本服务把它们与机房存储卷上已有的 blob 合并成
一个完整的单镜像版本，校验通过后原子切换，并以完整 OCI Image Layout tar 对外提供。

- 目标镜像：**单个镜像**，同时包含 `linux/amd64` 与 `linux/arm64`。
- 输入：单个 OCI Image Layout tar（`oci-layout` + `index.json` + `blobs/sha256/...`）。
- 输出：同样的 OCI Image Layout tar（完整，含两个平台），带索引摘要响应头。

## 快速开始（Docker Compose）

```sh
docker compose up --build -d
# Swagger: http://127.0.0.1:8080/  （文档在 /docs，OpenAPI 在 /openapi.json）
```

首次启动会自动导入并启用 `samples/image-v1.tar`（由 `SEED_INITIAL_IMAGE` 指定，
仅在存储卷尚无现行版本时执行）。

演示（对运行中的实例跑完整流程）：

```sh
python3 scripts/demo.py --base http://127.0.0.1:8080
```

本机没有 Docker 时（本仓库的开发/验证环境即如此）：

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install --no-index --find-links vendor/wheels -r requirements-dev.txt
python scripts/run_local.py --port 8080
```

### 离线构建说明

`vendor/wheels/` 已包含运行期与测试期依赖的 wheel，且同时带有
`manylinux x86_64` 与 `manylinux aarch64` 的二进制包，因此 `Dockerfile` 中的
`pip install --no-index` 在两种架构的构建机上都无需联网。
Swagger UI 的 JS/CSS 也已放在 `app/static/`，页面不引用任何 CDN。
唯一需要外网的是**重新生成样例**（首次下载 busybox 的 deb）；生成后的
`samples/*.tar` 完全自包含。

## 目录结构

```
app/            FastAPI 服务（oci 解析、store、tar 流式输出、demo 路由）
app/static/     本地 Swagger UI 资产
tools/          样例/坏包构建器
scripts/        demo.py（端到端演示）、run_local.py、build-samples.sh
samples/        初始镜像、更新包、三个坏包、demo.json
tests/          导入、生命周期、流式下载、启用、blob 来源
vendor/wheels/  离线依赖（x86_64 + aarch64）
```

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/status` | 现行版本摘要、`generation`、平台 |
| GET | `/api/v1/versions` | 所有已导入版本（含未启用的暂存版本） |
| POST | `/api/v1/images/import` | 导入一个版本；`enable=true` 时校验并原子启用 |
| POST | `/api/v1/images/{digest}/enable` | 启用先前仅暂存的版本（再次核对预期摘要） |
| GET | `/api/v1/images/current/layout.tar` | 下载现行版本（完整 OCI tar） |
| GET | `/api/v1/images/{digest}/layout.tar` | 下载指定版本（固定，不受换版影响） |
| GET | `/healthz` | 存活检查 |
| GET | `/api/v1/demo/samples` | 演示包清单（仅 `ENABLE_DEMO=true`） |

### 导入

```
POST /api/v1/images/import
  file=<OCI layout tar>
  expected_current_digest=<现行 index.json 摘要 | none>
  enable=true                # true: 校验+启用；false: 仅校验+暂存
  source_name=<可选标签>
```

响应 `201` 给出新摘要、`generation`、每个 blob 的来源（`package` / `store`）与下载地址。
`enable=false` 时版本进入暂存（完整可下载，但不改变现行版本），随后用
`POST /api/v1/images/{digest}/enable` 启用——这正对应"启用前中断，继续服务旧版本"的场景：
进程在暂存后被终止，重启后现行指针仍指向旧版本。

### 错误语义

| 状态码 | code | 含义 |
|---|---|---|
| 400 | `bad_package` / `bad_expected_digest` | tar 结构非法、路径穿越、缺少 `oci-layout`/`index.json`、摘要格式错误 |
| 409 | `version_conflict` | 传入的 `expected_current_digest` 与当前启用版本不符；`stagedDigest` 指向已校验暂存的版本 |
| 422 | `blob_missing` | 某 blob 既不在包内也不在存储卷；响应含该 blob 的 `digest` |
| 422 | `blob_mismatch` | 大小或内容摘要不符；响应含 `digest`、期望/实际大小 |
| 422 | `validation_failed` | 版本索引缺平台、manifest/config 结构非法、diff_id 不符等 |
| 422 | `unsupported_platform` | 出现 `linux/amd64`、`linux/arm64` 之外的平台 |
| 503 | `no_active_version` | 尚无任何成功启用的版本 |
| 404 | `version_not_found` | 指定版本未导入 |

多个缺失/不符的 blob 会合并为一个 422，`details.issues[]` 逐个列出摘要与来源。

## 一致性模型

- **blob 存储**：`DATA_DIR/blobs/sha256/<hex>`，内容寻址、只增不改。包内 blob 若与
  存储卷副本同名但内容损坏，以包内为准并修复存储卷副本。
- **版本**：`DATA_DIR/versions/<hex>/{meta.json,index.json}`，由校验过程整体 staging
  后 `rename` 落位。导入失败或启用前中断都不会产生可见版本。
- **现行指针**：`DATA_DIR/state.json` 以临时文件 + `fsync` + `rename` 原子替换。
  `generation` 每次变更递增，用于确认"确实换版了"。
- **导入→启用**：先完成全部校验与 staging（不改变现行版本），再在锁内校验
  `expected_current_digest` 是否仍等于现行摘要；不符即 409，相符才写指针。
- **下载**：请求开始时把版本摘要固定下来，直接流式读取该版本的 blob 文件；
  blob 与版本目录永不就地改写，因此下载途中换版仍完整返回原版本；重启后
  `current` 指向重启前最后一次成功启用的版本。
- **单实例**：进程对 `DATA_DIR/lock` 加 `flock`，第二个进程启动即失败；
  Compose 中只运行一个容器。

## 输入格式

`index.json` 接受两种形态：

1. **Layout index**（标准 OCI 导出）：`index.json` 的 `manifests[0]` 指向版本索引 blob。
2. **扁平**：`index.json` 本身就是多平台版本索引（部分工具直接这样导出）。

版本索引必须恰好覆盖 `linux/amd64` 与 `linux/arm64`，每个平台一个 manifest；
manifest 的 config 必须声明与之匹配的 `os`/`architecture`，且 config 的
`rootfs.diff_ids` 数量与层数一致（未压缩层会逐层校验内容摘要）。
OCI 与 Docker v2 媒体类型都接受；下载统一输出 OCI 布局。

## 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `DATA_DIR` | `/data` | 唯一可写目录 |
| `SEED_INITIAL_IMAGE` | 空 | 初始镜像 tar；仅在无现行版本时导入 |
| `SEED_REQUIRED` | `true` | 种子缺失/非法时是否拒绝启动 |
| `DEMO_DIR` / `ENABLE_DEMO` | 空 / `false` | 演示包目录与开关 |
| `MAX_IMPORT_BYTES` | 4 GiB | 上传与解包总大小上限 |
| `MAX_TAR_ENTRIES` | 20000 | tar 条目上限 |
| `MAX_METADATA_BYTES` | 4 MiB | 索引/manifest/config 单文件上限 |
| `VERIFY_STORE_BLOBS` | `true` | 从存储卷取 blob 时是否重算摘要 |
| `VERIFY_DIFF_IDS` | `true` | 是否校验未压缩层与 config `diff_ids` |

## 样例

`samples/` 中的镜像真实可运行（静态 busybox 作为层内容），`docker load` 后可 `docker run`。

| 文件 | 用途 |
|---|---|
| `image-v1.tar` | 初始镜像（完整，amd64+arm64），启动种子 |
| `image-v2.tar` | 完整 v2 镜像 |
| `update-v2.tar` | 增量更新包：v2 索引 + 新 blob（基础层省略，约 20 KB） |
| `broken-missing-layer.tar` | 缺 arm64 层 → 422 `blob_missing` |
| `broken-bad-digest.tar` | 层内容损坏 → 422 `blob_mismatch` |
| `broken-mismatch-size.tar` | 描述符大小不符 → 422 `blob_mismatch` |
| `demo.json` | 各包文件名、摘要与预期结果 |

坏包基于从未导入过的 v3 版本构建，确保其缺失/损坏的层在演示时确实不在存储卷上。
重新生成：`scripts/build-samples.sh`（首次需要 busybox 的 deb，之后走 `tools/.cache`）。

## Swagger 演示步骤

1. 打开 `http://127.0.0.1:8080/`。
2. `GET /api/v1/status` — 记下 `active`（即 `expected_current_digest`）。
3. `GET /api/v1/demo/samples`，下载 `update-v2.tar`。
4. `POST /api/v1/images/import` — 选 `update-v2.tar`，`expected_current_digest` 填上一步的
   `active`，`enable=true`。响应中 `blobs` 会显示基础层来自 `store`。
5. 用 `broken-*.tar` 重复，观察 422 与列出的摘要。
6. 再传一次 `update-v2.tar` 但填旧摘要（v1），观察 409。
7. `GET /api/v1/images/current/layout.tar` 下载现行版本；`/api/v1/images/{digest}/layout.tar`
   可下载任一历史版本。

也可直接运行 `scripts/demo.py` 自动执行以上全部步骤。

## 测试

```sh
.venv/bin/python -m pytest -q
```

覆盖：增量补齐、缺层/摘要不符/大小不符（逐项断言摘要）、并发导入（一 201 一 409）、
下载中途换版（真实 uvicorn 流式读取）、重启后取现行版、单实例锁、路径穿越与坏 tar。
