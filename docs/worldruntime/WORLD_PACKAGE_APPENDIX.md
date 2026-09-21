# 世界包附录（实现级：文件形态 / 标识编码 / 字段表 / 限额）

> 上游：[`WORLD_SETTING_SPEC.md`](WORLD_SETTING_SPEC.md) §十「残余」第 1 条 —— **实现级 JSON schema、稳定标识编码、容器布局**。
> 本文只写代码里**已经存在**的行为：每条给 `文件:行` 或 `文件::函数名`；代码里没有的一律标「未定义 / 未强制」，不按设计意图补写。
> 基线：`isekai_core/world/{package,portable,validate,instances}.py`、`isekai_core/{config,version,session,store,channel}.py`。
> 引用约定：行号写作时定格（近期提交漂移大处改按 `文件::函数名`）。
> 机器对拍：`scripts/_audit2_pkg_doc.py`（比对本文的键集 / 枚举 / 数值，不一致即 FAIL 并打印差异）。

## ① 文件形态与落盘位置

| 事项 | 事实 | 代码出处 |
|---|---|---|
| 包形态 | 单文件 JSON，UTF-8，`ensure_ascii=False` + `indent=2`（可手编、可 diff） | package.py:1-5 · package.py::save_package |
| 写盘 | 先写同目录 `.tmp` → `flush` + `fsync` → `os.replace` 原子替换；失败不覆盖原文件 | package.py::save_package |
| 读取闸 | 先 `stat().st_size`，超 `MAX_PACKAGE_BYTES` 直接拒（不把内容读进内存）；再 `json.loads` | package.py:140,143-163 |
| 顶层要求 | 必须是 JSON 对象，且必须含 `meta` 段（对象） | package.py::load_package |
| 原始名称 | 首次确认时把 `meta.original_name` 落定；之后改显示名不动它 | package.py:127-135 |
| 创作目录 | `<root>/packages`（世界包 / 角色卡默认落盘位置）、`<root>/exports` | config.py::Paths |
| 运行数据 | `data/isekai.db`（单库）、`data/core.lock`（单写入者锁）、`logs/`、`config/config.yaml` | config.py::Paths |
| root 解析 | `ISEKAI_ROOT` 环境变量 > 显式参数 > 仓库根 | config.py::resolve_root |

## ② 整库导出容器（`world/portable.py`）

顶层四段：`container`（清单）/ `setting`（锁定设定快照）/ `runtime`（运行部分）/ `integrity`（指纹）。

| `container` 字段 | 语义 | 代码出处 |
|---|---|---|
| `format` | 固定 `isekai.instance`（`CONTAINER_FORMAT`） | portable.py::build_container · version.py:19 |
| `container_version` | `CONTAINER_VERSION = "1.0"`；导入时**主版本**必须与本端一致 | portable.py::check_compatibility · version.py:20 |
| `app_version` | `APP_VERSION = "0.1.0"`（写件方的应用版本，不参与兼容判定） | version.py:9 |
| `data_format` / `rules_version` | 该实例的规则版本锚（导入后决定转换路径） | instances.py · converters |
| `exported_at` | 现实时间戳 | portable.py::build_container |
| `name` / `original_name` / `package_id` | 实例显示名、原始名、世界包稳定标识 | portable.py::build_container |
| `moment` | 导出时的世界时刻 | portable.py::build_container |
| `capabilities` | 本端能力表（`CAPABILITIES`，4 项） | version.py:23-28 |
| `counts` | `sessions` / `messages` / `timelines` / `commits` 四类计数 | portable.py::build_container |

- 指纹：`integrity = {algorithm: "sha256", digest: sha256(setting + runtime)}`；导入先查兼容、再验指纹，最后才落库（portable.py::_digest/verify_integrity/import_instance）。
- 上限：**`MAX_CONTAINER_BYTES = 256 MiB`**（容器件自带更大的闸，与单包 1 MiB 分开）；`read_container` 走同一把 `read_json_file`（portable.py:151-163）。
- 不导出：待生效倍率命令、投递回执、通道绑定（portable.py::build_container 注释，§2.6 / §2.3.6）。

## ③ 稳定标识编码

**代码强制的两条**（其余都是约定）：

- 条目必须给 `id`，且**同一集合内唯一**——`validate.py::_check_unique`（缺 `id` 或重复 → 报错；重复会打印冲突项）。
- 长度受加载限额约束：单条字符串 ≤ `MAX_STRING`（4000）。**没有字符集约束**（正则白名单不存在），
  也没有长度上限——`package.py:19` 的 `NAME_MAX_LEN = 64` 定义后从未被引用（已删），名称长度目前无上限。

**生成器前缀表**（机器生成的主键，前缀固定、后缀 hex）：

| 前缀 | 对象 | 代码出处 |
|---|---|---|
| `wp-` | 世界包 | package.py:26-27 |
| `in-` / `cm-` / `tl-` | 实例 / 提交 / 时间线 | instances.py:51-52,55-56,97 |
| `m-` | 消息 | session.py:190,544 |
| `ci-` / `se-` | 通道 / 会话 | store.py:1590,1672 |
| `cr-` / `bt-` / `bs-` / `mg-` | 核心令牌 / 引导令牌 / 通道引导令牌 / 管理面令牌 | store.py:993,997 · channel.py:99-100 |

**创作期标识的命名约定**（模板与示例口径，校验器不校前缀）：
`ax`（公理）、`cf`（实情条目）、`nv`（说法条目）、`src`（传本）、`hs`（史料）、`rc`（种族）、`en`（登记实体）、
`inst` / `off`（制度 / 职位）、`cus`（惯例）、`ef` / `et`（事件族 / 模板）、`lf` / `rl`（生活线 / 角色模板）、
`cm`（联络机制）、`seg`（时段）。

**名称 ≠ 主键**：名称比较规则 `normalize_name` = 去首尾空白 + NFKC + `casefold`（package.py:30-32）；
冲突取最小可用序号 `_2`、`_3`…（package.py::unique_name，空名抛 `PackageError`）；
转换器的格式标识用**同一套**规范化规则（`world/converters.py::normalize_format`）。

## ④ 顶层键与必需项（实现级 schema）

顶层 15 键（`template_package` 给的骨架即代码口径，可直接枚举）：

`meta` · `calendar` · `world` · `environment` · `sources` · `canon` · `narratives` · `entities` · `races` ·
`historiography` · `events` · `life` · `roles` · `comms` · `initial_state`

| 顶层键 | 模板给的子键（骨架口径） | 校验器**强制**的项（核实过的） |
|---|---|---|
| `meta` | `schema` `package_id` `original_name` `display_name` `description` `density` | `schema` 必须 == `1.0`；`package_id` / `original_name` 非空；`density` ∈ DENSITIES；`requires`（可选）必须 ⊆ CAPABILITIES |
| `calendar` | `era` `day_seconds` `months[]` `week{days}` `segments[]` `initial_moment` | `era` 非空；`day_seconds` 正整数；时段 `id/name/start/end` |
| `world` | `axioms[]` `geography` `society` `lexicon{terms[]}` `institutions[]` `customs[]` | 制度：`mandate` / `scope` / `succession` / `validity` 必填，职位 `id/name/holder`、`vacancy_policy{continues,suspended}` 显式；惯例：`applies_to` / `practice` / `basis` / `variation` 必填，`practice` ∈ `forms` |
| `environment` | `types[]` | 效果 `environment_state` 只能引用已声明类型与取值域 |
| `sources` | `id` `name` `kind` `reach` | 说法 `source_id` 必须指向已声明传本 |
| `canon` | `id` `statement` `tags[]` | 史料条目 `entries` / 事件前置条件只能引用 canon / narratives 标识 |
| `narratives` | `id` `text` `source_id` `canon_ref` `obtain[]` `confidence` | `confidence` **未定义枚举、校验器不校**（模板写 `believed`） |
| `entities` | `id` `kind` `name` `race_id` `born` `died` | `kind` ∈ ENTITY_KINDS；**名册非空**（至少一人）；制度在任者必须在这里 |
| `races` | `id` `name` `lifespan` | `lifespan` 两种形态之一：`{mode: long\|unbounded}` 或 `{min_years,max_years}`（正整数年、min ≤ max），**混写即拒** |
| `historiography` | `id` `title` `contributors[]` `written_at` `compiled_at` `coverage{from,to}` `genre` `stance` `entries[]` | 贡献者须给 `role` 与 `period`；`entries` 非空 |
| `events` | `families[]{id,name,templates[]}` `density` | `density` ∈ DENSITY_TARGETS（**体裁必填**）；模板效果 `target` 必须在册、每条效果给 `expiry` ∈ EXPIRY_KINDS |
| `life` | `id` `name` `sleep` `windows[]{start,end,activity}` | 生活线须显式声明是否睡眠；角色模板 `life_template` 必须指向这里 |
| `roles` | `id` `name` `description` `life_template` `channels[]` | `channels` 闭合到 `sources` |
| `comms` | `mechanisms[]{id,name,limits}` | — |
| `initial_state` | `events[]` `rumors[]` `mysteries[]` | 引用必须闭合到 canon / narratives |

> 未列进上表的子键 = 「模板给了但校验器不强制」，或本附录尚未核实；**不按设计意图补写**。

## ⑤ 枚举与取值域

| 枚举 | 取值 | 代码出处 |
|---|---|---|
| `meta.density` | `sparse` / `normal` / `rich` | package.py:18 |
| `events.density`（体裁） | `稀疏` / `常规` / `丰盛` → 每日目标 `0–1` / `1–3` / `2–5` | validate.py:638 |
| `entities[].kind` | `person` / `org` / `place` / `item` | validate.py:16 |
| `races[].lifespan.mode` | `long` / `unbounded` | validate.py:15 |
| 效果 `expiry` | `with_cause` / `until_cleared` / `natural_recovery` | validate.py:641 |
| 效果 `kind`（8 种） | `source_delay` 渠道受阻 · `route_blocked` 通行受阻 · `activity_constraint` 活动受限 · `public_notice` 公开通告 · `rumor_spread` 风闻流传 · `institution_state` 制度状态 · `custom_state` 惯例现行做法 · `environment_state` 环境状态 | validate.py:645-654 |
| `meta.requires` / 容器 `capabilities` | `world.package.v1` / `cards.v1` / `instance.v1` / `message.delivery.v1` | version.py:23-28 |
| 历法时段键 | `id` / `name` / `start` / `end` | validate.py:14 |

## ⑥ 上限与加载限额

| 常量 | 值 | 作用范围 | 代码出处 |
|---|---|---|---|
| `MAX_PACKAGE_BYTES` | 1 MiB | 单包 / 角色卡 / 一般导入件的读取闸 | package.py:140 |
| `MAX_CONTAINER_BYTES` | 256 MiB | 整库导出容器 | portable.py:151 |
| `MAX_DEPTH` | 12 | 嵌套深度（超限直接返回，不再继续） | validate.py:52 |
| `MAX_NODES` | 20000 | 节点总数 | validate.py:53 |
| `MAX_STRING` | 4000 | 单条字符串长度 | validate.py:54 |
| `MAX_COLLECTION` | 500 | 单个集合条目数 | validate.py:55 |
| `PACKAGE_SCHEMA_VERSION` | `1.0` | `meta.schema` 必须相等 | package.py:17 |

> 回填相关的实现参数（分卷 / 批量 / 折半阈值 / 要点人物 / 名册体量）属运行期，见 `WORLD_SETTING_SPEC.md` §十 与
> `isekai_core/runtime/events.py`，不在本文范围。
