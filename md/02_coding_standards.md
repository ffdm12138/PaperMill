# 编码规范

## 单源工具义务

以下能力一律复用 `src/utils/`，禁止在业务模块内联重复实现：

| 能力 | 权威实现 | 备注 |
| --- | --- | --- |
| JSON 读取 | `src/utils/jsonio.py` | 模块内薄适配器（`_read_json` 委托）允许 |
| 原子写 | `src/utils/atomic_io.py` | 关键状态文件必须原子写 + fsync |
| 时间戳 | `src/utils/timestamps.py` | 见下节 |
| 标识符 | `src/utils/identifiers.py` | DOI 归一、16 位纸号规则 |
| 文件指纹 | `src/utils/file_fingerprint.py` | sha256/md5/file_size |
| canonical JSON 哈希 | `src/utils/canonical_json.py` | 仅 A 族编码（compact 分隔符） |
| 进程存活 | `src/utils/process.py` | `is_pid_alive` |
| 命名/路径 | `src/utils/naming.py`、`src/utils/path_utils.py` | paper_name 校验、repo 相对路径 |
| 打包/仓库卫生策略 | `src/utils/repository_hygiene.py` | packer 与验收共用 |

**持久化哈希红线**：已存在于磁盘上的哈希（notebook 16-hex 身份、page journal
checksum、lane 请求签名等）使用与 `canonical_json` **不同**的字节编码，
相关站点带有 "do NOT swap" 注释——任何"顺手统一"都会腐蚀已存数据，禁止。

## 时间戳

- 持久化时间戳一律 timezone-aware，一律经 `src/utils/timestamps.py`：
  `now_iso()`（本地带偏移、秒精度，ingest/ledger/manifest 族）、
  `utc_now_iso()`（UTC 微秒精度，discovery journal 族）、
  `utc_now_iso_z()`（Z 后缀，catalog registry 族）。
- 例外（有意保留）：standalone 设计的脚本（pack_repo/test_runtime_workspace/
  cleanup_test_caches）不得 import src，因此各自内联；writer 族历史 naive
  时间戳的 tz 化是行为变更提案，见 [06_refactor_roadmap.md](06_refactor_roadmap.md)。

## 锁序

以 CLAUDE.md 的 Lock order 一节为准（此处不复述清单）。要点：
按序取所需子集；多把 paper 锁按纸号升序取、逆序放；持高阶锁时禁取低阶锁；
持 ledger 锁期间禁止 PDF/目录树拷贝。锁路径与超时策略随权威模块走，
不在调用点即兴发明。

## 错误处理

- 禁 bare `except:`；禁"宽且静默"的 `except Exception: pass`——捕获必须
  窄化到可解释的异常族（OSError/SubprocessError/ValueError…），或绑定后记日志。
- 缺省 fail-closed：输入不可信、状态不完整、路径未通过校验时报错停机，
  而不是猜测继续。
- 查询型探测函数（如 `is_pid_alive`）允许宽捕获并返回保守值，属于既定语义。

## 日志与报告

- loguru 单 sink，入口经 `scripts/_bootstrap` 配置；库模块只 `from loguru
  import logger`，绝不配置 sink；src/ 内禁止 `print()`。
- 持久化报告递归剥离 URL 查询串（含异常文本内嵌 URL）；日志与报告不得含凭证。

## 路径与命名

- 一律 `pathlib`；os.path 仅限符号链接安全检测等既有豁免点（带注释）。
- `.paper.number` 剥离完整后缀，禁 `Path.stem`；纸号只经 `PaperNumberLedger`，
  禁目录扫描推导。
- 下划线私名不得作为跨模块接口：要跨模块就公名化（历史案例：
  `local_evidence`、`fsync_dir`、`is_pid_alive`）。

## 网络访问

OpenAlex/Crossref 的一切 HTTP（发现分页、标题解析、metadata 解析、DOI 验证、
taxonomy 拉取）必须经统一 `ProviderClient`（限速/重试/熔断/预算/遥测）；
业务模块禁止直接调用 `requests.*`/`httpx.*`。PDF 传输走 `src/fetch/pdf_transport`
的 direct-first + 代理回退语义。
