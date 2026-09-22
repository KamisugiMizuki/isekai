"""SQLite 持久化（单一写入者）。

- WAL + 单连接 + 进程内锁；核心只有一个写库进程（DESKTOP_SPEC §2.1）。
- message 表同时承担入站去重与历史真值；delivery 表按批次记录投递状态。
- 去重作废记录（voided）不随世界历史回滚倒退，也不随实例导出（CHANNEL_PLUGIN_SPEC §2.2）。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import struct
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .log import get_logger
from .version import DATA_FORMAT_VERSION

log = get_logger("isekai.store")
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS channel_instance(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  version TEXT,
  protocol TEXT NOT NULL DEFAULT '1.0',
  capabilities TEXT NOT NULL DEFAULT '{}',
  credential_hash TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'active',
  created_at REAL NOT NULL,
  last_seen_at REAL
);

CREATE TABLE IF NOT EXISTS session(
  id TEXT PRIMARY KEY,
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  character_id TEXT NOT NULL,
  created_at REAL NOT NULL,
  UNIQUE(instance_id, timeline_id, character_id)
);

CREATE TABLE IF NOT EXISTS thread(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel_id TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  binding_version INTEGER NOT NULL DEFAULT 1,
  binding_token TEXT NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(channel_id, thread_id)
);

CREATE TABLE IF NOT EXISTS message(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  role TEXT NOT NULL,                 -- user | character | notice
  channel_id TEXT,
  thread_id TEXT,
  env_id TEXT,
  binding_version INTEGER,
  binding_token TEXT,                 -- 固化时捕获的目标令牌，不随重绑换代
  text TEXT,
  parts TEXT,
  message_id TEXT,                    -- 出站：稳定标识
  reply_message_id TEXT,              -- 入站：指向本轮的固化回复
  reply_to TEXT,
  batch_id TEXT,
  batch_index INTEGER,
  batch_count INTEGER,
  covers TEXT DEFAULT '[]',
  wait_until REAL NOT NULL DEFAULT 0, -- 入站：睡眠期合并批的现实截止点（一次确定，不因后续输入重置）
  model_fingerprint TEXT NOT NULL DEFAULT '',  -- 产出该回复的模型标识（换模型后旧行仍看得出边界）
  attachments TEXT NOT NULL DEFAULT '[]',      -- 入站：附件 JSON 列表 [{name,media_type,size,data(base64)}]（§七 附件项）
  state TEXT NOT NULL,
  error_code TEXT,
  created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_message_inbound
  ON message(channel_id, thread_id, env_id) WHERE env_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_message_out ON message(message_id) WHERE message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_message_session ON message(session_id, seq);

CREATE TABLE IF NOT EXISTS delivery(
  msg_seq INTEGER NOT NULL,
  batch_index INTEGER NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending',   -- pending|sent|accepted|failed|unknown
  updated_at REAL,
  PRIMARY KEY(msg_seq, batch_index)
);

CREATE TABLE IF NOT EXISTS voided(
  channel_id TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  env_id TEXT NOT NULL,
  reason TEXT,
  at REAL NOT NULL,
  PRIMARY KEY(channel_id, thread_id, env_id)
);

-- ---------- 世界设定层（阶段 1）：实例、锁定设定、时间线与提交 ----------

CREATE TABLE IF NOT EXISTS instance(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  original_name TEXT NOT NULL,
  package_id TEXT NOT NULL,
  data_format TEXT NOT NULL,
  rules_version TEXT NOT NULL,
  app_version TEXT NOT NULL,
  seed TEXT NOT NULL,
  moment INTEGER NOT NULL,           -- 初始世界时刻（世界秒）
  setting TEXT NOT NULL,             -- 锁定设定快照 JSON：{world_package, original_name, cards}
  imported INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS timeline(
  id TEXT PRIMARY KEY,
  instance_id TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'frozen',   -- frozen|active|archived
  source_commit TEXT,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS commit_auto_state(
  timeline_id TEXT PRIMARY KEY,
  instance_id TEXT NOT NULL,
  last_commit_at REAL NOT NULL DEFAULT 0,
  last_commit_moment INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS disclosure(
  id TEXT NOT NULL,
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  from_character TEXT NOT NULL,
  to_character TEXT NOT NULL,
  scope TEXT NOT NULL,                 -- 明确的消息 / 片段引用（JSON）
  granted_world INTEGER NOT NULL,      -- 生效水位：随时间线版本化（§7.1）
  granted_real REAL NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'granted',
  PRIMARY KEY(instance_id, timeline_id, id)
);

CREATE TABLE IF NOT EXISTS event_draft(
  id TEXT PRIMARY KEY,
  instance_id TEXT NOT NULL,
  source_timeline TEXT NOT NULL,
  source_commit TEXT,
  payload TEXT NOT NULL,               -- 规范化后的草案（用户意图 + 已确认部分）
  state TEXT NOT NULL DEFAULT 'draft', -- draft|confirmed|rejected
  timeline_id TEXT NOT NULL DEFAULT '',-- 确认后落成的新线
  created_world INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_event(
  id TEXT PRIMARY KEY,
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  at_world INTEGER NOT NULL,
  payload TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending',  -- pending|applied|cancelled|skipped
  note TEXT NOT NULL DEFAULT '',
  created_world INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS commit_snapshot(
  commit_id TEXT PRIMARY KEY,
  instance_id TEXT NOT NULL,
  payload TEXT NOT NULL,
  size INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS commit_log(
  id TEXT PRIMARY KEY,
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  kind TEXT NOT NULL,                     -- initial|manual|auto|import
  moment INTEGER NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_timeline_instance ON timeline(instance_id);
CREATE INDEX IF NOT EXISTS ix_commit_instance ON commit_log(instance_id, timeline_id, created_at);
CREATE INDEX IF NOT EXISTS ix_session_instance ON session(instance_id);
-- 名称唯一：规范化比较在应用层（NFKC + 大小写折叠），这里兜底同名直插（§7.4）
CREATE UNIQUE INDEX IF NOT EXISTS ux_instance_name ON instance(name);

-- ---------- 世界运行层（阶段 2）：时钟、倍率、水位、角色状态 ----------

CREATE TABLE IF NOT EXISTS timeline_clock(
  timeline_id TEXT PRIMARY KEY,
  base_real REAL NOT NULL,               -- 当前倍率段的起点（现实秒，UTC）
  base_world INTEGER NOT NULL,           -- 当前倍率段的起点（世界秒）
  rate INTEGER NOT NULL DEFAULT 1,       -- 当前倍率（正整数；冻结用时间线状态表达）
  high_water_real REAL NOT NULL DEFAULT 0,
  anchor_real REAL NOT NULL,             -- 激活时的现实锚点
  processed_world INTEGER NOT NULL,      -- 已处理世界时刻（共同水位）
  generation INTEGER NOT NULL DEFAULT 1, -- 运行世代：回滚 / 删除使旧任务失效
  catching_up INTEGER NOT NULL DEFAULT 0,-- 目标时刻领先于处理水位（§2.6）
  limited INTEGER NOT NULL DEFAULT 0     -- 追赶受限：滞后超过预算，停止扩大目标（§2.6）
);

-- ---------- 事件引擎（阶段 3）：事件 / 说法 / 获知 / 效果状态 ----------

-- 环境事实状态（WORLD_RUNTIME_SPEC §11.2）：可选状态域，未声明的类型没有真值
CREATE TABLE IF NOT EXISTS environment_state(
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  type_id TEXT NOT NULL,
  value TEXT NOT NULL,
  unit TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',       -- initial | natural:<名> | event:<事件标识>
  from_world INTEGER NOT NULL DEFAULT 0,
  expiry TEXT NOT NULL DEFAULT 'until_cleared',
  updated_world INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(instance_id, timeline_id, type_id)
);

-- 制度状态（阶段 6）：职位与在任者；空缺期间的事务规则随行（列表字段存 JSON）
CREATE TABLE IF NOT EXISTS institution_state(
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  office_id TEXT NOT NULL,
  institution_id TEXT NOT NULL DEFAULT '',
  institution_name TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL DEFAULT '',
  holder TEXT NOT NULL DEFAULT '',        -- 登记实体标识；空串 = 空缺
  continues_json TEXT NOT NULL DEFAULT '[]',
  suspended_json TEXT NOT NULL DEFAULT '[]',
  source TEXT NOT NULL DEFAULT '',        -- initial | 事件标识
  from_world INTEGER NOT NULL DEFAULT 0,
  updated_world INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(instance_id, timeline_id, office_id)
);

-- 文化惯例状态（阶段 6）：当前做法必须落在声明的允许范围内
CREATE TABLE IF NOT EXISTS custom_state(
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  custom_id TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  applies_to TEXT NOT NULL DEFAULT '',
  form TEXT NOT NULL DEFAULT '',
  forms_json TEXT NOT NULL DEFAULT '[]',
  basis TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  from_world INTEGER NOT NULL DEFAULT 0,
  updated_world INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(instance_id, timeline_id, custom_id)
);

-- 会话通告（SESSION_CORE_SPEC §5.7）：归档说明 / 最后联络一类，一次性
CREATE TABLE IF NOT EXISTS session_notice(
  session_id TEXT NOT NULL,
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  message_id TEXT NOT NULL DEFAULT '',
  created_real REAL NOT NULL DEFAULT 0,
  PRIMARY KEY(session_id, kind)
);

-- 初见（SESSION_CORE_SPEC §5.6）：每个会话只有一次独立开场；不占世界源主动配额
CREATE TABLE IF NOT EXISTS first_contact(
  session_id TEXT PRIMARY KEY,
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  character_id TEXT NOT NULL,
  message_id TEXT NOT NULL DEFAULT '',
  at_world INTEGER NOT NULL DEFAULT 0,
  created_real REAL NOT NULL DEFAULT 0
);

-- 主动发言账本（SESSION_CORE_SPEC §5.2）：配额按最终消息固化计数；同一素材不重复消费
CREATE TABLE IF NOT EXISTS proactive_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  character_id TEXT NOT NULL,
  world_day INTEGER NOT NULL,
  material_ref TEXT NOT NULL DEFAULT '',
  message_id TEXT NOT NULL DEFAULT '',
  created_world INTEGER NOT NULL DEFAULT 0,
  created_real REAL NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'fixed'
);
CREATE INDEX IF NOT EXISTS ix_proactive_day
  ON proactive_log(instance_id, timeline_id, character_id, world_day);

-- 外部调用账本（WORLD_RUNTIME_SPEC §2.8）：只记次数与量级，不记正文 / prompt / 密钥
CREATE TABLE IF NOT EXISTS call_ledger(
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  task TEXT NOT NULL,
  bucket INTEGER NOT NULL,               -- 现实日窗口（UTC 日序）
  calls INTEGER NOT NULL DEFAULT 0,
  tokens INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(instance_id, timeline_id, task, bucket)
);

CREATE TABLE IF NOT EXISTS memory(
  id TEXT NOT NULL,
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  character_id TEXT NOT NULL,
  text TEXT NOT NULL,
  kind TEXT NOT NULL,
  sources TEXT NOT NULL DEFAULT '[]',
  happened_world INTEGER,
  learned_world INTEGER NOT NULL,
  recorded_world INTEGER NOT NULL,
  semantic_watermark INTEGER NOT NULL,
  strength REAL NOT NULL DEFAULT 0.6,
  confidence REAL NOT NULL DEFAULT 0.7,
  state TEXT NOT NULL DEFAULT 'active',
  version INTEGER NOT NULL DEFAULT 1,
  supersedes TEXT,
  superseded_by TEXT,
  source_key TEXT,
  decay_world INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(instance_id, timeline_id, id)
);

CREATE TABLE IF NOT EXISTS memory_task(
  id TEXT NOT NULL,
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  character_id TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  source_ref TEXT NOT NULL,
  source_world INTEGER NOT NULL,
  created_world INTEGER NOT NULL,
  text TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  note TEXT NOT NULL DEFAULT '',
  UNIQUE(instance_id, timeline_id, character_id, source_kind, source_ref),
  PRIMARY KEY(instance_id, timeline_id, id)
);

CREATE TABLE IF NOT EXISTS memory_citation(
  turn_id TEXT NOT NULL,
  memory_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  character_id TEXT NOT NULL,
  world_seconds INTEGER NOT NULL,
  created_at REAL NOT NULL,
  PRIMARY KEY(timeline_id, turn_id, memory_id)
);

CREATE TABLE IF NOT EXISTS memory_embedding(
  memory_id TEXT NOT NULL,
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL,
  dim INTEGER NOT NULL,
  vector BLOB NOT NULL,
  source_version INTEGER NOT NULL,
  created_at REAL NOT NULL,
  PRIMARY KEY(instance_id, timeline_id, memory_id)
);

CREATE TABLE IF NOT EXISTS budget_reserve(
  id TEXT PRIMARY KEY,
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  task TEXT NOT NULL,
  bucket INTEGER NOT NULL,               -- 现实日窗口
  priority INTEGER NOT NULL,
  tokens_est INTEGER NOT NULL DEFAULT 0,
  tokens_actual INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'held',    -- held / settled / released
  outcome_kind TEXT NOT NULL DEFAULT '', -- ok / error / timeout / cancelled
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS budget_policy(
  instance_id TEXT PRIMARY KEY,
  paused_tasks TEXT NOT NULL DEFAULT '[]',
  instance_tokens_per_day INTEGER,
  timeline_tokens_per_day INTEGER,
  task_tokens_per_day INTEGER,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS event(
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  id TEXT NOT NULL,                      -- 稳定事件标识（种子 + 规则 + 历法日 + 槽）
  world_seconds INTEGER NOT NULL,
  seq INTEGER NOT NULL DEFAULT 0,        -- 同刻顺序（固定规则，不随消费者执行先后变）
  kind TEXT NOT NULL,                    -- world | character
  family TEXT NOT NULL DEFAULT '',
  template TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL,                  -- engine | backfill | life | character_action
  summary TEXT NOT NULL,                 -- 事实骨架（确定性；写后不可原地改写）
  detail TEXT NOT NULL DEFAULT '',       -- 实情文本（模板或 LLM 表述，固化后不改）
  text_source TEXT NOT NULL DEFAULT 'template',
  effects TEXT NOT NULL DEFAULT '[]',
  share_value INTEGER NOT NULL DEFAULT 0,
  importance REAL NOT NULL DEFAULT 0.0,
  created_real REAL NOT NULL DEFAULT 0.0,
  PRIMARY KEY(instance_id, timeline_id, id)
);
CREATE INDEX IF NOT EXISTS ix_event_window ON event(instance_id, timeline_id, world_seconds);

CREATE TABLE IF NOT EXISTS claim(
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  source_id TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL,
  audience TEXT NOT NULL DEFAULT '公开',
  earliest_world INTEGER NOT NULL,       -- 最早可传播时刻
  credibility TEXT NOT NULL DEFAULT 'recorded',
  derived_from TEXT,                     -- 派生记录：展开自哪条说法（不原地改写原条目）
  PRIMARY KEY(instance_id, timeline_id, id)
);
CREATE INDEX IF NOT EXISTS ix_claim_event ON claim(instance_id, timeline_id, event_id);

-- 插件登记（CHANNEL_PLUGIN_SPEC §3.1/§3.2）：只记清单与启停状态；运行进程不跨核心重启存活
CREATE TABLE IF NOT EXISTS plugin(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  version TEXT NOT NULL DEFAULT '',
  path TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'registered',   -- registered|starting|running|stopped|failed|invalid
  note TEXT NOT NULL DEFAULT '',
  updated_at REAL NOT NULL DEFAULT 0
);

-- 管理面通知（CHANNEL_PLUGIN_SPEC §2.5 末条）：只作**已固化主动消息**的入口，不存第二份历史。
-- 固定引用 (message_id, 原会话版本)；回滚 / 删除 / 归档 / 重绑后解析只返回管理错误，不改投、不激活冻结线。
CREATE TABLE IF NOT EXISTS notice(
  id TEXT PRIMARY KEY,
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  UNIQUE(instance_id, timeline_id, session_id, message_id)
);

-- 短期反应（WORLD_RUNTIME_SPEC §11.1）：有来源的素材集合，不是全局情绪数值
CREATE TABLE IF NOT EXISTS reaction(
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  character_id TEXT NOT NULL,
  id TEXT NOT NULL,                        -- 由（来源类型, 来源标识）派生：同一来源只建一条
  source_kind TEXT NOT NULL,               -- event_effect | experience | dialog
  source_ref TEXT NOT NULL,
  direction INTEGER NOT NULL DEFAULT 1,
  intensity TEXT NOT NULL DEFAULT 'mid',   -- low | mid | high（区间表述）
  stage TEXT NOT NULL DEFAULT 'candidate', -- candidate|adopted|active|fading|paused|expired|long_term
  tendency TEXT NOT NULL DEFAULT '',
  basis TEXT NOT NULL DEFAULT '',
  started_world INTEGER NOT NULL,
  expiry_condition TEXT NOT NULL DEFAULT '',
  updated_world INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(instance_id, timeline_id, character_id, id)
);
CREATE INDEX IF NOT EXISTS ix_reaction_stage ON reaction(instance_id, timeline_id, stage, started_world);

-- 叙事中介层（NARRATIVE_LAYER_SPEC §7）：派生记录——已固化消息引用的叙事单元 / 暂缓标记 / 审计结果。
-- 候选与排序是可重建的中间产物，不落库；落库的只有「她讲过什么、没讲出口什么」这件事。
CREATE TABLE IF NOT EXISTS narrative_unit(
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  character_id TEXT NOT NULL,
  id TEXT NOT NULL,                        -- 由主材引用派生：同一材料复用同一个单元
  primary_ref TEXT NOT NULL,
  refs TEXT NOT NULL DEFAULT '[]',         -- 单元内引用的材料（消费记账按这些引用去重）
  entry TEXT NOT NULL DEFAULT '',          -- experience | knowledge
  relation TEXT NOT NULL DEFAULT '',       -- 补充 | 连续 | 并列
  topic TEXT NOT NULL DEFAULT '',
  stage TEXT NOT NULL DEFAULT 'spoken',    -- spoken 已固化 | deferred 没讲出口（暂缓）
  message_id TEXT NOT NULL DEFAULT '',
  world_day INTEGER NOT NULL DEFAULT 0,
  audit TEXT NOT NULL DEFAULT '[]',        -- 后验检查结果（空列表 = 本次没有发现越界）
  note TEXT NOT NULL DEFAULT '',
  created_world INTEGER NOT NULL,
  updated_world INTEGER NOT NULL,
  PRIMARY KEY(instance_id, timeline_id, character_id, id)
);
CREATE INDEX IF NOT EXISTS ix_narrative_unit_stage
  ON narrative_unit(instance_id, timeline_id, character_id, stage, world_day);

-- 惰性展开的覆盖状态（EVENT_ENGINE_SPEC §3.4 / 附录B#10）：
-- 没有行 = 尚未生成；state=absent 只表示「这条记载没写下」，不是「历史被删改」的证据。
CREATE TABLE IF NOT EXISTS claim_coverage(
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  claim_id TEXT NOT NULL,
  state TEXT NOT NULL,                    -- done 已展开 | absent 已确认缺载
  derived_id TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  updated_world INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(instance_id, timeline_id, claim_id)
);

CREATE TABLE IF NOT EXISTS knowledge(
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  character_id TEXT NOT NULL,
  id TEXT NOT NULL,
  world_seconds INTEGER NOT NULL,        -- 获知时刻
  kind TEXT NOT NULL,                    -- claim | observation | experience
  target TEXT NOT NULL,                  -- 说法标识或事件标识
  source TEXT NOT NULL DEFAULT '',       -- 渠道 / 亲历
  stance TEXT NOT NULL DEFAULT 'recorded',
  text TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(instance_id, timeline_id, character_id, id)
);
CREATE INDEX IF NOT EXISTS ix_knowledge_character ON knowledge(instance_id, timeline_id, character_id, world_seconds);

-- 角色打算（WORLD_RUNTIME_SPEC §11.3）：角色状态，不是世界事实
CREATE TABLE IF NOT EXISTS intent(
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  character_id TEXT NOT NULL,
  id TEXT NOT NULL,
  object TEXT NOT NULL,
  basis TEXT NOT NULL DEFAULT '',
  strength REAL NOT NULL DEFAULT 0.5,
  window_from INTEGER NOT NULL DEFAULT 0,
  window_to INTEGER NOT NULL DEFAULT 0,
  preconditions TEXT NOT NULL DEFAULT '[]',
  effect TEXT NOT NULL DEFAULT '{}',
  stage TEXT NOT NULL DEFAULT 'adopted',   -- candidate|adopted|waiting|done|deferred|abandoned
  note TEXT NOT NULL DEFAULT '',
  source_world INTEGER NOT NULL DEFAULT 0,
  updated_world INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(instance_id, timeline_id, character_id, id)
);
CREATE INDEX IF NOT EXISTS ix_intent_character ON intent(instance_id, timeline_id, character_id, stage);

CREATE TABLE IF NOT EXISTS effect_state(
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  target TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL,
  family TEXT NOT NULL DEFAULT '',       -- 事件族：自然恢复按同族后续事件判定（§六）
  value TEXT,                            -- 环境类效果的取值（取值域内）
  from_world INTEGER NOT NULL,
  expiry TEXT NOT NULL DEFAULT 'until_cleared',
  recovery TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1,
  cleared_at INTEGER,
  priority INTEGER NOT NULL DEFAULT 0,   -- 同刻优先档位（固定规则，见 runtime/events.EFFECT_PRIORITY）
  seq INTEGER NOT NULL DEFAULT 0,        -- 同刻施加顺序（优先档位 + 稳定标识，不随调用方迭代顺序变）
  PRIMARY KEY(instance_id, timeline_id, id)
);
CREATE INDEX IF NOT EXISTS ix_effect_active ON effect_state(instance_id, timeline_id, active, from_world);

-- 补卡：角色在某个世界时刻加入该线（实例设定锁死，加入记录只进运行层，§九 / 附录 B #18）
CREATE TABLE IF NOT EXISTS character_join(
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  character_id TEXT NOT NULL,
  joined_world INTEGER NOT NULL,
  card TEXT NOT NULL,                    -- 卡片快照（复核后固化；实例级定义另见 instance.setting.cards）
  note TEXT NOT NULL DEFAULT '',
  acquainted INTEGER NOT NULL DEFAULT 0, -- 「已相识」声明：补一条对话单元
  created_real REAL NOT NULL,
  commit_id TEXT NOT NULL DEFAULT '',    -- 加入提交（§3.7 三处留痕之一）
  state TEXT NOT NULL DEFAULT 'active',  -- active|revoked：回滚跨过加入点时撤销，记录留着不复活
  request_id TEXT NOT NULL DEFAULT '',   -- 稳定请求标识：同一请求重试返回原子发布结果
  revoked_world INTEGER,
  PRIMARY KEY(instance_id, timeline_id, character_id)
);

CREATE TABLE IF NOT EXISTS rate_command(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  timeline_id TEXT NOT NULL,
  input_real REAL NOT NULL,              -- 权威输入时刻
  effective_real INTEGER NOT NULL,       -- 生效整秒
  rate INTEGER NOT NULL,
  seq INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'pending'  -- pending|applied|cancelled
);
CREATE INDEX IF NOT EXISTS ix_rate_pending ON rate_command(timeline_id, state, effective_real);

CREATE TABLE IF NOT EXISTS unit(
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  character_id TEXT NOT NULL,
  id TEXT NOT NULL,
  mode TEXT NOT NULL,                    -- anchor|event|dialog|time
  semantic TEXT NOT NULL,
  basis TEXT NOT NULL,
  confidence REAL NOT NULL,
  stability REAL NOT NULL DEFAULT 0,     -- 累积稳定度（驱动迁移的隐式依据，不对用户可见）
  archived INTEGER NOT NULL DEFAULT 0,
  consumed TEXT NOT NULL DEFAULT '[]',   -- 已消费来源键（同一来源只消费一次）
  updated_world INTEGER NOT NULL,
  PRIMARY KEY(instance_id, timeline_id, character_id, id)
);
CREATE INDEX IF NOT EXISTS ix_unit_character ON unit(instance_id, timeline_id, character_id);

CREATE TABLE IF NOT EXISTS life_plan(
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  character_id TEXT NOT NULL,
  day_index INTEGER NOT NULL,
  windows TEXT NOT NULL,                 -- 展开后的世界时间窗（含跨日）
  state TEXT NOT NULL DEFAULT 'fixed',
  created_world INTEGER NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  id TEXT NOT NULL,
  PRIMARY KEY(instance_id, timeline_id, character_id, day_index),
  UNIQUE(instance_id, timeline_id, id)
);

CREATE TABLE IF NOT EXISTS experience(
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  character_id TEXT NOT NULL,
  id TEXT NOT NULL,
  world_seconds INTEGER NOT NULL,
  kind TEXT NOT NULL,                    -- life|knowledge|dialog
  summary TEXT NOT NULL,
  source_ref TEXT,                       -- 来源稳定标识（亲历为空）
  confidence TEXT NOT NULL DEFAULT 'experienced',
  PRIMARY KEY(instance_id, timeline_id, character_id, id)
);
CREATE INDEX IF NOT EXISTS ix_experience_window ON experience(instance_id, timeline_id, character_id, world_seconds);

-- TRPG 战役运行时（TRPG_CAMPAIGN_RUNTIME_SPEC）：核心托管的编排状态与规则私有状态附件。
-- 全部键在 (instance_id, timeline_id) 上：随该线的提交 / 回滚 / 分叉 / 导入导出走同一条装载路径。
CREATE TABLE IF NOT EXISTS trpg_campaign(
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  campaign_id TEXT NOT NULL,
  ruleset_id TEXT NOT NULL DEFAULT '',
  ruleset_version TEXT NOT NULL DEFAULT '',
  plugin_manifest TEXT NOT NULL DEFAULT '',
  participants TEXT NOT NULL DEFAULT '[]',   -- JSON：玩家角色 / NPC 引用
  current_scene_id TEXT NOT NULL DEFAULT '',
  state_revision INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'preparing',  -- preparing/active/waiting/paused/blocked/archived
  note TEXT NOT NULL DEFAULT '',
  created_world INTEGER NOT NULL DEFAULT 0,
  updated_world INTEGER NOT NULL DEFAULT 0,
  created_real REAL NOT NULL DEFAULT 0,
  updated_real REAL NOT NULL DEFAULT 0,
  PRIMARY KEY(instance_id, timeline_id, campaign_id)
);

CREATE TABLE IF NOT EXISTS trpg_scene(
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  campaign_id TEXT NOT NULL,
  scene_id TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'exploration',
  location_refs TEXT NOT NULL DEFAULT '[]',
  world_snapshot TEXT NOT NULL DEFAULT '{}', -- {snapshot 引用，不复制世界事实正文}
  participants TEXT NOT NULL DEFAULT '[]',
  public_facts TEXT NOT NULL DEFAULT '[]',
  private_views TEXT NOT NULL DEFAULT '{}',
  active_risks TEXT NOT NULL DEFAULT '[]',
  available_actions TEXT NOT NULL DEFAULT '[]',
  turn_state TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'open',
  revision INTEGER NOT NULL DEFAULT 1,
  created_world INTEGER NOT NULL DEFAULT 0,
  updated_world INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(instance_id, timeline_id, campaign_id, scene_id)
);

CREATE TABLE IF NOT EXISTS trpg_action(
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  campaign_id TEXT NOT NULL,
  scene_id TEXT NOT NULL DEFAULT '',
  action_id TEXT NOT NULL,
  actor_id TEXT NOT NULL DEFAULT '',
  raw_text TEXT NOT NULL DEFAULT '',
  intent TEXT NOT NULL DEFAULT '',
  target_refs TEXT NOT NULL DEFAULT '[]',
  method TEXT NOT NULL DEFAULT '',
  expected_result TEXT NOT NULL DEFAULT '',
  preconditions TEXT NOT NULL DEFAULT '[]',
  visible_risks TEXT NOT NULL DEFAULT '[]',
  confirmation TEXT NOT NULL DEFAULT 'pending',  -- pending/confirmed/modified/abandoned
  action_revision INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'received',       -- 见 campaign.ACTION_STATES
  resolution TEXT NOT NULL DEFAULT '{}',         -- 插件原始响应（含 rule_state_patch / consequences / scene_transition）
  joint_commit_id TEXT NOT NULL DEFAULT '',
  failure_code TEXT NOT NULL DEFAULT '',
  audience TEXT NOT NULL DEFAULT 'public_party',   -- 这份材料的受众（§十五）
  created_world INTEGER NOT NULL DEFAULT 0,
  updated_world INTEGER NOT NULL DEFAULT 0,
  created_real REAL NOT NULL DEFAULT 0,
  updated_real REAL NOT NULL DEFAULT 0,
  PRIMARY KEY(instance_id, timeline_id, campaign_id, action_id)
);

CREATE TABLE IF NOT EXISTS trpg_choice(
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  campaign_id TEXT NOT NULL,
  scene_id TEXT NOT NULL DEFAULT '',
  choice_id TEXT NOT NULL,
  action_id TEXT NOT NULL DEFAULT '',
  prompt_ref TEXT NOT NULL DEFAULT '',
  choices TEXT NOT NULL DEFAULT '[]',
  audience TEXT NOT NULL DEFAULT 'public_party',
  expires_world INTEGER,
  created_revision INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'open',           -- open/selected/cancelled/expired
  selection TEXT NOT NULL DEFAULT '',
  created_real REAL NOT NULL DEFAULT 0,
  updated_real REAL NOT NULL DEFAULT 0,
  created_world INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(instance_id, timeline_id, campaign_id, choice_id)
);

-- 规则状态附件：字段由插件定义，核心只做版本边界（不解析 opaque_state）
CREATE TABLE IF NOT EXISTS trpg_rule_state(
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  campaign_id TEXT NOT NULL,
  ruleset_id TEXT NOT NULL,
  ruleset_version TEXT NOT NULL DEFAULT '',   -- 写这份状态时插件声明的规则版本（§十六 兼容性检查的比对基准）
  state_revision INTEGER NOT NULL DEFAULT 1,
  opaque_state TEXT NOT NULL DEFAULT '{}',
  created_world INTEGER NOT NULL DEFAULT 0,
  updated_world INTEGER NOT NULL DEFAULT 0,
  updated_real REAL NOT NULL DEFAULT 0,
  PRIMARY KEY(instance_id, timeline_id, campaign_id, ruleset_id)
);

-- 联合提交记录：规则状态 patch + 世界后果 + 场景转换同批落地的凭据（幂等键在此）
CREATE TABLE IF NOT EXISTS trpg_commit(
  joint_commit_id TEXT PRIMARY KEY,
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  campaign_id TEXT NOT NULL,
  action_id TEXT NOT NULL DEFAULT '',
  idempotency_key TEXT NOT NULL,
  campaign_revision INTEGER NOT NULL DEFAULT 0,
  world_revision INTEGER NOT NULL DEFAULT 0,
  world_commit_id TEXT NOT NULL DEFAULT '',
  state_revisions TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'committed',
  result TEXT NOT NULL DEFAULT '{}',
  created_world INTEGER NOT NULL DEFAULT 0,
  created_real REAL NOT NULL DEFAULT 0,
  UNIQUE(instance_id, timeline_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS ix_trpg_commit_campaign ON trpg_commit(instance_id, timeline_id, campaign_id);
"""


class EnvelopeConflict(Exception):
    """同键异文：不能静默丢弃或再执行。"""

    def __init__(self, existing: dict[str, Any]) -> None:
        super().__init__("same envelope id with different text")
        self.existing = existing


def _relabel_payload(payload: dict[str, Any], instance_id: str, timeline_id: str) -> dict[str, Any]:
    """把快照里每行都归到目标实例 / 线：分叉与回滚复用同一条装载路径（导入路径早已重映射过）。"""
    out: dict[str, Any] = dict(payload)
    for key, value in payload.items():
        if not isinstance(value, list):
            continue
        rows: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            row = dict(item)
            if "instance_id" in row:
                row["instance_id"] = instance_id
            if "timeline_id" in row:
                row["timeline_id"] = timeline_id
            rows.append(row)
        out[key] = rows
    return out


#: TRPG 战役运行时的表与列（TRPG_CAMPAIGN_RUNTIME_SPEC §十）：列名单一真源，
#: 读 / 写 / 装载共用，避免「导出有、导入后没」的老毛病。
TRPG_COLUMNS: dict[str, tuple[str, ...]] = {
    "campaign": (
        "instance_id", "timeline_id", "campaign_id", "ruleset_id", "ruleset_version",
        "plugin_manifest", "participants", "current_scene_id", "state_revision", "status",
        "note", "created_world", "updated_world", "created_real", "updated_real",
    ),
    "scene": (
        "instance_id", "timeline_id", "campaign_id", "scene_id", "kind", "location_refs",
        "world_snapshot", "participants", "public_facts", "private_views", "active_risks",
        "available_actions", "turn_state", "status", "revision",
        "created_world", "updated_world",
    ),
    "action": (
        "instance_id", "timeline_id", "campaign_id", "scene_id", "action_id", "actor_id",
        "raw_text", "intent", "target_refs", "method", "expected_result", "preconditions",
        "visible_risks", "confirmation", "action_revision", "status", "resolution",
        "joint_commit_id", "failure_code", "audience", "created_world", "updated_world",
        "created_real", "updated_real",
    ),
    "choice": (
        "instance_id", "timeline_id", "campaign_id", "scene_id", "choice_id", "action_id",
        "prompt_ref", "choices", "audience", "expires_world", "created_revision", "status",
        "selection", "created_real", "updated_real", "created_world",
    ),
    "rule_state": (
        "instance_id", "timeline_id", "campaign_id", "ruleset_id", "ruleset_version",
        "state_revision", "opaque_state", "created_world", "updated_world", "updated_real",
    ),
    "commit": (
        "joint_commit_id", "instance_id", "timeline_id", "campaign_id", "action_id",
        "idempotency_key", "campaign_revision", "world_revision", "world_commit_id",
        "state_revisions", "status", "result", "created_world", "created_real",
    ),
}
#: 主键列（冲突目标与更新时排除）
TRPG_KEYS: dict[str, tuple[str, ...]] = {
    "campaign": ("instance_id", "timeline_id", "campaign_id"),
    "scene": ("instance_id", "timeline_id", "campaign_id", "scene_id"),
    "action": ("instance_id", "timeline_id", "campaign_id", "action_id"),
    "choice": ("instance_id", "timeline_id", "campaign_id", "choice_id"),
    "rule_state": ("instance_id", "timeline_id", "campaign_id", "ruleset_id"),
    "commit": ("joint_commit_id",),
}
#: 提交账本只增不改：同一幂等键重放返回原记录，不覆盖
TRPG_APPEND_ONLY = frozenset({"commit"})
_TRPG_SQL_CACHE: dict[str, str] = {}


def _trpg_sql(kind: str) -> str:
    cached = _TRPG_SQL_CACHE.get(kind)
    if cached:
        return cached
    columns = TRPG_COLUMNS[kind]
    keys = TRPG_KEYS[kind]
    updates = [name for name in columns if name not in keys]
    statement = (
        f"INSERT INTO trpg_{kind}({', '.join(columns)})"
        f" VALUES({', '.join(':' + name for name in columns)})"
    )
    if kind in TRPG_APPEND_ONLY:
        statement += " ON CONFLICT DO NOTHING"
    else:
        statement += " ON CONFLICT DO UPDATE SET " + ", ".join(
            f"{name}=excluded.{name}" for name in updates
        )
    _TRPG_SQL_CACHE[kind] = statement
    return statement


def _trpg_row(kind: str, item: dict[str, Any]) -> dict[str, Any]:
    """按表列补齐缺省值：调用方只给关心的字段，其余落 schema 默认之外的稳定默认。"""
    defaults: dict[str, Any] = {name: "" for name in TRPG_COLUMNS[kind]}
    defaults.update({"expires_world": None, "audience": "public_party"})
    for name in TRPG_COLUMNS[kind]:
        if name in ("state_revision", "action_revision", "revision", "created_revision"):
            defaults[name] = 1
        elif name.endswith(("_world", "_real")):
            defaults[name] = 0
    return {**defaults, **{key: value for key, value in item.items() if key in TRPG_COLUMNS[kind]}}


def _trpg_where(kind: str, keys: dict[str, Any]) -> tuple[str, tuple[Any, ...]]:
    if kind not in TRPG_COLUMNS:
        raise ValueError(f"未知的战役运行时表：{kind}")
    if not keys:
        raise ValueError("战役运行时查询必须带作用域")
    unknown = [name for name in keys if name not in TRPG_COLUMNS[kind]]
    if unknown:
        raise ValueError(f"未知列：{', '.join(unknown)}")
    where = " AND ".join(f"{name}=?" for name in keys)
    return where, tuple(keys[name] for name in keys)


def _row_priority(row: dict[str, Any], key: str = "priority") -> int:
    """同刻档位：调用方 / 模板显式声明优先，其次按效果类型查同一张固定表。"""
    declared = row.get(key)
    if isinstance(declared, int) and not isinstance(declared, bool):
        return int(declared)
    from .runtime.events import EFFECT_PRIORITY  # 延迟导入：存储层不反向依赖运行层

    return int(EFFECT_PRIORITY.get(str(row.get("kind") or ""), 0))


def _same_instant_order(
    rows: list[dict[str, Any]], *, at_key: str, priority_key: str = "priority"
) -> list[dict[str, Any]]:
    """同刻顺序（EVENT_ENGINE_SPEC §六）：固定优先规则 + 稳定标识排序。

    结果只取决于内容，不取决于调用方的迭代顺序；同刻行统一写 `seq`，
    读取方按 `(时刻, seq, id)` 拿到的顺序处处一致。
    """
    groups: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(int(row.get(at_key) or 0), []).append(row)
    out: list[dict[str, Any]] = []
    for moment in sorted(groups):
        group = sorted(
            groups[moment],
            key=lambda item: (-_row_priority(item, priority_key), str(item.get("id") or "")),
        )
        for index, row in enumerate(group):
            row = dict(row)
            row["seq"] = index
            out.append(row)
    return out


def _event_payload(row: dict[str, Any]) -> dict[str, Any]:
    """事件行 → 存储形态：effects 一律 JSON 文本（引擎内部给列表，库内给文本）。"""
    payload = dict(row)
    if not isinstance(payload.get("effects"), str):
        payload["effects"] = json.dumps(payload.get("effects") or [], ensure_ascii=False)
    return payload


def _office_payload(row: dict[str, Any]) -> dict[str, Any]:
    """制度行 → 存储形态（列表字段走 JSON 列）。"""
    payload = dict(row)
    for key in ("continues", "suspended"):
        payload[f"{key}_json"] = json.dumps(
            [str(item) for item in payload.get(key) or []], ensure_ascii=False
        )
    return payload


def _custom_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    payload["forms_json"] = json.dumps(
        [str(item) for item in payload.get("forms") or []], ensure_ascii=False
    )
    return payload


def _institution_row(row: sqlite3.Row) -> dict[str, Any]:
    item = _row_to_dict(row)
    for key in ("continues", "suspended"):
        item[key] = json.loads(str(item.pop(f"{key}_json") or "[]"))
    return item


def _custom_row(row: sqlite3.Row) -> dict[str, Any]:
    item = _row_to_dict(row)
    item["forms"] = json.loads(str(item.pop("forms_json") or "[]"))
    return item


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _json_list(value: Any) -> list[str]:
    """JSON 数组列 → 字符串列表（叙事单元的 refs 走这一套，读侧统一在这里解）。"""
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def hash_credential(credential: str) -> str:
    return hashlib.sha256(credential.encode("utf-8")).hexdigest()


def new_credential() -> str:
    return f"cr-{secrets.token_urlsafe(32)}"


def new_binding_token() -> str:
    return f"bt-{secrets.token_urlsafe(18)}"


#: 待提取队列的每角色上限（MEMORY_SPEC §4.1「有界重试队列」）：超出按价值淘汰最旧的
MEMORY_PENDING_CAP = 200


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 写者之间用一把可重入锁串行（SQLite 单写者）；连接按**线程**各持一条：
        # 共享单连接在并发下会互相踩游标与隐式事务——实测 5 线程并发建实例时
        # 出现「提交了却读不回来」的丢写与 sqlite3.DatabaseError: no more rows available。
        self._lock = threading.RLock()
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._pool_lock = threading.Lock()
        self._conn.execute("PRAGMA synchronous=NORMAL")

    @property
    def _conn(self) -> sqlite3.Connection:
        """当前线程的连接（首次使用即建，WAL + busy_timeout）。"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=8000")
            self._local.conn = conn
            with self._pool_lock:
                self._connections.append(conn)
        return conn

    def thread_for_session(self, session_id: str) -> dict[str, Any] | None:
        """会话的主动投递目标：首次绑定默认使用该 thread（§5.3）。"""
        row = self._conn.execute(
            "SELECT * FROM thread WHERE session_id=? ORDER BY updated_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return _row_to_dict(row) if row else None

    def proactive_pending(self, session_id: str, *, since_world: int) -> list[dict[str, Any]]:
        """仍有时效的未投递主动消息（过期的留在历史里，不作为新通知补发）。"""
        rows = self._conn.execute(
            """SELECT m.* FROM message m JOIN proactive_log p ON p.message_id = m.message_id
               WHERE m.session_id=? AND m.reply_to IS NULL AND p.created_world>=?
               ORDER BY m.seq""",
            (session_id, int(since_world)),
        ).fetchall()
        return [_row_to_dict(item) for item in rows]

    def death_exists(self, instance_id: str, timeline_id: str, character_id: str) -> bool:
        """该角色是否已有身故记录（归档判定；不另立字段）。"""
        row = self._conn.execute(
            """SELECT 1 FROM event WHERE instance_id=? AND timeline_id=? AND template=?
               AND world_seconds>0 LIMIT 1""",
            (instance_id, timeline_id, f"death:{character_id}"),
        ).fetchone()
        return row is not None

    def session_notice_get(self, session_id: str, kind: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM session_notice WHERE session_id=? AND kind=?", (session_id, kind)
        ).fetchone()
        return _row_to_dict(row) if row else None

    def session_notice_put(self, row: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT OR IGNORE INTO session_notice(session_id, instance_id, timeline_id, kind,
                                                        message_id, created_real)
                   VALUES(:session_id, :instance_id, :timeline_id, :kind, :message_id, :created_real)""",
                row,
            )

    def session_notice_clear(self, session_id: str, kind: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM session_notice WHERE session_id=? AND kind=?", (str(session_id), str(kind))
            )

    def first_contact_get(self, session_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM first_contact WHERE session_id=?", (session_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None

    def first_contact_put(self, row: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT OR IGNORE INTO first_contact(session_id, instance_id, timeline_id,
                                                        character_id, message_id, at_world, created_real)
                   VALUES(:session_id, :instance_id, :timeline_id, :character_id, :message_id,
                          :at_world, :created_real)""",
                row,
            )

    def proactive_log_add(self, row: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO proactive_log(instance_id, timeline_id, character_id, world_day,
                                             material_ref, message_id, created_world, created_real, state)
                   VALUES(:instance_id, :timeline_id, :character_id, :world_day,
                          :material_ref, :message_id, :created_world, :created_real, :state)""",
                row,
            )

    def proactive_day_count(
        self, instance_id: str, timeline_id: str, character_id: str, *, world_day: int
    ) -> int:
        row = self._conn.execute(
            """SELECT COUNT(*) FROM proactive_log
               WHERE instance_id=? AND timeline_id=? AND character_id=? AND world_day=?""",
            (instance_id, timeline_id, character_id, int(world_day)),
        ).fetchone()
        return int(row[0]) if row else 0

    def proactive_consumed(
        self, instance_id: str, timeline_id: str, character_id: str
    ) -> set[str]:
        rows = self._conn.execute(
            """SELECT material_ref FROM proactive_log
               WHERE instance_id=? AND timeline_id=? AND character_id=? AND material_ref<>''""",
            (instance_id, timeline_id, character_id),
        ).fetchall()
        return {str(item[0]) for item in rows}

    def proactive_list(self, instance_id: str, timeline_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT * FROM proactive_log WHERE instance_id=? AND timeline_id=?
               ORDER BY created_world, id""",
            (instance_id, timeline_id),
        ).fetchall()
        return [_row_to_dict(item) for item in rows]

    # ---------- 叙事中介层（NARRATIVE_LAYER_SPEC §7） ----------

    def narrative_unit_put(self, row: dict[str, Any]) -> None:
        """落一条叙事单元记录：同一单元从「暂缓」到「讲出来」是同一行的推进。"""
        payload = {"refs": "[]", "audit": "[]", "note": "", "message_id": "", "world_day": 0, **row}
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO narrative_unit(instance_id, timeline_id, character_id, id, primary_ref, refs,
                                              entry, relation, topic, stage, message_id, world_day, audit, note,
                                              created_world, updated_world)
                   VALUES(:instance_id, :timeline_id, :character_id, :id, :primary_ref, :refs,
                          :entry, :relation, :topic, :stage, :message_id, :world_day, :audit, :note,
                          :created_world, :updated_world)
                   ON CONFLICT(instance_id, timeline_id, character_id, id) DO UPDATE SET
                     refs=excluded.refs, entry=excluded.entry, relation=excluded.relation, topic=excluded.topic,
                     stage=excluded.stage, message_id=excluded.message_id, world_day=excluded.world_day,
                     audit=excluded.audit, note=excluded.note, updated_world=excluded.updated_world""",
                payload,
            )

    def narrative_unit_list(
        self, instance_id: str, timeline_id: str, *, character_id: str | None = None
    ) -> list[dict[str, Any]]:
        if character_id:
            rows = self._conn.execute(
                """SELECT * FROM narrative_unit WHERE instance_id=? AND timeline_id=? AND character_id=?
                   ORDER BY created_world, id""",
                (instance_id, timeline_id, character_id),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM narrative_unit WHERE instance_id=? AND timeline_id=? ORDER BY created_world, id",
                (instance_id, timeline_id),
            ).fetchall()
        return [_row_to_dict(item) for item in rows]

    def narrative_consumed_refs(self, instance_id: str, timeline_id: str, character_id: str) -> set[str]:
        """已固化消息引用的叙事单元里的材料：这些素材算消费过（§7.1 消费记账）。"""
        rows = self._conn.execute(
            """SELECT refs FROM narrative_unit
               WHERE instance_id=? AND timeline_id=? AND character_id=? AND stage='spoken' AND message_id<>''""",
            (instance_id, timeline_id, character_id),
        ).fetchall()
        out: set[str] = set()
        for item in rows:
            out |= set(_json_list(item[0]))
        return out

    def narrative_deferred_refs(
        self, instance_id: str, timeline_id: str, character_id: str, *, world_day: int
    ) -> set[str]:
        """同一世界日内没讲出口的单元：降级但不禁用——新依据或她改主意都合法（§5.2）。"""
        rows = self._conn.execute(
            """SELECT refs FROM narrative_unit
               WHERE instance_id=? AND timeline_id=? AND character_id=? AND stage='deferred' AND world_day=?""",
            (instance_id, timeline_id, character_id, int(world_day)),
        ).fetchall()
        out: set[str] = set()
        for item in rows:
            out |= set(_json_list(item[0]))
        return out

    # ---------- 整库备份 / 恢复（DESKTOP_SPEC §3.3） ----------

    def backup_create(self, target: str | Path, *, note: str = "") -> dict[str, Any]:
        """一致快照：用 SQLite 在线备份 API 落一份副本，再抹掉明文绑定令牌。

        不复制文件——核心在跑，直接拷文件会带到半截 WAL。备份不含 API Key（它在配置里、不在库内）
        与可重放的连接令牌（副本里清空并抬版本，恢复后必须重新握手）。
        """
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        dest = sqlite3.connect(str(path))
        try:
            with self._lock:
                self._conn.backup(dest)
            dest.execute("UPDATE thread SET binding_token='', binding_version=binding_version+1")
            dest.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('backup_at', ?)",
                (str(time.time()),),
            )
            if note:
                dest.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('backup_note', ?)", (note,))
            dest.commit()
            check = dest.execute("PRAGMA integrity_check").fetchone()
        finally:
            dest.close()
        ok = bool(check) and str(check[0]) == "ok"
        return {
            "file": str(path),
            "bytes": path.stat().st_size if path.exists() else 0,
            "created_at": time.time(),
            "ok": ok,
        }

    def backup_check(self, source: str | Path) -> tuple[bool, str]:
        """暂存库校验：能打开、完整、有受管元数据。恢复前必须过这一关。"""
        path = Path(source)
        if not path.exists():
            return False, "备份文件不存在"
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            return False, f"打不开：{exc}"
        try:
            # 不是数据库的文件在第一条查询上才报错：整段都要接住
            row = conn.execute("PRAGMA integrity_check").fetchone()
            if not row or str(row[0]) != "ok":
                return False, "完整性检查未通过"
            tables = {
                str(item[0])
                for item in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            if not {"meta", "instance", "timeline"} <= tables:
                return False, "不像本项目的受管数据库（缺关键表）"
            version_row = conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            version = str(version_row[0]) if version_row else ""
            if version and version != str(SCHEMA_VERSION):
                return False, f"模式版本不符（备份 {version}，本端 {SCHEMA_VERSION}）"
            return True, ""
        except sqlite3.Error as exc:
            return False, f"不是可用的数据库：{exc}"
        finally:
            conn.close()

    def backup_restore(self, source: str | Path, *, safety: str | Path | None = "auto") -> dict[str, Any]:
        """整库恢复：先保留现库副本，再把暂存库原子切换进来，然后全线冻结 + 令牌失效。

        恢复不是实例导入——它替换整套受管数据（草稿、实例登记、控制记录都按备份来）。
        """
        ok, reason = self.backup_check(source)
        if not ok:
            raise ValueError(f"备份不可用：{reason}")
        if safety == "auto":
            safety = self.backup_dir_default() / "isekai-restore-safety.db"
        kept = self.backup_create(safety, note="restore-before") if safety else None
        src = sqlite3.connect(str(source))
        try:
            with self._lock, self._conn:
                src.backup(self._conn)
                # 恢复后：所有线先冻结（不自动补算旧间隔）、世代提升（在途任务一律作废）、
                # 明文令牌与待生效控制命令失效，必须重新握手与明确激活。
                self._conn.execute("UPDATE timeline SET state='frozen'")
                self._conn.execute("UPDATE timeline_clock SET generation = generation + 1")
                self._conn.execute("DELETE FROM rate_command")
                self._conn.execute("UPDATE thread SET binding_token='', binding_version=binding_version+1")
                self._conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES('restored_at', ?)",
                    (str(time.time()),),
                )
        finally:
            src.close()
        lines = self._conn.execute("SELECT COUNT(*) FROM timeline").fetchone()[0]
        return {"restored": True, "timelines": int(lines), "safety": (kept or {}).get("file", "")}

    def backup_list(self, directory: str | Path) -> list[dict[str, Any]]:
        """只给时间、大小与完整性——不做内容浏览。"""
        folder = Path(directory)
        if not folder.exists():
            return []
        out: list[dict[str, Any]] = []
        for path in sorted(folder.glob("isekai-*.db"), key=lambda item: item.stat().st_mtime, reverse=True):
            row = self._conn.execute("SELECT 1").fetchone()  # 保持同连接语义，避免误用
            _ = row
            ok, reason = self.backup_check(path)
            out.append(
                {
                    "file": str(path),
                    "name": path.name,
                    "bytes": path.stat().st_size,
                    "mtime": path.stat().st_mtime,
                    "ok": ok,
                    "reason": reason,
                }
            )
        return out

    def backup_prune(self, directory: str | Path, *, keep: int) -> list[str]:
        """轮转：只删最旧的、且不删当前这一份；删除失败不影响新备份的可用性。"""
        folder = Path(directory)
        if not folder.exists() or keep <= 0:
            return []
        files = sorted(folder.glob("isekai-*.db"), key=lambda item: item.stat().st_mtime, reverse=True)
        removed: list[str] = []
        for path in files[keep:]:
            try:
                path.unlink()
                # 配对的世界包快照一起轮转（不然只会越堆越多）
                companion = path.with_name(path.name.replace(".db", ".packages.zip"))
                if companion.exists():
                    companion.unlink()
                removed.append(str(path))
            except OSError:
                continue
        return removed

    def backup_dir_default(self) -> Path:
        return Path(self.path).parent / "backups"

    # ---------- 生命周期 ----------

    def _migrate_memory_tables(self) -> None:
        """旧库迁移：memory / memory_task / memory_embedding 改成按线隔离的主键。"""
        def sql_of(table: str) -> str:
            row = self._conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            return str(row["sql"] or "") if row is not None else ""

        timeline_sql = sql_of("timeline")
        if timeline_sql and "description" not in timeline_sql:
            with self._lock, self._conn:
                self._conn.execute("ALTER TABLE timeline ADD COLUMN description TEXT NOT NULL DEFAULT ''")

        memory_sql = sql_of("memory")
        if memory_sql and "PRIMARY KEY(instance_id, timeline_id, id)" not in memory_sql:
            with self._lock, self._conn:
                self._conn.execute(
                    """CREATE TABLE memory_new(
                         id TEXT NOT NULL, instance_id TEXT NOT NULL, timeline_id TEXT NOT NULL,
                         character_id TEXT NOT NULL, text TEXT NOT NULL, kind TEXT NOT NULL,
                         sources TEXT NOT NULL DEFAULT '[]', happened_world INTEGER,
                         learned_world INTEGER NOT NULL, recorded_world INTEGER NOT NULL,
                         semantic_watermark INTEGER NOT NULL, strength REAL NOT NULL DEFAULT 0.6,
                         confidence REAL NOT NULL DEFAULT 0.7, state TEXT NOT NULL DEFAULT 'active',
                         version INTEGER NOT NULL DEFAULT 1, supersedes TEXT, superseded_by TEXT,
                         source_key TEXT, decay_world INTEGER NOT NULL DEFAULT 0,
                         PRIMARY KEY(instance_id, timeline_id, id))"""
                )
                self._conn.execute(
                    """INSERT OR IGNORE INTO memory_new SELECT id, instance_id, timeline_id, character_id,
                         text, kind, sources, happened_world, learned_world, recorded_world,
                         semantic_watermark, strength, confidence, state, version, supersedes,
                         superseded_by, source_key, decay_world FROM memory"""
                )
                self._conn.execute("DROP TABLE memory")
                self._conn.execute("ALTER TABLE memory_new RENAME TO memory")

        task_sql = sql_of("memory_task")
        if task_sql and "PRIMARY KEY(instance_id, timeline_id, id)" not in task_sql:
            with self._lock, self._conn:
                self._conn.execute(
                    """CREATE TABLE memory_task_new(
                         id TEXT NOT NULL, instance_id TEXT NOT NULL, timeline_id TEXT NOT NULL,
                         character_id TEXT NOT NULL, source_kind TEXT NOT NULL, source_ref TEXT NOT NULL,
                         source_world INTEGER NOT NULL, created_world INTEGER NOT NULL,
                         text TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'pending',
                         attempts INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '',
                         UNIQUE(instance_id, timeline_id, character_id, source_kind, source_ref),
                         PRIMARY KEY(instance_id, timeline_id, id))"""
                )
                self._conn.execute(
                    """INSERT OR IGNORE INTO memory_task_new SELECT id, instance_id, timeline_id,
                         character_id, source_kind, source_ref, source_world, created_world, text,
                         state, attempts, note FROM memory_task"""
                )
                self._conn.execute("DROP TABLE memory_task")
                self._conn.execute("ALTER TABLE memory_task_new RENAME TO memory_task")

        embed_sql = sql_of("memory_embedding")
        if embed_sql and "timeline_id" not in embed_sql:
            with self._lock, self._conn:
                self._conn.execute(
                    """CREATE TABLE memory_embedding_new(
                         memory_id TEXT NOT NULL, instance_id TEXT NOT NULL,
                         timeline_id TEXT NOT NULL DEFAULT '', model TEXT NOT NULL, dim INTEGER NOT NULL,
                         vector BLOB NOT NULL, source_version INTEGER NOT NULL, created_at REAL NOT NULL,
                         PRIMARY KEY(instance_id, timeline_id, memory_id))"""
                )
                self._conn.execute(
                    """INSERT OR IGNORE INTO memory_embedding_new
                         (memory_id, instance_id, timeline_id, model, dim, vector, source_version, created_at)
                       SELECT e.memory_id, e.instance_id, COALESCE(m.timeline_id, ''), e.model, e.dim,
                              e.vector, e.source_version, e.created_at
                       FROM memory_embedding e LEFT JOIN memory m ON m.id = e.memory_id"""
                )
                self._conn.execute("DROP TABLE memory_embedding")
                self._conn.execute("ALTER TABLE memory_embedding_new RENAME TO memory_embedding")

        # 引用记录也要按线隔离：导入副本 / 分叉线会持有同一 turn_id+memory_id，
        # 单主键会把副本那份静默吞掉（派生表一律复合主键）
        citation_sql = sql_of("memory_citation")
        if citation_sql and "PRIMARY KEY(timeline_id" not in citation_sql:
            with self._lock, self._conn:
                self._conn.execute(
                    """CREATE TABLE memory_citation_new(
                         turn_id TEXT NOT NULL, memory_id TEXT NOT NULL, timeline_id TEXT NOT NULL,
                         character_id TEXT NOT NULL, world_seconds INTEGER NOT NULL, created_at REAL NOT NULL,
                         PRIMARY KEY(timeline_id, turn_id, memory_id))"""
                )
                self._conn.execute(
                    """INSERT OR IGNORE INTO memory_citation_new(turn_id, memory_id, timeline_id, character_id,
                                                                world_seconds, created_at)
                       SELECT turn_id, memory_id, timeline_id, character_id, world_seconds, created_at
                       FROM memory_citation"""
                )
                self._conn.execute("DROP TABLE memory_citation")
                self._conn.execute("ALTER TABLE memory_citation_new RENAME TO memory_citation")

    def ensure_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate_memory_tables()
            self._migrate_disclosure_pk()
            self._migrate_runtime_tables()
            row = self._conn.execute("SELECT value FROM meta WHERE key='data_format'").fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES('data_format', ?)", (DATA_FORMAT_VERSION,)
                )
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema', ?)", (str(SCHEMA_VERSION),)
            )
            self._conn.commit()

    def _migrate_disclosure_pk(self) -> None:
        """旧库迁移：披露授权表从 id 单主键改成 (实例, 线, id) 复合主键。

        导入的副本与源实例会持有同一个授权标识，单主键会把副本那一份静默吞掉（§7.3）。
        """
        sql = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='disclosure'"
        ).fetchone()
        if not sql or "PRIMARY KEY(instance_id, timeline_id, id)" in str(sql[0]):
            return
        with self._lock, self._conn:
            self._conn.execute(
                """CREATE TABLE disclosure_new(
                     id TEXT NOT NULL, instance_id TEXT NOT NULL, timeline_id TEXT NOT NULL,
                     from_character TEXT NOT NULL, to_character TEXT NOT NULL, scope TEXT NOT NULL,
                     granted_world INTEGER NOT NULL, granted_real REAL NOT NULL,
                     note TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'granted',
                     PRIMARY KEY(instance_id, timeline_id, id))"""
            )
            self._conn.execute(
                """INSERT OR IGNORE INTO disclosure_new
                     (id, instance_id, timeline_id, from_character, to_character, scope,
                      granted_world, granted_real, note, state)
                   SELECT id, instance_id, timeline_id, from_character, to_character, scope,
                          granted_world, granted_real, note, state FROM disclosure"""
            )
            self._conn.execute("DROP TABLE disclosure")
            self._conn.execute("ALTER TABLE disclosure_new RENAME TO disclosure")

    def _migrate_runtime_tables(self) -> None:
        """运行层表的形态迁移（旧库就地升级；派生数据重建是最后手段）。"""
        expected = {
            "unit": ("instance_id", "timeline_id", "character_id", "id"),
            "experience": ("instance_id", "timeline_id", "character_id", "id"),
            "life_plan": ("instance_id", "timeline_id", "character_id", "day_index"),
        }
        for table, columns in expected.items():
            rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            if not rows:
                continue
            primary = tuple(row["name"] for row in rows if row["pk"])
            if primary == columns:
                continue
            log.warning("重建运行层表 %s（主键形态升级：%s → %s）", table, primary, columns)
            self._conn.executescript(f"DROP TABLE {table};")
        # 加列：不重建，直接补（§2.6 追赶状态与补卡表随阶段 2 审计加入）
        clock_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(timeline_clock)")}
        for name, ddl in (("catching_up", "INTEGER NOT NULL DEFAULT 0"), ("limited", "INTEGER NOT NULL DEFAULT 0")):
            if clock_columns and name not in clock_columns:
                log.info("timeline_clock 增列 %s", name)
                self._conn.execute(f"ALTER TABLE timeline_clock ADD COLUMN {name} {ddl}")
        claim_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(claim)")}
        if claim_columns and "derived_from" not in claim_columns:
            log.info("claim 增列 derived_from")
            self._conn.execute("ALTER TABLE claim ADD COLUMN derived_from TEXT")
        effect_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(effect_state)")}
        if effect_columns and "family" not in effect_columns:
            log.info("effect_state 增列 family")
            self._conn.execute("ALTER TABLE effect_state ADD COLUMN family TEXT NOT NULL DEFAULT ''")
        if effect_columns and "value" not in effect_columns:
            log.info("effect_state 增列 value")
            self._conn.execute("ALTER TABLE effect_state ADD COLUMN value TEXT")
        if effect_columns and "priority" not in effect_columns:
            log.info("effect_state 增列 priority / seq（同刻顺序，§六）")
            self._conn.execute("ALTER TABLE effect_state ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
            self._conn.execute("ALTER TABLE effect_state ADD COLUMN seq INTEGER NOT NULL DEFAULT 0")
        message_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(message)")}
        if message_columns and "model_fingerprint" not in message_columns:
            log.info("message 增列：model_fingerprint")
            self._conn.execute("ALTER TABLE message ADD COLUMN model_fingerprint TEXT NOT NULL DEFAULT ''")
        if message_columns and "wait_until" not in message_columns:
            log.info("message 增列 wait_until（睡眠期合并批的截止点，§4.5）")
            self._conn.execute("ALTER TABLE message ADD COLUMN wait_until REAL NOT NULL DEFAULT 0")
        if message_columns and "attachments" not in message_columns:
            log.info("message 增列 attachments（附件随消息持久化，§七）")
            self._conn.execute("ALTER TABLE message ADD COLUMN attachments TEXT NOT NULL DEFAULT '[]'")
        # TRPG 规则状态兼容性比对需要的规则版本列（早先建的表没有）
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(trpg_rule_state)")}
        if columns and "ruleset_version" not in columns:
            self._conn.execute(
                "ALTER TABLE trpg_rule_state ADD COLUMN ruleset_version TEXT NOT NULL DEFAULT ''"
            )
        # 行动材料的受众列（§十五）
        action_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(trpg_action)")}
        if action_columns and "audience" not in action_columns:
            self._conn.execute(
                "ALTER TABLE trpg_action ADD COLUMN audience TEXT NOT NULL DEFAULT 'public_party'"
            )
        join_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(character_join)")}
        for name, ddl in (
            ("commit_id", "TEXT NOT NULL DEFAULT ''"),
            ("state", "TEXT NOT NULL DEFAULT 'active'"),
            ("request_id", "TEXT NOT NULL DEFAULT ''"),
            ("revoked_world", "INTEGER"),
        ):
            if join_columns and name not in join_columns:
                log.info("character_join 增列 %s", name)
                self._conn.execute(f"ALTER TABLE character_join ADD COLUMN {name} {ddl}")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            for conn in list(self._connections):
                try:
                    conn.commit()
                    conn.close()
                except sqlite3.Error:  # pragma: no cover - 已关掉的连接忽略
                    pass
            self._connections.clear()
            self._local = threading.local()

    # ---------- 通道实例 ----------

    def channel_by_name(self, name: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM channel_instance WHERE name=?", (name,)).fetchone()
        return _row_to_dict(row) if row else None

    def channel_get(self, channel_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM channel_instance WHERE id=?", (channel_id,)).fetchone()
        return _row_to_dict(row) if row else None

    def channel_register(
        self,
        *,
        name: str,
        display_name: str,
        version: str,
        protocol: str,
        capabilities: dict[str, Any],
        rotate: bool = True,
    ) -> tuple[dict[str, Any], str | None]:
        """登记通道实例并签发凭据，返回 (行, 明文凭据)。

        `rotate=False` 且实例已存在时不轮换：凭据只在首次签发或显式轮换时更新，
        否则多个调用方会互相把对方踢下线。返回的凭据为 None 表示沿用既有凭据。
        """
        credential = new_credential()
        now = time.time()
        with self._lock, self._conn:
            row = self._conn.execute("SELECT * FROM channel_instance WHERE name=?", (name,)).fetchone()
            if row is None:
                channel_id = f"ci-{secrets.token_hex(4)}"
                self._conn.execute(
                    """INSERT INTO channel_instance(id, name, version, protocol, capabilities,
                                                   credential_hash, created_at, last_seen_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        channel_id,
                        name,
                        version,
                        protocol,
                        json.dumps(capabilities, ensure_ascii=False),
                        hash_credential(credential),
                        now,
                        now,
                    ),
                )
                self._conn.commit()
                return self.channel_get(channel_id), credential  # type: ignore[return-value]
            channel_id = row["id"]
            if rotate:
                self._conn.execute(
                    """UPDATE channel_instance SET version=?, protocol=?, capabilities=?,
                                                   credential_hash=?, last_seen_at=? WHERE id=?""",
                    (
                        version,
                        protocol,
                        json.dumps(capabilities, ensure_ascii=False),
                        hash_credential(credential),
                        now,
                        channel_id,
                    ),
                )
                issued: str | None = credential
            else:
                self._conn.execute(
                    "UPDATE channel_instance SET version=?, last_seen_at=? WHERE id=?",
                    (version, now, channel_id),
                )
                issued = None
        return self.channel_get(channel_id), issued  # type: ignore[return-value]

    def channel_verify_credential(self, name: str, credential: str) -> dict[str, Any] | None:
        row = self.channel_by_name(name)
        if row is None:
            return None
        if not secrets.compare_digest(row["credential_hash"], hash_credential(credential)):
            return None
        return row

    def channel_forget(self, ref: str) -> dict[str, int]:
        """卸载通道：删登记与绑定（会话 / 消息 / 角色历史一概不动）。"""
        row = self.channel_get(str(ref)) or self.channel_by_name(str(ref))
        if row is None:
            return {"channel": 0, "threads": 0}
        with self._lock, self._conn:
            threads = self._conn.execute("DELETE FROM thread WHERE channel_id=?", (row["id"],)).rowcount
            channels = self._conn.execute("DELETE FROM channel_instance WHERE id=?", (row["id"],)).rowcount
        return {"channel": max(0, int(channels or 0)), "threads": max(0, int(threads or 0))}

    def channel_touch(self, channel_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE channel_instance SET last_seen_at=? WHERE id=?", (time.time(), channel_id)
            )

    def channel_set_handshake(self, channel_id: str, protocol: str, capabilities: dict[str, Any]) -> None:
        """持久记录协商结果：协议版本、能力交集与限额（握手一次，重连复用）。"""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE channel_instance SET protocol=?, capabilities=?, last_seen_at=? WHERE id=?",
                (protocol, json.dumps(capabilities, ensure_ascii=False), time.time(), channel_id),
            )

    # ---------- 会话 ----------

    def session_ensure(self, instance_id: str, timeline_id: str, character_id: str) -> dict[str, Any]:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM session WHERE instance_id=? AND timeline_id=? AND character_id=?",
                (instance_id, timeline_id, character_id),
            ).fetchone()
            if row is None:
                session_id = f"se-{secrets.token_hex(4)}"
                self._conn.execute(
                    "INSERT INTO session(id, instance_id, timeline_id, character_id, created_at) VALUES(?,?,?,?,?)",
                    (session_id, instance_id, timeline_id, character_id, time.time()),
                )
                row = self._conn.execute("SELECT * FROM session WHERE id=?", (session_id,)).fetchone()
        return _row_to_dict(row)  # type: ignore[arg-type]

    def session_get(self, session_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM session WHERE id=?", (str(session_id),)).fetchone()
        return _row_to_dict(row) if row else None

    def session_find(self, instance_id: str, timeline_id: str, character_id: str) -> dict[str, Any] | None:
        """按三元组找会话（只读；没有就返回 None——建会话仍走 `session_ensure`）。"""
        row = self._conn.execute(
            "SELECT * FROM session WHERE instance_id=? AND timeline_id=? AND character_id=? LIMIT 1",
            (str(instance_id), str(timeline_id), str(character_id)),
        ).fetchone()
        return _row_to_dict(row) if row else None

    def session_list(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM session ORDER BY created_at").fetchall()
        return [_row_to_dict(r) for r in rows]

    # ---------- thread 绑定 ----------

    def thread_get(self, channel_id: str, thread_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM thread WHERE channel_id=? AND thread_id=?", (channel_id, thread_id)
        ).fetchone()
        return _row_to_dict(row) if row else None

    def thread_bind(self, channel_id: str, thread_id: str, session_id: str) -> dict[str, Any]:
        """绑定 / 重绑：换代表令（binding_version + token 一起换）。"""
        token = new_binding_token()
        now = time.time()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM thread WHERE channel_id=? AND thread_id=?", (channel_id, thread_id)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    """INSERT INTO thread(channel_id, thread_id, session_id, binding_version, binding_token, updated_at)
                       VALUES(?,?,?,1,?,?)""",
                    (channel_id, thread_id, session_id, token, now),
                )
            else:
                self._conn.execute(
                    """UPDATE thread SET session_id=?, binding_version=binding_version+1,
                                         binding_token=?, updated_at=? WHERE id=?""",
                    (session_id, token, now, row["id"]),
                )
        return self.thread_get(channel_id, thread_id)  # type: ignore[return-value]

    def thread_list(self, channel_id: str | None = None) -> list[dict[str, Any]]:
        if channel_id is None:
            rows = self._conn.execute("SELECT * FROM thread ORDER BY id").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM thread WHERE channel_id=? ORDER BY id", (channel_id,)
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ---------- 入站 ----------

    def inbound_put(
        self,
        *,
        session_id: str,
        channel_id: str,
        thread_id: str,
        env_id: str,
        binding_version: int,
        text: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """插入入站消息；同键同文返回既有行，同键异文（或附件不同）抛 EnvelopeConflict。

        附件以 base64 内联在行里（上限由协商配额兜底，默认单件 512 KiB）：
        ponytail: 内联省一张表与一套导出/回滚管线；附件变大再搬到 data/attachments 的 blob 文件。
        """
        blob = json.dumps(attachments or [], ensure_ascii=False)
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM message WHERE channel_id=? AND thread_id=? AND env_id=?",
                (channel_id, thread_id, env_id),
            ).fetchone()
            if row is not None:
                if row["text"] != text or str(row["attachments"] or "[]") != blob:
                    raise EnvelopeConflict(_row_to_dict(row))
                return _row_to_dict(row), False
            cur = self._conn.execute(
                """INSERT INTO message(session_id, role, channel_id, thread_id, env_id, binding_version,
                                       text, attachments, state, created_at)
                   VALUES(?, 'user', ?,?,?,?,?,?, 'queued', ?)""",
                (session_id, channel_id, thread_id, env_id, binding_version, text, blob, time.time()),
            )
            seq = cur.lastrowid
            row = self._conn.execute("SELECT * FROM message WHERE seq=?", (seq,)).fetchone()
        return _row_to_dict(row), True  # type: ignore[arg-type]

    def message_get(self, seq: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM message WHERE seq=?", (seq,)).fetchone()
        return _row_to_dict(row) if row else None

    def inbound_find(self, channel_id: str, thread_id: str, env_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM message WHERE channel_id=? AND thread_id=? AND env_id=?",
            (channel_id, thread_id, env_id),
        ).fetchone()
        return _row_to_dict(row) if row else None

    def last_inbound(self, session_id: str) -> dict[str, Any] | None:
        """该会话最近一条入站（只读）：产品状态投影按它算「这一轮到哪了」。"""
        row = self._conn.execute(
            "SELECT * FROM message WHERE session_id=? AND role='user' ORDER BY seq DESC LIMIT 1",
            (str(session_id),),
        ).fetchone()
        return _row_to_dict(row) if row else None

    def inbound_set_state(self, seq: int, state: str, *, error_code: str | None = None,
                          reply_message_id: str | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """UPDATE message SET state=?, error_code=?,
                                     reply_message_id=COALESCE(?, reply_message_id) WHERE seq=?""",
                (state, error_code, reply_message_id, seq),
            )

    def inbound_claim_deadline(self, seq: int, *, delay: float) -> float:
        """睡眠期合并批的截止点：以首次接受该输入的现实时间为基准，一次确定并持久化（§4.5）。

        已确定过就返回原值——后续输入不重置、不按条数叠加；取快照与排队耗去的时间
        由调用方按 `deadline - now` 计入等待。
        """
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT created_at, wait_until FROM message WHERE seq=?", (seq,)
            ).fetchone()
            if row is None:
                return time.time() + float(delay)
            claimed = float(row["wait_until"] or 0.0)
            if claimed > 0:
                return claimed
            deadline = float(row["created_at"] or 0.0) + float(delay)
            self._conn.execute("UPDATE message SET wait_until=? WHERE seq=?", (deadline, seq))
        return deadline

    def inbound_queued_after(self, session_id: str, seq: int) -> list[dict[str, Any]]:
        """该会话接受顺序上排在这条入站之后、仍未处理的入站（合并批候选，§4.5）。"""
        rows = self._conn.execute(
            """SELECT * FROM message WHERE session_id=? AND role='user' AND state='queued' AND seq>?
               ORDER BY seq""",
            (session_id, int(seq)),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def inbound_queued_count(self, session_id: str) -> int:
        """该会话排队中的入站条数（§3.2 容量闸：满了拒绝新输入，不丢弃已接受的）。"""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM message WHERE session_id=? AND role='user' AND state='queued'",
            (session_id,),
        ).fetchone()
        return int((row or {"n": 0})["n"])

    def interrupt_open_turns(self) -> int:
        """启动时收尾上次进程留下的未完成轮次：标记中断，等待显式重试。

        提交是单事务（状态与产物一起发布），因此 queued / processing 的行必然没有
        已固化的结果，标记为 interrupted 可重试不会造成漏单或执行两次（SESSION_CORE §4.3）。
        """
        with self._lock, self._conn:
            cur = self._conn.execute(
                """UPDATE message SET state='failed', error_code='interrupted'
                   WHERE role='user' AND state IN ('queued','processing')"""
            )
            return int(cur.rowcount or 0)

    # ---------- 出站 ----------

    def outbound_put(
        self,
        *,
        session_id: str,
        message_id: str,
        reply_to: str | None,
        covers: Iterable[str],
        batches: list[list[str]],
        target_channel: str,
        target_thread: str,
        binding_version: int,
        binding_token: str,
        role: str = "character",
        model_fingerprint: str = "",
    ) -> dict[str, Any]:
        """固化回复（一次逻辑轮次的产物）。batches 是分批后的分段计划。

        `role` 区分作者分类：角色发言是 `character`，联络系统 / 管理机制的说明是 `notice`
        （不进角色上下文，投递走 `system_notice` 信封——CHANNEL_PLUGIN_SPEC §2.3）。
        """
        with self._lock, self._conn:
            return self._outbound_put_locked(
                session_id=session_id,
                message_id=message_id,
                reply_to=reply_to,
                covers=covers,
                batches=batches,
                target_channel=target_channel,
                target_thread=target_thread,
                binding_version=binding_version,
                binding_token=binding_token,
                role=role,
            )

    def _outbound_put_locked(self, **kwargs: Any) -> dict[str, Any]:
        session_id = kwargs["session_id"]
        message_id = kwargs["message_id"]
        batches = kwargs["batches"]
        covers_json = json.dumps(list(kwargs["covers"]), ensure_ascii=False)
        now = time.time()
        cur = self._conn.execute(
            """INSERT INTO message(session_id, role, channel_id, thread_id, binding_version, binding_token,
                                   message_id, reply_to, batch_id, batch_index, batch_count, parts, covers,
                                   model_fingerprint, state, created_at)
               VALUES(?, ?, ?,?,?,?, ?,?, ?, 0, ?, ?, ?, ?, 'fixed', ?)""",
            (
                session_id,
                str(kwargs.get("role") or "character"),
                kwargs["target_channel"],
                kwargs["target_thread"],
                kwargs["binding_version"],
                kwargs["binding_token"],
                message_id,
                kwargs["reply_to"],
                message_id,
                len(batches),
                json.dumps(batches, ensure_ascii=False),
                covers_json,
                str(kwargs.get("model_fingerprint") or ""),
                now,
            ),
        )
        msg_seq = cur.lastrowid
        for index in range(len(batches)):
            self._conn.execute(
                "INSERT OR REPLACE INTO delivery(msg_seq, batch_index, state, updated_at) VALUES(?,?, 'pending', ?)",
                (msg_seq, index, now),
            )
        row = self._conn.execute("SELECT * FROM message WHERE seq=?", (msg_seq,)).fetchone()
        return _row_to_dict(row)  # type: ignore[arg-type]

    def commit_turn(
        self,
        *,
        inbound_seq: int,
        outbound: dict[str, Any],
        inbound_seqs: Iterable[int] | None = None,
    ) -> dict[str, Any]:
        """一次逻辑轮次的固化：回复与输入处理状态共同发布（SESSION_CORE_SPEC §4.2）。

        合并批（§4.5）：批内每条入站都指向同一份固化回复，查询或重试任一条都得到同一
        message_id；不逐条补发、不重复提取同一来源。
        """
        seqs = [int(item) for item in (list(inbound_seqs) if inbound_seqs is not None else [inbound_seq])]
        with self._lock, self._conn:
            row = self._outbound_put_locked(**outbound)
            marks = ",".join("?" for _ in seqs)
            self._conn.execute(
                f"UPDATE message SET state='done', error_code=NULL, reply_message_id=? "
                f"WHERE seq IN ({marks})",
                (outbound["message_id"], *seqs),
            )
            return row

    def outbound_by_message_id(self, message_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM message WHERE message_id=? AND role != 'user'", (message_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None

    # ---------- 投递 ----------

    def delivery_rows(self, msg_seq: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM delivery WHERE msg_seq=? ORDER BY batch_index", (msg_seq,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def delivery_set(self, msg_seq: int, batch_index: int, state: str) -> str:
        """更新单批状态并返回整条消息的汇总状态。

        迟到回执不能倒退已确认状态：`accepted` 不被 `unknown` 覆盖（CHANNEL_PLUGIN_SPEC §2.3）。
        """
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT state FROM delivery WHERE msg_seq=? AND batch_index=?", (msg_seq, batch_index)
            ).fetchone()
            if row is not None:
                current = row["state"]
                regress = current == "accepted" and state in ("unknown", "pending")
                if not regress:
                    self._conn.execute(
                        "UPDATE delivery SET state=?, updated_at=? WHERE msg_seq=? AND batch_index=?",
                        (state, time.time(), msg_seq, batch_index),
                    )
        return self.delivery_rollup(msg_seq)

    def delivery_rollup(self, msg_seq: int) -> str:
        states = [r["state"] for r in self.delivery_rows(msg_seq)]
        if not states:
            return "unknown"
        if all(s == "accepted" for s in states):
            return "delivered"
        if any(s == "unknown" for s in states):
            return "unknown"
        if any(s == "failed" for s in states):
            return "failed"
        if any(s == "incompatible" for s in states):
            return "incompatible"
        if all(s in ("sent", "accepted") for s in states):
            return "sent"
        return "pending"

    def session_has_inbound(self, session_id: str) -> bool:
        """该会话是否已有用户侧来言（初见不双发用，§5.6）。"""
        row = self._conn.execute(
            "SELECT 1 FROM message WHERE session_id=? AND role='user' LIMIT 1", (str(session_id),)
        ).fetchone()
        return row is not None

    def has_later_success(self, session_id: str, seq: int) -> bool:
        """该轮次之后是否已经有成功轮次（旧失败轮次的显式重试要拒，§4.3）。"""
        row = self._conn.execute(
            """SELECT 1 FROM message WHERE session_id=? AND seq>? AND role='user' AND state='done' LIMIT 1""",
            (str(session_id), int(seq)),
        ).fetchone()
        return row is not None

    def pending_outbound(self, channel_id: str, thread_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """仍可投递的已固化回复（未确认成功的批次）。"""
        rows = self._conn.execute(
            """SELECT m.* FROM message m
               JOIN delivery d ON d.msg_seq = m.seq
               WHERE m.channel_id=? AND m.thread_id=? AND m.role IN ('character','notice')
                 AND d.state IN ('pending','sent','failed')
               GROUP BY m.seq ORDER BY m.seq LIMIT ?""",
            (channel_id, thread_id, limit),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ---------- 历史 ----------

    def history_page(self, session_id: str, *, limit: int = 50, before_seq: int | None = None) -> dict[str, Any]:
        params: list[Any] = [session_id]
        clause = ""
        if before_seq is not None:
            clause = "AND seq < ?"
            params.append(before_seq)
        params.append(limit + 1)
        rows = self._conn.execute(
            f"SELECT * FROM message WHERE session_id=? {clause} ORDER BY seq DESC LIMIT ?", params
        ).fetchall()
        rows = list(reversed(rows))
        has_more = len(rows) > limit
        rows = rows[-limit:] if has_more else rows
        # 历史版本：本会话的最大序号。回滚会删掉行让这个值倒退，客户端据此判定本地
        # 缓存与旧分页游标失效（§3 / 验收 8），不必自己猜。
        head = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS revision FROM message WHERE session_id=?", (session_id,)
        ).fetchone()
        return {
            "messages": [_row_to_dict(r) for r in rows],
            "has_more": has_more,
            "next_before_seq": rows[0]["seq"] if rows else None,
            "revision": int(head["revision"] if head is not None else 0),
        }

    def context_window(self, session_id: str, limit: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT * FROM message WHERE session_id=? AND role IN ('user','character') AND state IN ('done','fixed')
               ORDER BY seq DESC LIMIT ?""",
            (session_id, limit),
        ).fetchall()
        return [_row_to_dict(r) for r in reversed(rows)]

    # ---------- 作废 ----------

    def void_put(self, channel_id: str, thread_id: str, env_id: str, reason: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO voided(channel_id, thread_id, env_id, reason, at) VALUES(?,?,?,?,?)",
                (channel_id, thread_id, env_id, reason, time.time()),
            )

    def void_has(self, channel_id: str, thread_id: str, env_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM voided WHERE channel_id=? AND thread_id=? AND env_id=?",
            (channel_id, thread_id, env_id),
        ).fetchone()
        return row is not None

    def counts(self) -> dict[str, int]:
        def one(sql: str) -> int:
            return int(self._conn.execute(sql).fetchone()[0])

        return {
            "sessions": one("SELECT COUNT(*) FROM session"),
            "threads": one("SELECT COUNT(*) FROM thread"),
            "channels": one("SELECT COUNT(*) FROM channel_instance"),
            "messages": one("SELECT COUNT(*) FROM message"),
            "instances": one("SELECT COUNT(*) FROM instance"),
        }

    # ---------- 实例（世界设定层） ----------

    def instance_names(self) -> list[str]:
        # 只读也要走同一把锁 + 显式收尾：裸 SELECT 会在共享连接上留下隐式事务，
        # 与并发写入混在一起时会把别处的提交卷回（实测：并发建实例时偶发丢写）
        with self._lock, self._conn:
            rows = self._conn.execute("SELECT name FROM instance").fetchall()
            return [str(row["name"]) for row in rows]

    def instance_get(self, instance_id: str) -> dict[str, Any] | None:
        with self._lock, self._conn:
            row = self._conn.execute("SELECT * FROM instance WHERE id=?", (instance_id,)).fetchone()
            return _row_to_dict(row) if row else None

    def instance_by_name(self, name: str) -> dict[str, Any] | None:
        with self._lock, self._conn:
            row = self._conn.execute("SELECT * FROM instance WHERE name=?", (name,)).fetchone()
            return _row_to_dict(row) if row else None

    def instance_list(self) -> list[dict[str, Any]]:
        """管理面列表：只有公开元数据，不含世界内部内容（§3.5）。"""
        rows = self._conn.execute(
            """SELECT i.id, i.name, i.original_name, i.package_id, i.data_format, i.rules_version,
                      i.seed, i.moment, i.imported, i.created_at,
                      (SELECT COUNT(*) FROM timeline t WHERE t.instance_id=i.id) AS timelines,
                      (SELECT COUNT(*) FROM session s WHERE s.instance_id=i.id) AS sessions
               FROM instance i ORDER BY i.created_at"""
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def _settle_connection(self) -> None:
        """把连接上的隐式事务收干净（并发写前的清场）。

        只读方法（如 instance_names）会隐式开启一个读事务且从不提交；同一连接上随后任何
        写方法的 rollback 都会从那个点往后卷，把别处已"提交"的写入一起带走。
        """
        try:
            self._conn.rollback()
        except sqlite3.Error:  # pragma: no cover - 连接层面没事务时忽略
            pass

    def instance_create(self, row: dict[str, Any], *, timelines: list[dict[str, Any]], commits: list[dict[str, Any]]) -> None:
        """原子固化：实例行 + 初始时间线 + 初始提交一起落入，失败不留半个实例（§3.4）。

        并发下连接上可能挂着别人留下的隐式事务：先收干净、再显式开一个写事务并提交，
        否则一次 UNIQUE 冲突的 rollback 会把同一连接上还没落到盘上的行一起卷走
        （实测：5 线程并发建同名实例时，成功的调用回来读不到自己刚建的行）。
        """
        with self._lock:
            self._settle_connection()
            with self._conn:
                self._conn.execute(
                    """INSERT INTO instance(id, name, original_name, package_id, data_format, rules_version,
                                            app_version, seed, moment, setting, imported, created_at)
                       VALUES(:id, :name, :original_name, :package_id, :data_format, :rules_version,
                              :app_version, :seed, :moment, :setting, :imported, :created_at)""",
                    row,
                )
                for timeline in timelines:
                    self._conn.execute(
                        """INSERT INTO timeline(id, instance_id, name, state, source_commit, created_at)
                           VALUES(:id, :instance_id, :name, :state, :source_commit, :created_at)""",
                        timeline,
                    )
                for commit in commits:
                    self._conn.execute(
                        """INSERT INTO commit_log(id, instance_id, timeline_id, kind, moment, note, created_at)
                           VALUES(:id, :instance_id, :timeline_id, :kind, :moment, :note, :created_at)""",
                        commit,
                    )

    def instance_convert(
        self, instance_id: str, *, setting: str, data_format: str, rules_version: str
    ) -> None:
        """转换后的原子发布（§7.6）：设定快照与版本标记一起写；线先冻结，由用户明确激活再推进。

        写在同一事务里：中途失败保持原实例（旧设定与旧版本标记都还在）。
        """
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE instance SET setting=?, data_format=?, rules_version=? WHERE id=?",
                (setting, str(data_format), str(rules_version), instance_id),
            )
            self._conn.execute(
                "UPDATE timeline SET state='frozen' WHERE instance_id=? AND state<>'archived'",
                (instance_id,),
            )

    def instance_rename(self, instance_id: str, name: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE instance SET name=? WHERE id=?", (name, instance_id))

    def instance_delete(self, instance_id: str) -> None:
        """删除实例及其会话 / 线程绑定 / 消息 / 时间线 / 提交；凭据与通道不连带删除。"""
        with self._lock, self._conn:
            session_ids = [
                row["id"]
                for row in self._conn.execute("SELECT id FROM session WHERE instance_id=?", (instance_id,)).fetchall()
            ]
            for session_id in session_ids:
                self._conn.execute(
                    "DELETE FROM delivery WHERE msg_seq IN (SELECT seq FROM message WHERE session_id=?)",
                    (session_id,),
                )
                self._conn.execute("DELETE FROM message WHERE session_id=?", (session_id,))
                self._conn.execute("DELETE FROM thread WHERE session_id=?", (session_id,))
                self._conn.execute("DELETE FROM session WHERE id=?", (session_id,))
            # 运行层也要清：否则删掉实例会留下孤儿状态（阶段 2 审计发现）
            self._conn.execute(
                "DELETE FROM timeline_clock WHERE timeline_id IN (SELECT id FROM timeline WHERE instance_id=?)",
                (instance_id,),
            )
            self._conn.execute(
                "DELETE FROM rate_command WHERE timeline_id NOT IN (SELECT id FROM timeline)"
            )
            for table in (
                "unit",
                "life_plan",
                "experience",
                "character_join",
                "event",
                "claim",
                "knowledge",
                "effect_state",
                "intent",
                "call_ledger",
                "commit_snapshot",
                "disclosure",
                "event_draft",
                "pending_event",
                "commit_auto_state",
                "memory",
                "memory_task",
                "memory_embedding",
                "budget_reserve",
                "budget_policy",
                "environment_state",
                "institution_state",
                "custom_state",
                "proactive_log",
                "first_contact",
                "session_notice",
                "narrative_unit",
                "trpg_campaign",
                "trpg_scene",
                "trpg_action",
                "trpg_choice",
                "trpg_rule_state",
                "trpg_commit",
            ):
                self._conn.execute(f"DELETE FROM {table} WHERE instance_id=?", (instance_id,))
            self._conn.execute(
                "DELETE FROM memory_citation WHERE timeline_id IN (SELECT id FROM timeline WHERE instance_id=?)",
                (instance_id,),
            )
            self._conn.execute("DELETE FROM commit_log WHERE instance_id=?", (instance_id,))
            self._conn.execute("DELETE FROM timeline WHERE instance_id=?", (instance_id,))
            self._conn.execute("DELETE FROM instance WHERE id=?", (instance_id,))

    def timeline_list(self, instance_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM timeline WHERE instance_id=? ORDER BY created_at", (instance_id,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def instance_sessions(self, instance_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM session WHERE instance_id=? ORDER BY created_at", (instance_id,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def instance_messages(self, instance_id: str) -> list[dict[str, Any]]:
        """导出用的完整对话：不含投递回执、去重作废记录与通道凭据（§7.1）。"""
        rows = self._conn.execute(
            """SELECT m.* FROM message m JOIN session s ON s.id = m.session_id
               WHERE s.instance_id=? ORDER BY m.seq""",
            (instance_id,),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for raw in rows:
            row = _row_to_dict(raw)
            # 出站正文在 parts 里，text 为空——导出必须给正文，否则副本读不到她说过的话
            out.append(
                {
                    "session_id": row["session_id"],
                    "role": row["role"],
                    "text": self.message_text(row),
                    "state": row["state"],
                    "binding_version": row.get("binding_version"),
                    "created_at": row.get("created_at"),
                    "message_id": row.get("message_id"),
                }
            )
        return out

    def instance_import_messages(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        """导入的对话行：**重新签发消息标识**（导入=新实例，本地标识一律重映射，§7.3）。"""
        with self._lock, self._conn:
            for item in messages:
                self._conn.execute(
                    """INSERT INTO message(session_id, role, channel_id, thread_id, env_id, binding_version,
                                            binding_token, text, state, message_id, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        session_id,
                        str(item.get("role") or "character"),
                        None,
                        None,
                        None,
                        0,
                        None,
                        str(item.get("text") or ""),
                        str(item.get("state") or "fixed"),
                        f"m-{secrets.token_hex(6)}",
                        float(item.get("created_at") or 0.0),
                    ),
                )

    def timeline_update(self, timeline_id: str, *, name: str | None = None, description: str | None = None) -> None:
        """可命名、可描述（§四 列表只含管理元数据）。"""
        fields: list[str] = []
        args: list[Any] = []
        if name is not None:
            fields.append("name=?")
            args.append(str(name))
        if description is not None:
            fields.append("description=?")
            args.append(str(description))
        if not fields:
            return
        args.append(timeline_id)
        with self._lock, self._conn:
            self._conn.execute(f"UPDATE timeline SET {', '.join(fields)} WHERE id=?", args)

    def timeline_delete(self, instance_id: str, timeline_id: str) -> dict[str, int]:
        """删除一条线：清运行状态、对话与任务；其他线引用的提交数据保留（§四）。"""
        protected = {
            str(row["source_commit"])
            for row in self.timeline_list(instance_id)
            if row["id"] != timeline_id and row.get("source_commit")
        }
        doomed = [row["id"] for row in self.commit_list(instance_id, timeline_id) if row["id"] not in protected]
        counts: dict[str, int] = {}
        with self._lock, self._conn:
            self.timeline_clear_state(timeline_id)
            self.timeline_clear_dialog(timeline_id)
            for table in ("session", "character_join", "rate_command", "budget_reserve",
                          "commit_auto_state", "timeline_clock"):
                cur = self._conn.execute(f"DELETE FROM {table} WHERE timeline_id=?", (timeline_id,))
                counts[table] = int(cur.rowcount or 0)
            if doomed:
                # 删之前先把「以它们为基」的 diff 物化成全量：别的线引用的祖先不能因为删除动作变不可读（§8）
                self.unbase_dependents(doomed)
                marks = ",".join("?" for _ in doomed)
                cur = self._conn.execute(
                    f"DELETE FROM commit_snapshot WHERE commit_id IN ({marks})", tuple(doomed)
                )
                counts["commit_snapshot"] = int(cur.rowcount or 0)
                cur = self._conn.execute(f"DELETE FROM commit_log WHERE id IN ({marks})", tuple(doomed))
                counts["commit_log"] = int(cur.rowcount or 0)
            counts["commit_kept"] = len(protected & {row["id"] for row in self.commit_list(instance_id)})
            cur = self._conn.execute("DELETE FROM timeline WHERE id=?", (timeline_id,))
            counts["timeline"] = int(cur.rowcount or 0)
        return counts

    def timeline_set_state(self, timeline_id: str, state: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE timeline SET state=? WHERE id=?", (state, timeline_id))

    def commit_add(self, row: dict[str, Any], *, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        """记一条提交；给快照就一并落盘（一致快照，§5.1）。"""
        payload = self._snapshot_payload_for(row, snapshot) if snapshot is not None else None
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO commit_log(id, instance_id, timeline_id, kind, moment, note, created_at)
                   VALUES(:id, :instance_id, :timeline_id, :kind, :moment, :note, :created_at)""",
                row,
            )
            if payload is not None:
                self._conn.execute(
                    """INSERT INTO commit_snapshot(commit_id, instance_id, payload, size, created_at)
                       VALUES(?,?,?,?,?)""",
                    (row["id"], row["instance_id"], payload, len(payload), time.time()),
                )
        if payload is not None:
            self.commit_snapshot_compress(str(row["id"]))  # §8：链太长就把这条物化成全量
        return row

    def _snapshot_payload_for(self, row: dict[str, Any], snapshot: dict[str, Any]) -> str:
        """这条提交的存储形态（§6）：接得上上一条就存 diff，接不上（或没有上一条）就存全量。

        全量与 diff 是等价的存储方式，读的时候统一物化，调用方看不到差别。
        """
        from .runtime import versioning as versioning_mod

        previous = self._conn.execute(
            """SELECT id FROM commit_log WHERE instance_id=? AND timeline_id=?
               ORDER BY created_at DESC, id DESC LIMIT 1""",
            (row["instance_id"], row["timeline_id"]),
        ).fetchone()
        base = self.commit_snapshot_get(str(previous["id"])) if previous is not None else None
        if base is None:
            return versioning_mod.dump_snapshot(snapshot)
        delta = versioning_mod.encode_delta(base, snapshot)
        return versioning_mod.dump_snapshot(
            delta, kind=versioning_mod.SNAPSHOT_DELTA, base=str(previous["id"])
        )

    def commit_snapshot_depth(self, commit_id: str, *, limit: int = 64) -> int:
        """从这条沿 base 往回数到全量的距离；链断了返回 -1。"""
        from .runtime import versioning as versioning_mod

        depth, current = 0, str(commit_id)
        while current and depth <= limit:
            row = self._conn.execute(
                "SELECT payload FROM commit_snapshot WHERE commit_id=?", (current,)
            ).fetchone()
            if row is None:
                return -1
            kind, base = versioning_mod.snapshot_kind(row["payload"])
            if kind != versioning_mod.SNAPSHOT_DELTA:
                return depth
            depth, current = depth + 1, base
        return -1

    def commit_snapshot_compress(self, commit_id: str) -> dict[str, Any]:
        """§8 压缩：链太长就把这条物化成全量，链从它重新开始。

        不改变可观察状态（物化结果逐字段相同），也不删任何祖先——别的线引用的祖先照旧可读。
        """
        from .runtime import versioning as versioning_mod

        row = self._conn.execute(
            "SELECT * FROM commit_snapshot WHERE commit_id=?", (commit_id,)
        ).fetchone()
        if row is None:
            return {"compressed": False, "reason": "没有该提交的快照"}
        kind, _base = versioning_mod.snapshot_kind(row["payload"])
        if kind != versioning_mod.SNAPSHOT_DELTA:
            return {"compressed": False, "depth": 0}
        depth = self.commit_snapshot_depth(commit_id)
        if depth < 0 or depth <= versioning_mod.MAX_DELTA_CHAIN:
            return {"compressed": False, "depth": depth}
        payload = self.commit_snapshot_get(commit_id)
        if payload is None:
            return {"compressed": False, "reason": "链不完整，物化失败"}
        dumped = versioning_mod.dump_snapshot(payload)
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE commit_snapshot SET payload=?, size=? WHERE commit_id=?",
                (dumped, len(dumped), commit_id),
            )
        return {"compressed": True, "depth": depth, "kind": versioning_mod.SNAPSHOT_FULL}

    def unbase_dependents(self, commit_ids: list[str]) -> int:
        """删快照前先把「以它为基」的 diff 物化成全量：祖先可读性不能被删除动作带走（§8）。"""
        from .runtime import versioning as versioning_mod

        moved = 0
        for cid in [str(item) for item in commit_ids if str(item)]:
            rows = self._conn.execute(
                "SELECT commit_id, payload FROM commit_snapshot WHERE payload LIKE ?",
                (f'%"base": "{cid}"%',),
            ).fetchall()
            for row in rows:
                kind, base = versioning_mod.snapshot_kind(row["payload"])
                if kind != versioning_mod.SNAPSHOT_DELTA or base != cid:
                    continue
                payload = self.commit_snapshot_get(str(row["commit_id"]))
                if payload is None:
                    continue
                dumped = versioning_mod.dump_snapshot(payload)
                with self._lock, self._conn:
                    self._conn.execute(
                        "UPDATE commit_snapshot SET payload=?, size=? WHERE commit_id=?",
                        (dumped, len(dumped), str(row["commit_id"])),
                    )
                moved += 1
        return moved

    # ---------- 多角色披露（§七） ----------

    def message_text(self, row: dict[str, Any]) -> str:
        """消息正文：入站看 text，出站的正文在 parts（分批 JSON）里。"""
        text = str(row.get("text") or "").strip()
        if text:
            return text
        from .session import _flatten  # 同一套展开逻辑，避免两处各写一遍

        return _flatten(row.get("parts"))

    def message_by_ref(self, instance_id: str, timeline_id: str, ref: str) -> dict[str, Any] | None:
        """按 message_id 或 env_id 取一条已固化消息（披露范围选择用）。"""
        row = self._conn.execute(
            """SELECT * FROM message WHERE (message_id=? OR env_id=?)
               AND session_id IN (SELECT id FROM session WHERE instance_id=? AND timeline_id=?)
               ORDER BY seq LIMIT 1""",
            (ref, ref, instance_id, timeline_id),
        ).fetchone()
        return _row_to_dict(row) if row is not None else None

    def disclosure_add(self, row: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO disclosure(id, instance_id, timeline_id, from_character, to_character,
                                          scope, granted_world, granted_real, note, state)
                   VALUES(:id, :instance_id, :timeline_id, :from_character, :to_character,
                          :scope, :granted_world, :granted_real, :note, :state)""",
                row,
            )

    def disclosure_list(
        self, instance_id: str, timeline_id: str, *, to_character: str | None = None, until: int | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM disclosure WHERE instance_id=? AND timeline_id=? AND state='granted'"
        args: list[Any] = [instance_id, timeline_id]
        if to_character:
            sql += " AND to_character=?"
            args.append(to_character)
        if until is not None:
            sql += " AND granted_world<=?"
            args.append(int(until))
        rows = self._conn.execute(sql + " ORDER BY granted_world, id", args).fetchall()
        return [_row_to_dict(r) for r in rows]

    def disclosure_get(self, disclosure_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM disclosure WHERE id=?", (disclosure_id,)).fetchone()
        return _row_to_dict(row) if row is not None else None

    # ---------- 用户引入事件（§八） ----------

    def draft_put(self, row: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO event_draft(id, instance_id, source_timeline, source_commit, payload,
                                           state, timeline_id, created_world, created_at)
                   VALUES(:id, :instance_id, :source_timeline, :source_commit, :payload,
                          :state, :timeline_id, :created_world, :created_at)
                   ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, state=excluded.state,
                     timeline_id=excluded.timeline_id""",
                row,
            )

    def draft_get(self, draft_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM event_draft WHERE id=?", (draft_id,)).fetchone()
        return _row_to_dict(row) if row is not None else None

    def pending_event_add(self, row: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO pending_event(id, instance_id, timeline_id, at_world, payload, state,
                                             note, created_world, created_at)
                   VALUES(:id, :instance_id, :timeline_id, :at_world, :payload, :state,
                          :note, :created_world, :created_at)
                   ON CONFLICT(id) DO NOTHING""",
                row,
            )

    def pending_events_due(self, instance_id: str, timeline_id: str, *, until: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT * FROM pending_event WHERE instance_id=? AND timeline_id=? AND state='pending'
               AND at_world<=? ORDER BY at_world, id""",
            (instance_id, timeline_id, int(until)),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def pending_event_set(self, event_id: str, *, state: str, note: str = "") -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE pending_event SET state=?, note=? WHERE id=? AND state='pending'",
                (state, note, event_id),
            )
        return bool(cur.rowcount)

    def commit_state_get(self, timeline_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT * FROM commit_auto_state WHERE timeline_id=?", (timeline_id,)
        ).fetchone()
        return _row_to_dict(row) if row is not None else {
            "timeline_id": timeline_id, "last_commit_at": 0.0, "last_commit_moment": 0
        }

    def commit_state_set(
        self, timeline_id: str, instance_id: str, *, last_commit_at: float, last_commit_moment: int
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO commit_auto_state(timeline_id, instance_id, last_commit_at, last_commit_moment)
                   VALUES(?,?,?,?)
                   ON CONFLICT(timeline_id) DO UPDATE SET
                     last_commit_at=excluded.last_commit_at,
                     last_commit_moment=excluded.last_commit_moment""",
                (timeline_id, instance_id, float(last_commit_at), int(last_commit_moment)),
            )

    def event_count_since(self, instance_id: str, timeline_id: str, *, since: int) -> int:
        row = self._conn.execute(
            """SELECT COUNT(*) n FROM event WHERE instance_id=? AND timeline_id=? AND world_seconds>?""",
            (instance_id, timeline_id, int(since)),
        ).fetchone()
        return int(row["n"] if row is not None else 0)

    def commit_snapshot_get(self, commit_id: str) -> dict[str, Any] | None:
        from .runtime import versioning as versioning_mod

        row = self._conn.execute("SELECT * FROM commit_snapshot WHERE commit_id=?", (commit_id,)).fetchone()
        if row is None:
            return None
        kind, base = versioning_mod.snapshot_kind(row["payload"])
        if kind != versioning_mod.SNAPSHOT_DELTA:
            data = versioning_mod.parse_snapshot(row["payload"])
            body = data.get("body")
            return body if isinstance(body, dict) else data  # 兼容早期的裸全量
        # 沿 base 收集差异，到全量处自顶向下合成（§6：祖先基线与差异的状态物化）
        chain: list[dict[str, Any]] = []
        current, depth = str(commit_id), 0
        while current and depth <= 64:
            step = self._conn.execute(
                "SELECT payload FROM commit_snapshot WHERE commit_id=?", (current,)
            ).fetchone()
            if step is None:
                return None
            step_kind, next_base = versioning_mod.snapshot_kind(step["payload"])
            data = versioning_mod.parse_snapshot(step["payload"])
            if step_kind != versioning_mod.SNAPSHOT_DELTA:
                body = data.get("body")
                out = body if isinstance(body, dict) else data
                for delta in reversed(chain):
                    out = versioning_mod.apply_delta(out, delta)
                return out
            chain.append(data.get("body") or {})
            current, depth = next_base, depth + 1
        return None

    def commit_snapshot_put(self, commit_id: str, instance_id: str, payload: str) -> None:
        """随件恢复提交快照（§7.1 提交闭包）：导入后该线仍能回滚 / 分叉。"""
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT OR REPLACE INTO commit_snapshot(commit_id, instance_id, payload, size, created_at)
                   VALUES(?,?,?,?,?)""",
                (str(commit_id), str(instance_id), payload, len(payload), time.time()),
            )

    def commit_get(self, commit_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM commit_log WHERE id=?", (commit_id,)).fetchone()
        return _row_to_dict(row) if row is not None else None

    def timeline_add(self, row: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO timeline(id, instance_id, name, state, source_commit, created_at)
                   VALUES(:id, :instance_id, :name, :state, :source_commit, :created_at)""",
                row,
            )

    def timeline_clear_state(self, timeline_id: str) -> None:
        """回滚 / 分叉前清空该线的运行状态（保留线身份与时钟）。"""
        for table in (
            "unit", "life_plan", "experience", "claim", "knowledge", "effect_state", "intent",
            "event", "environment_state", "institution_state", "custom_state",
            "proactive_log",   # 回滚撤销还没投出去的主动消息与素材消费（§5.3 末条）
            "session_notice",  # 回滚撤销通告资格
            # 注意：`first_contact` **不在这里**——「初见已完成」记号属控制状态，不随回滚倒退（§5.6）
            # 注意：`character_join` **不在这里**——成员资格记录要留着并转为 revoked（§3.7 末条：
            # 回滚后再次补入必须用新的加入版本，不能复活被撤销的旧记录），撤销由回滚流程显式执行。
            "memory", "memory_task", "memory_citation",
            "rate_command",     # 历史里的待生效倍率不是现时控制命令（§七）
            "pending_event",    # 回滚撤销待执行状态及其后果（§八 末条）
            "disclosure",       # 回滚同时撤销授权与依赖它的派生（§7.2）
            "reaction",         # 短期反应随线版本化：回滚撤销派生状态（§11.1 / 附录B#17）
            "narrative_unit",   # 叙事单元 / 暂缓标记 / 审计结果同样是派生状态（NARRATIVE_LAYER §7.2）
            # 战役运行时：场景 / 行动 / 选择是派生编排状态，随回滚撤销；战役与规则状态由
            # 快照回写给出「提交那一刻」的值（TRPG_CAMPAIGN_RUNTIME_SPEC §十七）
            "trpg_scene",
            "trpg_action",
            "trpg_choice",
            "trpg_campaign",
            "trpg_rule_state",
            "trpg_commit",
        ):
            self._conn.execute(f"DELETE FROM {table} WHERE timeline_id=?", (timeline_id,))
        # 向量表两种形态都清：按线（新）与按 memory 归属（兼容早期没有 timeline_id 的行）
        self._conn.execute("DELETE FROM memory_embedding WHERE timeline_id=?", (timeline_id,))
        self._conn.execute(
            """DELETE FROM memory_embedding WHERE memory_id IN
               (SELECT id FROM memory WHERE timeline_id=?)""",
            (timeline_id,),
        )

    def timeline_void_inflight(self, timeline_id: str, *, reason: str = "rollback") -> int:
        """把该线还在处理中的输入登记作废：迟到的生成结果不得写回（§七）。

        走既有的 voided 表——重绑与回滚共用同一套「这条输入已经不算数」的判定。
        """
        rows = self._conn.execute(
            """SELECT channel_id, thread_id, env_id FROM message
               WHERE role='user' AND env_id IS NOT NULL AND state IN ('queued','processing')
                 AND session_id IN (SELECT id FROM session WHERE timeline_id=?)""",
            (timeline_id,),
        ).fetchall()
        for row in rows:
            self.void_put(str(row["channel_id"] or ""), str(row["thread_id"] or ""), str(row["env_id"]), reason)
        return len(rows)

    def timeline_delivered_replies(self, timeline_id: str) -> int:
        """该线已经投递出去（外部平台确认收到）的回复条数：回滚抹不掉它们（§7.2）。

        判据是**投递批次全部 accepted**：出站行的 `state` 固化后就是 `fixed`（不会被投递改写），
        拿它当「已送达」的条件会让这个计数永远是 0——那句「外部已显示的内容不保证撤回」
        也就永远发不出来。
        """
        row = self._conn.execute(
            """SELECT COUNT(*) n FROM message
               WHERE role='character' AND state NOT IN ('cancelled')
                 AND session_id IN (SELECT id FROM session WHERE timeline_id=?)
                 AND seq IN (SELECT msg_seq FROM delivery GROUP BY msg_seq
                             HAVING COUNT(*) > 0
                                AND SUM(CASE WHEN state='accepted' THEN 0 ELSE 1 END) = 0)""",
            (timeline_id,),
        ).fetchone()
        return int(row["n"] if row is not None else 0)

    def timeline_cancel_undelivered(self, timeline_id: str) -> int:
        """该线已固化但还没投递出去的回复一并取消：不继续发送被回滚的内容（§七）。"""
        with self._lock, self._conn:
            cur = self._conn.execute(
                """UPDATE message SET state='cancelled'
                   WHERE role='character' AND state='fixed'  -- 已固化但还没投递完成
                     AND session_id IN (SELECT id FROM session WHERE timeline_id=?)""",
                (timeline_id,),
            )
            cancelled = int(cur.rowcount or 0)
            self._conn.execute(
                """UPDATE delivery SET state='cancelled'
                   WHERE msg_seq IN (SELECT seq FROM message WHERE session_id IN
                                     (SELECT id FROM session WHERE timeline_id=?))""",
                (timeline_id,),
            )
        return cancelled

    def timeline_clear_dialog(self, timeline_id: str) -> None:
        """该线会话的对话原文（回滚要把被截去的未来对话一并撤掉，§七）。"""
        self._conn.execute(
            """DELETE FROM message WHERE session_id IN (SELECT id FROM session WHERE timeline_id=?)""",
            (timeline_id,),
        )

    def commit_list(self, instance_id: str, timeline_id: str | None = None) -> list[dict[str, Any]]:
        if timeline_id is None:
            rows = self._conn.execute(
                "SELECT * FROM commit_log WHERE instance_id=? ORDER BY created_at", (instance_id,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM commit_log WHERE instance_id=? AND timeline_id=? ORDER BY created_at",
                (instance_id, timeline_id),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ---------- 运行层：时钟 / 倍率 / 水位 ----------

    def clock_get(self, timeline_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM timeline_clock WHERE timeline_id=?", (timeline_id,)).fetchone()
        return _row_to_dict(row) if row else None

    def clock_put(self, row: dict[str, Any]) -> None:
        payload = {"catching_up": 0, "limited": 0, **row}
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO timeline_clock(timeline_id, base_real, base_world, rate, high_water_real,
                                              anchor_real, processed_world, generation, catching_up, limited)
                   VALUES(:timeline_id, :base_real, :base_world, :rate, :high_water_real,
                          :anchor_real, :processed_world, :generation, :catching_up, :limited)
                   ON CONFLICT(timeline_id) DO UPDATE SET
                     base_real=:base_real, base_world=:base_world, rate=:rate,
                     high_water_real=:high_water_real, anchor_real=:anchor_real,
                     processed_world=:processed_world, generation=:generation,
                     catching_up=:catching_up, limited=:limited""",
                payload,
            )

    def apply_runtime_batch(
        self,
        *,
        timeline_id: str,
        generation: int,
        processed_world: int,
        catching_up: bool,
        limited: bool = False,
        plans: Iterable[dict[str, Any]] = (),
        units: Iterable[dict[str, Any]] = (),
        experiences: Iterable[dict[str, Any]] = (),
        events: Iterable[dict[str, Any]] = (),
        claims: Iterable[dict[str, Any]] = (),
        knowledge: Iterable[dict[str, Any]] = (),
        effects: Iterable[dict[str, Any]] = (),
        intents: Iterable[dict[str, Any]] = (),
        environment: Iterable[dict[str, Any]] = (),
        institution: Iterable[dict[str, Any]] = (),
        customs: Iterable[dict[str, Any]] = (),
        clear_effects: Iterable[Any] = (),
        reactions: Iterable[dict[str, Any]] = (),
        trpg: dict[str, Any] | None = None,
        clock_shift_seconds: int = 0,
    ) -> bool:
        """把一批事实转移整体提交（§2.7：一批失败即整批回到批前水位）。

        返回 False 表示世代已变（冻结 / 回滚后的迟到任务），整批不落盘。
        """
        # 同刻顺序在入库前定死（§六）：同一批里同刻的多事件 / 多效果，谁的顺序不取决于谁先被迭代到。
        events = _same_instant_order(list(events), at_key="world_seconds")
        effects = _same_instant_order(list(effects), at_key="from_world")
        with self._lock, self._conn:  # 单次 with → 一次提交，中途异常整批回滚
            row = self._conn.execute(
                "SELECT generation, processed_world FROM timeline_clock WHERE timeline_id=?", (timeline_id,)
            ).fetchone()
            if row is None or int(row["generation"]) != int(generation):
                return False
            if int(row["processed_world"]) > int(processed_world):
                return False  # 水位只前进：并发的另一批已经先写过
            if clock_shift_seconds:
                # 场景内时间消耗（§十四）改的是**锚点**不是水位：世界时间往前跳 N 秒，
                # 之后由 advance 按正常批次结算这段。跟本批同一个事务、同一个世代。
                self._conn.execute(
                    "UPDATE timeline_clock SET base_world = base_world + ? WHERE timeline_id=?",
                    (int(clock_shift_seconds), timeline_id),
                )
            for plan in plans:
                self._conn.execute(
                    """INSERT OR IGNORE INTO life_plan(id, instance_id, timeline_id, character_id, day_index,
                                                       windows, state, created_world, note)
                       VALUES(:id, :instance_id, :timeline_id, :character_id, :day_index,
                              :windows, :state, :created_world, :note)""",
                    plan,
                )
            for unit in units:
                self._conn.execute(
                    """INSERT INTO unit(instance_id, timeline_id, character_id, id, mode, semantic, basis,
                                        confidence, stability, archived, consumed, updated_world)
                       VALUES(:instance_id, :timeline_id, :character_id, :id, :mode, :semantic, :basis,
                              :confidence, :stability, :archived, :consumed, :updated_world)
                       ON CONFLICT(instance_id, timeline_id, character_id, id) DO UPDATE SET
                         confidence=:confidence, stability=:stability, archived=:archived,
                         consumed=:consumed, updated_world=:updated_world""",
                    unit,
                )
            for item in experiences:
                self._conn.execute(
                    """INSERT OR IGNORE INTO experience(id, instance_id, timeline_id, character_id, world_seconds,
                                                        kind, summary, source_ref, confidence)
                       VALUES(:id, :instance_id, :timeline_id, :character_id, :world_seconds,
                              :kind, :summary, :source_ref, :confidence)""",
                    item,
                )
            for row in events:
                self._conn.execute(
                    """INSERT INTO event(instance_id, timeline_id, id, world_seconds, seq, kind, family,
                                        template, source, summary, detail, text_source, effects,
                                        share_value, importance, created_real)
                       VALUES(:instance_id, :timeline_id, :id, :world_seconds, :seq, :kind, :family,
                              :template, :source, :summary, :detail, :text_source, :effects,
                              :share_value, :importance, :created_real)
                       ON CONFLICT(instance_id, timeline_id, id) DO NOTHING""",
                    _event_payload(row),
                )
            for row in claims:
                self._conn.execute(
                    """INSERT INTO claim(instance_id, timeline_id, id, event_id, source_id, text,
                                        audience, earliest_world, credibility)
                       VALUES(:instance_id, :timeline_id, :id, :event_id, :source_id, :text,
                              :audience, :earliest_world, :credibility)
                       ON CONFLICT(instance_id, timeline_id, id) DO NOTHING""",
                    row,
                )
            for row in knowledge:
                self._conn.execute(
                    """INSERT INTO knowledge(instance_id, timeline_id, character_id, id, world_seconds,
                                            kind, target, source, stance, text)
                       VALUES(:instance_id, :timeline_id, :character_id, :id, :world_seconds,
                              :kind, :target, :source, :stance, :text)
                       ON CONFLICT(instance_id, timeline_id, character_id, id) DO NOTHING""",
                    row,
                )
            from .runtime import reaction as reaction_mod  # 延迟导入：存储层不反向依赖运行层

            cleared_ids = {
                str(item.get("id") if isinstance(item, dict) else item) for item in (clear_effects or ())
            }
            for raw in reactions:
                incoming = {"tendency": "", "basis": "", "expiry_condition": "", "updated_world": 0, **raw}
                existing = self._conn.execute(
                    """SELECT * FROM reaction WHERE instance_id=? AND timeline_id=? AND character_id=? AND id=?""",
                    (
                        incoming["instance_id"],
                        incoming["timeline_id"],
                        incoming["character_id"],
                        incoming["id"],
                    ),
                ).fetchone()
                merged = reaction_mod.merge(
                    _row_to_dict(existing) if existing else None, incoming
                )
                self._conn.execute(
                    """INSERT INTO reaction(instance_id, timeline_id, character_id, id, source_kind, source_ref,
                                            direction, intensity, stage, tendency, basis, started_world,
                                            expiry_condition, updated_world)
                       VALUES(:instance_id, :timeline_id, :character_id, :id, :source_kind, :source_ref,
                              :direction, :intensity, :stage, :tendency, :basis, :started_world,
                              :expiry_condition, :updated_world)
                       ON CONFLICT(instance_id, timeline_id, character_id, id) DO UPDATE SET
                         direction=:direction, intensity=:intensity, stage=:stage, tendency=:tendency,
                         basis=:basis, expiry_condition=:expiry_condition, updated_world=:updated_world""",
                    merged,
                )
            # 阶段推进：纯函数，按新水位与解除集合重算该线所有反应（行数少，直接全算）
            for raw in self._conn.execute(
                "SELECT * FROM reaction WHERE timeline_id=?", (timeline_id,)
            ).fetchall():
                current = _row_to_dict(raw)
                advanced = reaction_mod.advance(
                    current, watermark=int(processed_world), cleared=cleared_ids
                )
                if advanced != current:
                    self._conn.execute(
                        """UPDATE reaction SET stage=?, intensity=?, updated_world=?
                           WHERE instance_id=? AND timeline_id=? AND character_id=? AND id=?""",
                        (
                            advanced["stage"],
                            advanced["intensity"],
                            int(advanced.get("updated_world") or advanced.get("started_world") or 0),
                            current["instance_id"],
                            current["timeline_id"],
                            current["character_id"],
                            current["id"],
                        ),
                    )
            for raw in effects:
                row = {
                    "value": None, "family": "", "recovery": "", "cleared_at": None, "seq": 0, **raw,
                }
                row["priority"] = _row_priority(row)  # 没声明的按固定档位补上，读回也看得见
                self._conn.execute(
                    """INSERT INTO effect_state(instance_id, timeline_id, id, event_id, target, kind, family,
                                               value, from_world, expiry, recovery, active, cleared_at,
                                               priority, seq)
                       VALUES(:instance_id, :timeline_id, :id, :event_id, :target, :kind, :family,
                              :value, :from_world, :expiry, :recovery, :active, :cleared_at,
                              :priority, :seq)
                       ON CONFLICT(instance_id, timeline_id, id) DO NOTHING""",
                    row,
                )
            for row in intents:
                self._conn.execute(
                    """INSERT INTO intent(instance_id, timeline_id, character_id, id, object, basis, strength,
                                          window_from, window_to, preconditions, effect, stage, note,
                                          source_world, updated_world)
                       VALUES(:instance_id, :timeline_id, :character_id, :id, :object, :basis, :strength,
                              :window_from, :window_to, :preconditions, :effect, :stage, :note,
                              :source_world, :updated_world)
                       ON CONFLICT(instance_id, timeline_id, character_id, id) DO UPDATE SET
                         object=:object, basis=:basis, strength=:strength, window_from=:window_from,
                         window_to=:window_to, preconditions=:preconditions, effect=:effect,
                         stage=:stage, note=:note, updated_world=:updated_world""",
                    row,
                )
            for row in environment:
                self._conn.execute(
                    """INSERT INTO environment_state(instance_id, timeline_id, type_id, value, unit, source,
                                                    from_world, expiry, updated_world)
                       VALUES(:instance_id, :timeline_id, :type_id, :value, :unit, :source,
                              :from_world, :expiry, :updated_world)
                       ON CONFLICT(instance_id, timeline_id, type_id) DO UPDATE SET
                         value=:value, unit=:unit, source=:source, from_world=:from_world,
                         expiry=:expiry, updated_world=:updated_world""",
                    row,
                )
            for item in institution:
                row = _office_payload(item)
                self._conn.execute(
                    """INSERT INTO institution_state(instance_id, timeline_id, office_id, institution_id,
                                                    institution_name, name, holder, continues_json,
                                                    suspended_json, source, from_world, updated_world)
                       VALUES(:instance_id, :timeline_id, :office_id, :institution_id, :institution_name,
                              :name, :holder, :continues_json, :suspended_json, :source, :from_world,
                              :updated_world)
                       ON CONFLICT(instance_id, timeline_id, office_id) DO UPDATE SET
                         holder=:holder, source=:source, from_world=:from_world,
                         updated_world=:updated_world""",
                    row,
                )
            for item in customs:
                row = _custom_payload(item)
                self._conn.execute(
                    """INSERT INTO custom_state(instance_id, timeline_id, custom_id, name, applies_to, form,
                                               forms_json, basis, source, from_world, updated_world)
                       VALUES(:instance_id, :timeline_id, :custom_id, :name, :applies_to, :form,
                              :forms_json, :basis, :source, :from_world, :updated_world)
                       ON CONFLICT(instance_id, timeline_id, custom_id) DO UPDATE SET
                         form=:form, source=:source, from_world=:from_world, updated_world=:updated_world""",
                    row,
                )
            for item in clear_effects:
                # 解除时刻由调用方给（条件首次成立的世界时刻）；缺省才退回批边界——
                # 退回会让「同一区间换一种分批」记出不同的 cleared_at（§2.6 分批与在线等价）
                if isinstance(item, tuple):
                    effect_id = item[0]
                    instance_id_ = item[1] if len(item) > 1 else None
                    cleared_at = int(item[2]) if len(item) > 2 and item[2] is not None else int(processed_world)
                else:
                    effect_id, instance_id_, cleared_at = item, None, int(processed_world)
                self._conn.execute(
                    """UPDATE effect_state SET active=0, cleared_at=?
                       WHERE id=? AND timeline_id=? AND active=1 AND (? IS NULL OR instance_id=?)""",
                    (cleared_at, effect_id, timeline_id, instance_id_, instance_id_),
                )
            self._conn.execute(
                """UPDATE timeline_clock SET processed_world=?, catching_up=?, limited=?
                   WHERE timeline_id=? AND generation=?""",
                (processed_world, 1 if catching_up else 0, 1 if limited else 0, timeline_id, generation),
            )
            # 战役运行时的同批写入：规则状态 patch、场景 / 行动 / 选择与联合提交凭据
            # 必须在**这一个事务**里落地，否则「规则扣了资源、世界没变」这种半条状态会出现。
            self._trpg_apply(self._conn, trpg)
            return True

    # ---------- TRPG 战役运行时（TRPG_CAMPAIGN_RUNTIME_SPEC §十） ----------

    def trpg_upserts(self, rows: dict[str, Any] | None) -> None:
        """把一组战役运行时行写进库（自成一次事务；批提交走 apply_runtime_batch 的同名参数）。"""
        if not rows:
            return
        with self._lock, self._conn:
            self._trpg_apply(self._conn, rows)

    def _trpg_apply(self, conn: sqlite3.Connection, rows: dict[str, Any]) -> None:
        """按 kind 逐行 upsert（供批提交在**同一事务内**调用，别自己开事务）。"""
        for kind, items in (rows or {}).items():
            if kind not in TRPG_COLUMNS:
                raise ValueError(f"未知的战役运行时表：{kind}")
            for item in items or []:
                conn.execute(_trpg_sql(kind), _trpg_row(kind, item))

    def trpg_get(self, kind: str, **keys: Any) -> dict[str, Any] | None:
        where, params = _trpg_where(kind, keys)
        row = self._conn.execute(f"SELECT * FROM trpg_{kind} WHERE {where}", params).fetchone()
        return _row_to_dict(row) if row is not None else None

    def trpg_list(self, kind: str, **keys: Any) -> list[dict[str, Any]]:
        where, params = _trpg_where(kind, keys)
        rows = self._conn.execute(
            f"SELECT * FROM trpg_{kind} WHERE {where} ORDER BY rowid", params
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def trpg_commit_by_key(self, instance_id: str, timeline_id: str, idempotency_key: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            """SELECT * FROM trpg_commit WHERE instance_id=? AND timeline_id=? AND idempotency_key=?""",
            (instance_id, timeline_id, str(idempotency_key)),
        ).fetchone()
        return _row_to_dict(row) if row is not None else None

    def trpg_delete(self, kind: str, **keys: Any) -> int:
        where, params = _trpg_where(kind, keys)
        with self._lock, self._conn:
            cur = self._conn.execute(f"DELETE FROM trpg_{kind} WHERE {where}", params)
            return int(cur.rowcount or 0)

    def runtime_dump(self, instance_id: str, timeline_id: str, *, watermark: int) -> dict[str, Any]:
        def rows(sql: str, *args: Any) -> list[dict[str, Any]]:
            return [_row_to_dict(r) for r in self._conn.execute(sql, args).fetchall()]

        return {
            "watermark": int(watermark),
            "characters": rows(
                "SELECT * FROM character_join WHERE instance_id=? AND timeline_id=? ORDER BY joined_world",
                instance_id,
                timeline_id,
            ),
            "units": rows(
                """SELECT * FROM unit WHERE instance_id=? AND timeline_id=? AND updated_world<=?
                   ORDER BY character_id, id""",
                instance_id,
                timeline_id,
                watermark,
            ),
            "plans": rows(
                """SELECT * FROM life_plan WHERE instance_id=? AND timeline_id=? AND created_world<=?
                   ORDER BY character_id, day_index""",
                instance_id,
                timeline_id,
                watermark,
            ),
            "memories": rows(
                """SELECT * FROM memory WHERE instance_id=? AND timeline_id=? AND semantic_watermark<=?
                   ORDER BY character_id, learned_world""",
                instance_id,
                timeline_id,
                watermark,
            ),
            "memory_tasks": rows(
                """SELECT * FROM memory_task WHERE instance_id=? AND timeline_id=? AND source_world<=?
                   ORDER BY character_id, source_world""",
                instance_id,
                timeline_id,
                watermark,
            ),
            "citations": rows(
                """SELECT * FROM memory_citation WHERE timeline_id=? AND world_seconds<=?
                   ORDER BY turn_id, memory_id""",
                timeline_id,
                watermark,
            ),
            "experiences": rows(
                """SELECT * FROM experience WHERE instance_id=? AND timeline_id=? AND world_seconds<=?
                   ORDER BY character_id, world_seconds""",
                instance_id,
                timeline_id,
                watermark,
            ),
            "events": rows(
                """SELECT * FROM event WHERE instance_id=? AND timeline_id=? AND world_seconds<=?
                   ORDER BY world_seconds, seq""",
                instance_id,
                timeline_id,
                watermark,
            ),
            "claims": rows(
                """SELECT * FROM claim WHERE instance_id=? AND timeline_id=? AND earliest_world<=?
                   ORDER BY event_id, id""",
                instance_id,
                timeline_id,
                watermark,
            ),
            "knowledge": rows(
                """SELECT * FROM knowledge WHERE instance_id=? AND timeline_id=? AND world_seconds<=?
                   ORDER BY character_id, world_seconds""",
                instance_id,
                timeline_id,
                watermark,
            ),
            "reactions": rows(
                """SELECT * FROM reaction WHERE instance_id=? AND timeline_id=? AND started_world<=?
                   ORDER BY started_world, id""",
                instance_id,
                timeline_id,
                watermark,
            ),
            "narrative": rows(
                """SELECT * FROM narrative_unit WHERE instance_id=? AND timeline_id=? AND created_world<=?
                   ORDER BY created_world, id""",
                instance_id,
                timeline_id,
                watermark,
            ),
            "effects": rows(
                """SELECT * FROM effect_state WHERE instance_id=? AND timeline_id=? AND from_world<=?
                   ORDER BY from_world, seq, id""",
                instance_id,
                timeline_id,
                watermark,
            ),
            "environment": rows(
                """SELECT * FROM environment_state WHERE instance_id=? AND timeline_id=? AND updated_world<=?
                   ORDER BY type_id""",
                instance_id,
                timeline_id,
                watermark,
            ),
            "disclosure": rows(
                """SELECT * FROM disclosure WHERE instance_id=? AND timeline_id=? AND granted_world<=?
                   ORDER BY granted_world, id""",
                instance_id,
                timeline_id,
                watermark,
            ),
            "institution": rows(
                """SELECT * FROM institution_state WHERE instance_id=? AND timeline_id=? AND updated_world<=?
                   ORDER BY office_id""",
                instance_id,
                timeline_id,
                watermark,
            ),
            "customs": rows(
                """SELECT * FROM custom_state WHERE instance_id=? AND timeline_id=? AND updated_world<=?
                   ORDER BY custom_id""",
                instance_id,
                timeline_id,
                watermark,
            ),
            "intents": rows(
                """SELECT * FROM intent WHERE instance_id=? AND timeline_id=? AND source_world<=?
                   ORDER BY character_id, id""",
                instance_id,
                timeline_id,
                watermark,
            ),
            # 战役运行时（TRPG_CAMPAIGN_RUNTIME_SPEC §十三）：按水位截断，回滚 / 分叉 / 导出共用
            "trpg_campaigns": rows(
                """SELECT * FROM trpg_campaign WHERE instance_id=? AND timeline_id=? AND created_world<=?
                   ORDER BY campaign_id""",
                instance_id,
                timeline_id,
                watermark,
            ),
            "trpg_scenes": rows(
                """SELECT * FROM trpg_scene WHERE instance_id=? AND timeline_id=? AND created_world<=?
                   ORDER BY scene_id""",
                instance_id,
                timeline_id,
                watermark,
            ),
            "trpg_actions": rows(
                """SELECT * FROM trpg_action WHERE instance_id=? AND timeline_id=? AND created_world<=?
                   ORDER BY action_id""",
                instance_id,
                timeline_id,
                watermark,
            ),
            "trpg_choices": rows(
                """SELECT * FROM trpg_choice WHERE instance_id=? AND timeline_id=? AND created_world<=?
                   ORDER BY choice_id""",
                instance_id,
                timeline_id,
                watermark,
            ),
            "trpg_rule_states": rows(
                """SELECT * FROM trpg_rule_state WHERE instance_id=? AND timeline_id=? AND created_world<=?
                   ORDER BY ruleset_id""",
                instance_id,
                timeline_id,
                watermark,
            ),
            "trpg_commits": rows(
                """SELECT * FROM trpg_commit WHERE instance_id=? AND timeline_id=? AND created_world<=?
                   ORDER BY created_real""",
                instance_id,
                timeline_id,
                watermark,
            ),
        }

    def runtime_load(
        self, instance_id: str, timeline_id: str, payload: dict[str, Any], *, clear: bool = False
    ) -> int:
        """导入运行层快照：整体一次提交，返回写入的行数。

        `clear=True` 时在同一事务里先撤掉该线现有状态与对话——回滚用它做原子切换（§七）。
        """
        payload = _relabel_payload(payload, instance_id, timeline_id)
        plans = [
            {**row, "created_world": int(row.get("created_world", payload.get("watermark", 0)))}
            for row in payload.get("plans") or []
        ]
        with self._lock, self._conn:
            if clear:
                self.timeline_clear_state(timeline_id)
                self.timeline_clear_dialog(timeline_id)
            for row in payload.get("characters") or []:
                row = {
                    "commit_id": "", "state": "active", "request_id": "", "revoked_world": None,
                    **{k: v for k, v in row.items() if k in (
                        "instance_id", "timeline_id", "character_id", "joined_world", "card", "note",
                        "acquainted", "created_real", "commit_id", "state", "request_id", "revoked_world",
                    )},
                }
                self._conn.execute(
                    """INSERT OR REPLACE INTO character_join(instance_id, timeline_id, character_id, joined_world,
                                                             card, note, acquainted, created_real,
                                                             commit_id, state, request_id, revoked_world)
                       VALUES(:instance_id, :timeline_id, :character_id, :joined_world,
                              :card, :note, :acquainted, :created_real,
                              :commit_id, :state, :request_id, :revoked_world)""",
                    row,
                )
            for row in payload.get("units") or []:
                self._conn.execute(
                    """INSERT INTO unit(instance_id, timeline_id, character_id, id, mode, semantic, basis,
                                        confidence, stability, archived, consumed, updated_world)
                       VALUES(:instance_id, :timeline_id, :character_id, :id, :mode, :semantic, :basis,
                              :confidence, :stability, :archived, :consumed, :updated_world)
                       ON CONFLICT(instance_id, timeline_id, character_id, id) DO NOTHING""",
                    row,
                )
            for plan in plans:
                self._conn.execute(
                    """INSERT OR IGNORE INTO life_plan(id, instance_id, timeline_id, character_id, day_index,
                                                       windows, state, created_world, note)
                       VALUES(:id, :instance_id, :timeline_id, :character_id, :day_index,
                              :windows, :state, :created_world, :note)""",
                    plan,
                )
            for row in payload.get("events") or []:
                self._conn.execute(
                    """INSERT INTO event(instance_id, timeline_id, id, world_seconds, seq, kind, family,
                                        template, source, summary, detail, text_source, effects,
                                        share_value, importance, created_real)
                       VALUES(:instance_id, :timeline_id, :id, :world_seconds, :seq, :kind, :family,
                              :template, :source, :summary, :detail, :text_source, :effects,
                              :share_value, :importance, :created_real)
                       ON CONFLICT(instance_id, timeline_id, id) DO NOTHING""",
                    _event_payload(row),
                )
            for row in payload.get("claims") or []:
                self._conn.execute(
                    """INSERT INTO claim(instance_id, timeline_id, id, event_id, source_id, text,
                                        audience, earliest_world, credibility)
                       VALUES(:instance_id, :timeline_id, :id, :event_id, :source_id, :text,
                              :audience, :earliest_world, :credibility)
                       ON CONFLICT(instance_id, timeline_id, id) DO NOTHING""",
                    row,
                )
            for row in payload.get("knowledge") or []:
                self._conn.execute(
                    """INSERT INTO knowledge(instance_id, timeline_id, character_id, id, world_seconds,
                                            kind, target, source, stance, text)
                       VALUES(:instance_id, :timeline_id, :character_id, :id, :world_seconds,
                              :kind, :target, :source, :stance, :text)
                       ON CONFLICT(instance_id, timeline_id, character_id, id) DO NOTHING""",
                    row,
                )
            for row in payload.get("reactions") or []:
                payload_row = {"tendency": "", "basis": "", "expiry_condition": "", "updated_world": 0, **row}
                self._conn.execute(
                    """INSERT INTO reaction(instance_id, timeline_id, character_id, id, source_kind, source_ref,
                                            direction, intensity, stage, tendency, basis, started_world,
                                            expiry_condition, updated_world)
                       VALUES(:instance_id, :timeline_id, :character_id, :id, :source_kind, :source_ref,
                              :direction, :intensity, :stage, :tendency, :basis, :started_world,
                              :expiry_condition, :updated_world)
                       ON CONFLICT(instance_id, timeline_id, character_id, id) DO NOTHING""",
                    payload_row,
                )
            for row in payload.get("effects") or []:
                self._conn.execute(
                    """INSERT INTO effect_state(instance_id, timeline_id, id, event_id, target, kind, family,
                                               value, from_world, expiry, recovery, active, cleared_at)
                       VALUES(:instance_id, :timeline_id, :id, :event_id, :target, :kind, :family,
                              :value, :from_world, :expiry, :recovery, :active, :cleared_at)
                       ON CONFLICT(instance_id, timeline_id, id) DO NOTHING""",
                    row,
                )
            for row in payload.get("environment") or []:
                self._conn.execute(
                    """INSERT INTO environment_state(instance_id, timeline_id, type_id, value, unit, source,
                                                    from_world, expiry, updated_world)
                       VALUES(:instance_id, :timeline_id, :type_id, :value, :unit, :source,
                              :from_world, :expiry, :updated_world)
                       ON CONFLICT(instance_id, timeline_id, type_id) DO NOTHING""",
                    row,
                )
            for row in payload.get("dialog") or []:
                self._conn.execute(
                    """INSERT INTO message(session_id, role, channel_id, thread_id, env_id, binding_version,
                                          binding_token, text, state, message_id, created_at)
                       VALUES(:session_id, :role, NULL, NULL, NULL, 0, NULL, :text, :state,
                              :message_id, :created_at)
                       ON CONFLICT DO NOTHING""",
                    {
                        "session_id": str(row.get("session_id") or ""),
                        "role": str(row.get("role") or "character"),
                        "text": str(row.get("text") or ""),
                        "state": str(row.get("state") or "fixed"),
                        "message_id": row.get("message_id"),
                        "created_at": float(row.get("created_at") or 0.0),
                    },
                )
            for row in payload.get("institution") or []:
                self._conn.execute(
                    """INSERT INTO institution_state(instance_id, timeline_id, office_id, institution_id,
                                                    institution_name, name, holder, continues_json,
                                                    suspended_json, source, from_world, updated_world)
                       VALUES(:instance_id, :timeline_id, :office_id, :institution_id, :institution_name,
                              :name, :holder, :continues_json, :suspended_json, :source, :from_world,
                              :updated_world)
                       ON CONFLICT(instance_id, timeline_id, office_id) DO NOTHING""",
                    row,
                )
            # 战役运行时：装载路径与写入共用同一份列定义（store.TRPG_COLUMNS），
            # 少一处就会出现「导出有、导入后没」——本仓库踩过四次的老账。
            self._trpg_apply(
                self._conn,
                {
                    "campaign": payload.get("trpg_campaigns") or [],
                    "scene": payload.get("trpg_scenes") or [],
                    "action": payload.get("trpg_actions") or [],
                    "choice": payload.get("trpg_choices") or [],
                    "rule_state": payload.get("trpg_rule_states") or [],
                    "commit": payload.get("trpg_commits") or [],
                },
            )
            for row in payload.get("customs") or []:
                self._conn.execute(
                    """INSERT INTO custom_state(instance_id, timeline_id, custom_id, name, applies_to, form,
                                               forms_json, basis, source, from_world, updated_world)
                       VALUES(:instance_id, :timeline_id, :custom_id, :name, :applies_to, :form,
                              :forms_json, :basis, :source, :from_world, :updated_world)
                       ON CONFLICT(instance_id, timeline_id, custom_id) DO NOTHING""",
                    row,
                )
            for row in payload.get("disclosure") or []:
                self._conn.execute(
                    """INSERT INTO disclosure(instance_id, timeline_id, id, from_character, to_character,
                                             scope, granted_world, granted_real, note, state)
                       VALUES(:instance_id, :timeline_id, :id, :from_character, :to_character,
                              :scope, :granted_world, :granted_real, :note, :state)
                       ON CONFLICT DO NOTHING""",
                    row,
                )
            for row in payload.get("memories") or []:
                self._conn.execute(
                    """INSERT INTO memory(id, instance_id, timeline_id, character_id, text, kind, sources,
                                          happened_world, learned_world, recorded_world, semantic_watermark,
                                          strength, confidence, state, version, supersedes, superseded_by,
                                          source_key, decay_world)
                       VALUES(:id, :instance_id, :timeline_id, :character_id, :text, :kind, :sources,
                              :happened_world, :learned_world, :recorded_world, :semantic_watermark,
                              :strength, :confidence, :state, :version, :supersedes, :superseded_by,
                              :source_key, :decay_world)
                       ON CONFLICT(instance_id, timeline_id, id) DO NOTHING""",
                    row,
                )
            for row in payload.get("memory_tasks") or []:
                self._conn.execute(
                    """INSERT INTO memory_task(id, instance_id, timeline_id, character_id, source_kind,
                                               source_ref, source_world, created_world, text, state, attempts, note)
                       VALUES(:id, :instance_id, :timeline_id, :character_id, :source_kind, :source_ref,
                              :source_world, :created_world, :text, :state, :attempts, :note)
                       ON CONFLICT(instance_id, timeline_id, id) DO NOTHING""",
                    row,
                )
            for row in payload.get("citations") or []:
                # 引用记录随件（MEMORY_SPEC 验收 8）：主键带线，导入副本与源线不会互相吞
                self._conn.execute(
                    """INSERT OR IGNORE INTO memory_citation(turn_id, memory_id, timeline_id, character_id,
                                                             world_seconds, created_at)
                       VALUES(:turn_id, :memory_id, :timeline_id, :character_id, :world_seconds, :created_at)""",
                    row,
                )
            for row in payload.get("intents") or []:
                self._conn.execute(
                    """INSERT INTO intent(instance_id, timeline_id, character_id, id, object, basis, strength,
                                          window_from, window_to, preconditions, effect, stage, note,
                                          source_world, updated_world)
                       VALUES(:instance_id, :timeline_id, :character_id, :id, :object, :basis, :strength,
                              :window_from, :window_to, :preconditions, :effect, :stage, :note,
                              :source_world, :updated_world)
                       ON CONFLICT(instance_id, timeline_id, character_id, id) DO NOTHING""",
                    row,
                )
            for item in payload.get("experiences") or []:
                self._conn.execute(
                    """INSERT OR IGNORE INTO experience(id, instance_id, timeline_id, character_id, world_seconds,
                                                        kind, summary, source_ref, confidence)
                       VALUES(:id, :instance_id, :timeline_id, :character_id, :world_seconds,
                              :kind, :summary, :source_ref, :confidence)""",
                    item,
                )
            for item in payload.get("narrative") or []:
                # 对照 dump 的键一一对应：少一处就是「导出有、导入后没」（§7.2）
                row = {"refs": "[]", "audit": "[]", "note": "", "message_id": "", "world_day": 0, **item}
                self._conn.execute(
                    """INSERT OR REPLACE INTO narrative_unit(instance_id, timeline_id, character_id, id,
                                                             primary_ref, refs, entry, relation, topic, stage,
                                                             message_id, world_day, audit, note,
                                                             created_world, updated_world)
                       VALUES(:instance_id, :timeline_id, :character_id, :id,
                              :primary_ref, :refs, :entry, :relation, :topic, :stage,
                              :message_id, :world_day, :audit, :note,
                              :created_world, :updated_world)""",
                    row,
                )
        return sum(
            len(payload.get(key) or [])
            for key in (
                "characters",
                "units",
                "plans",
                "experiences",
                "events",
                "claims",
                "knowledge",
                "effects",
                "intents",
                "environment",
                "citations",
                "narrative",
            )
        )

    # ---------- 环境事实状态 ----------

    def environment_put(self, row: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO environment_state(instance_id, timeline_id, type_id, value, unit, source,
                                                 from_world, expiry, updated_world)
                   VALUES(:instance_id, :timeline_id, :type_id, :value, :unit, :source,
                          :from_world, :expiry, :updated_world)
                   ON CONFLICT(instance_id, timeline_id, type_id) DO UPDATE SET
                     value=:value, unit=:unit, source=:source, from_world=:from_world,
                     expiry=:expiry, updated_world=:updated_world""",
                row,
            )

    def environment_list(self, instance_id: str, timeline_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM environment_state WHERE instance_id=? AND timeline_id=? ORDER BY type_id",
            (instance_id, timeline_id),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def institution_put(self, row: dict[str, Any]) -> None:
        payload = _office_payload(row)
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO institution_state(instance_id, timeline_id, office_id, institution_id,
                                                institution_name, name, holder, continues_json,
                                                suspended_json, source, from_world, updated_world)
                   VALUES(:instance_id, :timeline_id, :office_id, :institution_id, :institution_name,
                          :name, :holder, :continues_json, :suspended_json, :source, :from_world,
                          :updated_world)
                   ON CONFLICT(instance_id, timeline_id, office_id) DO UPDATE SET
                     holder=:holder, source=:source, from_world=:from_world,
                     updated_world=:updated_world""",
                payload,
            )

    def institution_list(self, instance_id: str, timeline_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM institution_state WHERE instance_id=? AND timeline_id=? ORDER BY office_id",
            (instance_id, timeline_id),
        ).fetchall()
        return [_institution_row(r) for r in rows]

    def custom_put(self, row: dict[str, Any]) -> None:
        payload = _custom_payload(row)
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO custom_state(instance_id, timeline_id, custom_id, name, applies_to, form,
                                           forms_json, basis, source, from_world, updated_world)
                   VALUES(:instance_id, :timeline_id, :custom_id, :name, :applies_to, :form,
                          :forms_json, :basis, :source, :from_world, :updated_world)
                   ON CONFLICT(instance_id, timeline_id, custom_id) DO UPDATE SET
                     form=:form, source=:source, from_world=:from_world, updated_world=:updated_world""",
                payload,
            )

    def custom_list(self, instance_id: str, timeline_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM custom_state WHERE instance_id=? AND timeline_id=? ORDER BY custom_id",
            (instance_id, timeline_id),
        ).fetchall()
        return [_custom_row(r) for r in rows]

    # ---------- 表述与展开所需的存储 ----------

    def event_get(self, instance_id: str, timeline_id: str, event_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM event WHERE instance_id=? AND timeline_id=? AND id=?",
            (instance_id, timeline_id, event_id),
        ).fetchone()
        return _row_to_dict(row) if row else None

    def event_render_save(
        self, instance_id: str, timeline_id: str, event_id: str, *, detail: str, claims: dict[str, str]
    ) -> None:
        """固化表述（§3.2）：写后不重生成，读取 / 重启 / 换消费者都用它。"""
        with self._lock, self._conn:
            self._conn.execute(
                """UPDATE event SET detail=?, text_source='llm'
                   WHERE instance_id=? AND timeline_id=? AND id=?""",
                (detail, instance_id, timeline_id, event_id),
            )
            for source_id, text in claims.items():
                self._conn.execute(
                    """UPDATE claim SET text=? WHERE instance_id=? AND timeline_id=? AND event_id=?
                       AND source_id=? AND derived_from IS NULL""",
                    (text, instance_id, timeline_id, event_id, source_id),
                )

    def claim_derived(
        self, instance_id: str, timeline_id: str, original_id: str
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            """SELECT * FROM claim WHERE instance_id=? AND timeline_id=? AND derived_from=?
               ORDER BY id LIMIT 1""",
            (instance_id, timeline_id, original_id),
        ).fetchone()
        return _row_to_dict(row) if row else None

    def plugin_put(self, row: dict[str, Any]) -> None:
        payload = {"name": "", "version": "", "path": "", "enabled": 0, "state": "registered", "note": "",
                   "updated_at": 0.0, **row}
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO plugin(id, name, version, path, enabled, state, note, updated_at)
                   VALUES(:id, :name, :version, :path, :enabled, :state, :note, :updated_at)
                   ON CONFLICT(id) DO UPDATE SET
                     name=:name, version=:version, path=:path, enabled=:enabled,
                     state=:state, note=:note, updated_at=:updated_at""",
                {"enabled": int(payload["enabled"]), **payload},
            )

    def plugin_get(self, plugin_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM plugin WHERE id=?", (str(plugin_id),)).fetchone()
        return _row_to_dict(row) if row else None

    def plugin_list(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM plugin ORDER BY id").fetchall()
        return [_row_to_dict(row) for row in rows]

    def plugin_forget(self, plugin_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM plugin WHERE id=?", (str(plugin_id),))

    def notice_put(self, row: dict[str, Any]) -> dict[str, Any]:
        """登记一条通知引用（同一条固化消息只登记一次：唯一键 + 幂等返回既有行）。"""
        payload = {"revision": 0, "created_at": time.time(), **row}
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO notice(id, instance_id, timeline_id, session_id, message_id, revision, created_at)
                   VALUES(:id, :instance_id, :timeline_id, :session_id, :message_id, :revision, :created_at)
                   ON CONFLICT(instance_id, timeline_id, session_id, message_id) DO NOTHING""",
                payload,
            )
        existing = self.notice_get(str(payload["message_id"]))
        return existing or payload

    def notice_get(self, ident: str) -> dict[str, Any] | None:
        """按通知 id 或 message_id 取一条。"""
        row = self._conn.execute("SELECT * FROM notice WHERE id=?", (str(ident),)).fetchone()
        if row is None:
            row = self._conn.execute(
                "SELECT * FROM notice WHERE message_id=? ORDER BY created_at LIMIT 1", (str(ident),)
            ).fetchone()
        return _row_to_dict(row) if row else None

    def notice_list(self, instance_id: str | None = None) -> list[dict[str, Any]]:
        if instance_id:
            rows = self._conn.execute(
                "SELECT * FROM notice WHERE instance_id=? ORDER BY created_at, id", (instance_id,)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM notice ORDER BY created_at, id").fetchall()
        return [_row_to_dict(row) for row in rows]

    def notice_target(self, row: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """定位通知指向的会话（§2.5 末条）：只解析、不改投、不激活；失效时给出管理错误原因。"""
        issues: list[str] = []
        instance = self.instance_get(str(row.get("instance_id") or ""))
        if instance is None:
            issues.append("实例已删除")
        timeline = next(
            (item for item in self.timeline_list(str(row.get("instance_id") or "")) if str(item["id"]) == str(row.get("timeline_id"))),
            None,
        )
        if timeline is None:
            issues.append("时间线已删除")
        elif str(timeline.get("state")) == "archived":
            issues.append("时间线已归档")
        session = self.session_get(str(row.get("session_id") or ""))
        if session is None:
            issues.append("会话已删除（回滚或重建）")
        elif str(session.get("instance_id")) != str(row.get("instance_id")) or str(
            session.get("timeline_id")
        ) != str(row.get("timeline_id")):
            issues.append("会话已重绑到别的时间线")
        message = self._conn.execute(
            "SELECT seq, state, role FROM message WHERE session_id=? AND message_id=? ORDER BY seq LIMIT 1",
            (str(row.get("session_id") or ""), str(row.get("message_id") or "")),
        ).fetchone()
        if message is None:
            issues.append("消息已不存在（回滚或作废）")
        target = {
            "instance_id": str(row.get("instance_id") or ""),
            "timeline_id": str(row.get("timeline_id") or ""),
            "session_id": str(row.get("session_id") or ""),
            "message_id": str(row.get("message_id") or ""),
            "revision": int(row.get("revision") or 0),
            "timeline_state": None if timeline is None else str(timeline.get("state") or ""),
            "message_seq": None if message is None else int(message["seq"]),
            "message_state": None if message is None else str(message["state"]),
            "valid": not issues,
            "reason": "；".join(issues),
        }
        return target, issues

    def reaction_list(
        self, instance_id: str, timeline_id: str, *, character_id: str | None = None
    ) -> list[dict[str, Any]]:
        """该线的短期反应（§11.1）；按角色取时只给该角色。"""
        if character_id:
            rows = self._conn.execute(
                """SELECT * FROM reaction WHERE instance_id=? AND timeline_id=? AND character_id=?
                   ORDER BY started_world, id""",
                (instance_id, timeline_id, str(character_id)),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT * FROM reaction WHERE instance_id=? AND timeline_id=?
                   ORDER BY character_id, started_world, id""",
                (instance_id, timeline_id),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def claim_coverage_put(self, row: dict[str, Any]) -> None:
        payload = {"derived_id": "", "note": "", "updated_world": 0, **row}
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO claim_coverage(instance_id, timeline_id, claim_id, state, derived_id,
                                              note, updated_world)
                   VALUES(:instance_id, :timeline_id, :claim_id, :state, :derived_id, :note, :updated_world)
                   ON CONFLICT(instance_id, timeline_id, claim_id) DO UPDATE SET
                     state=:state, derived_id=:derived_id, note=:note, updated_world=:updated_world""",
                payload,
            )

    def claim_coverage_get(
        self, instance_id: str, timeline_id: str, claim_id: str
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            """SELECT * FROM claim_coverage WHERE instance_id=? AND timeline_id=? AND claim_id=?""",
            (instance_id, timeline_id, claim_id),
        ).fetchone()
        return _row_to_dict(row) if row else None

    def claim_put(self, row: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO claim(instance_id, timeline_id, id, event_id, source_id, text,
                                     audience, earliest_world, credibility, derived_from)
                   VALUES(:instance_id, :timeline_id, :id, :event_id, :source_id, :text,
                          :audience, :earliest_world, :credibility, :derived_from)
                   ON CONFLICT(instance_id, timeline_id, id) DO NOTHING""",
                row,
            )

    def knowledge_put(self, row: dict[str, Any]) -> None:
        """单条获知（用于运行层之外的补记与验收）。"""
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO knowledge(instance_id, timeline_id, character_id, id, world_seconds,
                                         kind, target, source, stance, text)
                   VALUES(:instance_id, :timeline_id, :character_id, :id, :world_seconds,
                          :kind, :target, :source, :stance, :text)
                   ON CONFLICT(instance_id, timeline_id, character_id, id) DO NOTHING""",
                {
                    "stance": str(row.get("stance") or "recorded"),
                    "source": str(row.get("source") or ""),
                    "text": str(row.get("text") or ""),
                    **{key: row.get(key) for key in
                       ("instance_id", "timeline_id", "character_id", "id", "world_seconds", "kind", "target")},
                },
            )

    def knowledge_holders(self, instance_id: str, timeline_id: str, target: str) -> list[str]:
        """已经掌握该记载的角色（展开只能返回他们原本掌握范围内的内容，§3.4）。"""
        rows = self._conn.execute(
            """SELECT DISTINCT character_id FROM knowledge
               WHERE instance_id=? AND timeline_id=? AND target=?""",
            (instance_id, timeline_id, target),
        ).fetchall()
        return [str(row["character_id"]) for row in rows]

    # ---------- 角色记忆（MEMORY_SPEC） ----------

    def memory_scope(
        self, instance_id: str, timeline_id: str, character_id: str, *, until: int | None = None
    ) -> list[dict[str, Any]]:
        """可召回集合（§5.1 第一步）：实例 + 线 + 角色 + 查询水位；来源状态与归档不豁免隔离。"""
        sql = """SELECT * FROM memory WHERE instance_id=? AND timeline_id=? AND character_id=?
                 AND learned_world<=? ORDER BY learned_world, id"""
        rows = self._conn.execute(
            sql, (instance_id, timeline_id, character_id, int(until) if until is not None else 10**15)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def memory_add(self, row: dict[str, Any]) -> dict[str, Any] | None:
        """写入一条规范记忆：同源幂等 + 同事实去重（同角色同线内）+ 相反说法建替代关系。"""
        from .runtime import memory as memory_mod

        character_id = str(row["character_id"])
        source_key = str(row.get("source_key") or "")
        if source_key:
            dup = self._conn.execute(
                "SELECT * FROM memory WHERE instance_id=? AND timeline_id=? AND character_id=? AND source_key=?",
                (row["instance_id"], row["timeline_id"], character_id, source_key),
            ).fetchone()
            if dup is not None:
                return None  # 同源重复提取：不重复写、不重复强化
        siblings = [
            item
            for item in self.memory_scope(str(row["instance_id"]), str(row["timeline_id"]), character_id)
            if str(item["kind"]) == str(row["kind"])
        ]
        # 矛盾链的新旧只看**获知时刻**：记录水位会被迟到提取带成「最新」，把已固化的纠正顶掉（验收 14）
        def knowledge_world(item: dict[str, Any]) -> int:
            return int(
                item.get("learned_world") or item.get("happened_world")
                or item.get("semantic_watermark") or item.get("recorded_world") or 0
            )

        incoming_world = knowledge_world(row)
        supersedes = None
        best_world = -1
        newest_contradiction = None
        for item in siblings:
            # 先判相反：矛盾里也常有相同词，反着判顺序会把纠正当成重复吞掉
            if not memory_mod.contradicts(str(item["text"]), str(row["text"])):
                if memory_mod.same_fact(str(item["text"]), str(row["text"])):
                    return None  # 同事实：合并来源即可，不新增条目
                continue
            item_world = knowledge_world(item)
            if newest_contradiction is None or item_world > knowledge_world(newest_contradiction):
                newest_contradiction = item
            if item_world <= incoming_world and (supersedes is None or item_world > best_world):
                supersedes = str(item["id"])
                best_world = item_world
        late = False
        if newest_contradiction is not None and supersedes is None:
            # 本行比已有的纠正还旧：新条目作为「当时认知」入档，由既有条目取代
            late = True
            supersedes = str(newest_contradiction["id"])
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO memory(id, instance_id, timeline_id, character_id, text, kind, sources,
                                      happened_world, learned_world, recorded_world, semantic_watermark,
                                      strength, confidence, state, version, supersedes, superseded_by,
                                      source_key, decay_world)
                   VALUES(:id,:instance_id,:timeline_id,:character_id,:text,:kind,:sources,
                          :happened_world,:learned_world,:recorded_world,:semantic_watermark,
                          :strength,:confidence,:state,1,:supersedes,NULL,:source_key,:decay_world)""",
                {
                    "sources": json.dumps(row.get("sources") or [], ensure_ascii=False),
                    "state": "archived" if late else memory_mod.state_for(float(row.get("strength") or 0.6)),
                    "supersedes": supersedes if not late else None,
                    # 衰减从记录水位起算；缺省用记录时刻（不是 0——0 会让条目一推进就归零）
                    "decay_world": int(row.get("decay_world") or row.get("recorded_world") or 0),
                    **{key: row.get(key) for key in (
                        "id", "instance_id", "timeline_id", "character_id", "text", "kind",
                        "happened_world", "learned_world", "recorded_world", "semantic_watermark",
                        "strength", "confidence", "source_key",
                    )},
                },
            )
            if late:
                # 迟到提取：本行只是「当时认知」，记「被既有纠正取代」入档；既有纠正保持有效
                self._conn.execute(
                    "UPDATE memory SET superseded_by=? WHERE instance_id=? AND timeline_id=? AND id=?",
                    (supersedes, row.get("instance_id"), row.get("timeline_id"), str(row.get("id"))),
                )
            elif supersedes:
                # 明确纠正：新条目替代旧的，旧条目保留（历史可查当时认知）
                self._conn.execute(
                    "UPDATE memory SET superseded_by=?, state='archived' WHERE id=?", (row["id"], supersedes)
                )
        saved = self.memory_get(str(row["id"]))
        return saved

    def memory_get(
        self, memory_id: str, *, instance_id: str = "", timeline_id: str = ""
    ) -> dict[str, Any] | None:
        if instance_id and timeline_id:
            row = self._conn.execute(
                "SELECT * FROM memory WHERE instance_id=? AND timeline_id=? AND id=?",
                (instance_id, timeline_id, memory_id),
            ).fetchone()
        else:
            row = self._conn.execute("SELECT * FROM memory WHERE id=?", (memory_id,)).fetchone()
        return _row_to_dict(row) if row is not None else None

    def memory_update_strength(
        self,
        memory_id: str,
        strength: float,
        *,
        state: str | None = None,
        instance_id: str = "",
        timeline_id: str = "",
    ) -> None:
        where = "id=?" if not (instance_id and timeline_id) else "instance_id=? AND timeline_id=? AND id=?"
        args: list[Any] = [float(strength)]
        if state is not None:
            args.append(state)
        args.extend([memory_id] if not (instance_id and timeline_id) else [instance_id, timeline_id, memory_id])
        columns = "strength=?" if state is None else "strength=?, state=?"
        with self._lock, self._conn:
            self._conn.execute(f"UPDATE memory SET {columns} WHERE {where}", args)

    def memory_task_add(self, row: dict[str, Any], *, pending_cap: int = MEMORY_PENDING_CAP) -> bool:
        """登记一条待提取来源（同角色同来源幂等，§4.1）。

        队列**有界**（§4.1「有界重试队列」）：某角色待处理数超过 `pending_cap` 时，
        同一次事务里淘汰最旧的低价值项——淘汰顺序 经历 < 说法 < 打算 < 转述 < 对话，
        即流水账先走、对话与转述最后走。被淘汰的标 `dropped` 并写明原因，不是静默删除。
        """
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO memory_task(id, instance_id, timeline_id, character_id, source_kind,
                                                     source_ref, source_world, created_world, text,
                                                     state, attempts, note)
                   VALUES(?,?,?,?,?,?,?,?,?, 'pending', 0, '')""",
                (
                    row["id"], row["instance_id"], row["timeline_id"], row["character_id"],
                    row["source_kind"], row["source_ref"], int(row["source_world"]), int(row["created_world"]),
                    str(row.get("text") or ""),
                ),
            )
            if cur.rowcount and int(pending_cap) > 0:
                pending = int(
                    self._conn.execute(
                        """SELECT COUNT(*) FROM memory_task
                           WHERE instance_id=? AND timeline_id=? AND character_id=? AND state='pending'""",
                        (row["instance_id"], row["timeline_id"], row["character_id"]),
                    ).fetchone()[0]
                )
                over = pending - int(pending_cap)
                if over > 0:
                    self._conn.execute(
                        """UPDATE memory_task SET state='dropped', note='队列有界淘汰（§4.1）'
                           WHERE id IN (
                             SELECT id FROM memory_task
                             WHERE instance_id=? AND timeline_id=? AND character_id=? AND state='pending'
                             ORDER BY CASE source_kind
                                        WHEN 'experience' THEN 0 WHEN 'claim' THEN 1
                                        WHEN 'intent' THEN 2 WHEN 'disclosed' THEN 3
                                        WHEN 'dialog' THEN 4
                                        ELSE 5 END ASC, source_world ASC, id ASC
                             LIMIT ?)""",
                        (row["instance_id"], row["timeline_id"], row["character_id"], int(over)),
                    )
        return bool(cur.rowcount)

    def memory_tasks(self, instance_id: str, timeline_id: str, *, state: str = "pending") -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT * FROM memory_task WHERE instance_id=? AND timeline_id=? AND state=?
               ORDER BY source_world, id""",
            (instance_id, timeline_id, state),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def memory_task_set(self, task_id: str, *, state: str, note: str = "") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE memory_task SET state=?, note=?, attempts=attempts+1 WHERE id=?", (state, note, task_id)
            )

    def memory_cite(
        self,
        turn_id: str,
        memory_id: str,
        *,
        instance_id: str = "",
        timeline_id: str,
        character_id: str,
        world_seconds: int,
    ) -> bool:
        """实际被采纳一轮用到的条目才强化：同一轮幂等（§5.3）。"""
        from .runtime import memory as memory_mod

        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO memory_citation(turn_id, memory_id, timeline_id, character_id,
                                                         world_seconds, created_at)
                   VALUES(?,?,?,?,?,?)""",
                (turn_id, memory_id, timeline_id, character_id, int(world_seconds), time.time()),
            )
            if not cur.rowcount:
                return False
            if instance_id:
                row = self._conn.execute(
                    "SELECT strength FROM memory WHERE instance_id=? AND timeline_id=? AND id=?",
                    (instance_id, timeline_id, memory_id),
                ).fetchone()
            else:
                row = self._conn.execute("SELECT strength FROM memory WHERE id=?", (memory_id,)).fetchone()
            if row is None:
                return False
            strength = memory_mod.reinforce(float(row["strength"]))
            self._conn.execute(
                "UPDATE memory SET strength=?, state=? WHERE instance_id=? AND timeline_id=? AND id=?",
                (strength, memory_mod.state_for(strength), instance_id or self._scope_instance(memory_id),
                 timeline_id, memory_id),
            )
        return True

    def timeline_get(self, timeline_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM timeline WHERE id=?", (timeline_id,)).fetchone()
        return _row_to_dict(row) if row is not None else None

    def memory_vector_scores(
        self,
        instance_id: str,
        timeline_id: str,
        character_id: str,
        query: str,
        *,
        query_vector: list[float] | None = None,
        model: str = "",
    ) -> dict[str, float]:
        """向量召回分数（§5.2）：没有查询向量 / 模型不符 / 维度不符时返回空——调用方退化全文召回。"""
        if not query_vector:
            return {}
        import math

        rows = self._conn.execute(
            """SELECT e.memory_id, e.vector, e.dim FROM memory_embedding e
               JOIN memory m ON m.id = e.memory_id
               WHERE m.instance_id=? AND m.timeline_id=? AND m.character_id=? AND e.dim=? AND e.model=?""",
            (instance_id, timeline_id, character_id, len(query_vector), str(model or "")),
        ).fetchall()
        scores: dict[str, float] = {}
        norm = math.sqrt(sum(value * value for value in query_vector)) or 1.0
        for row in rows:
            blob = row["vector"]
            dim = int(row["dim"])
            if len(blob) != dim * 4:
                continue
            values = struct.unpack(f"<{dim}f", blob)
            other = math.sqrt(sum(value * value for value in values)) or 1.0
            dot = sum(a * b for a, b in zip(query_vector, values))
            scores[str(row["memory_id"])] = dot / (norm * other)
        return scores

    def memory_decay(self, *, timeline_id: str, to_world: int, day_seconds: int, per_day: float) -> int:
        """按世界时长衰减（§六）：从每条的 decay_world 推到 to_world；同一水位重跑无副作用。"""
        from .runtime import memory as memory_mod

        rows = self._conn.execute(
            "SELECT id, strength, decay_world, state FROM memory WHERE timeline_id=? AND state<>'archived'",
            (timeline_id,),
        ).fetchall()
        changed = 0
        with self._lock, self._conn:
            for row in rows:
                start = int(row["decay_world"] or 0)
                if int(to_world) <= start:
                    continue
                strength = memory_mod.decayed_strength(
                    float(row["strength"]), from_world=start, to_world=int(to_world),
                    day_seconds=day_seconds, per_day=per_day,
                )
                self._conn.execute(
                    "UPDATE memory SET strength=?, state=?, decay_world=? WHERE id=?",
                    (strength, memory_mod.state_for(strength), int(to_world), row["id"]),
                )
                changed += 1
        return changed

    def _scope_instance(self, memory_id: str) -> str:
        row = self._conn.execute("SELECT instance_id FROM memory WHERE id=?", (memory_id,)).fetchone()
        return str(row["instance_id"]) if row is not None else ""

    def memory_embedding_put(
        self,
        memory_id: str,
        *,
        instance_id: str,
        timeline_id: str = "",
        model: str,
        vector: list[float],
        content_hash: str,
    ) -> None:
        """落一条向量（带模型指纹与源文本版本，§5.2）。"""
        from .runtime import embedding as embedding_mod

        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO memory_embedding(memory_id, instance_id, timeline_id, model, dim, vector,
                                               source_version, created_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(instance_id, timeline_id, memory_id) DO UPDATE SET
                     model=excluded.model, dim=excluded.dim, vector=excluded.vector,
                     source_version=excluded.source_version, created_at=excluded.created_at""",
                (
                    memory_id,
                    instance_id,
                    str(timeline_id or ""),
                    str(model),
                    len(vector),
                    embedding_mod.pack(vector),
                    str(content_hash),
                    time.time(),
                ),
            )

    def memory_missing_embeddings(
        self, instance_id: str, timeline_id: str, *, model: str, dim: int = 0, limit: int = 64
    ) -> list[dict[str, Any]]:
        """缺向量或指纹不符的条目（模型 / 维度变了就重建，旧向量不再参与召回）。"""
        rows = self._conn.execute(
            """SELECT m.id, m.text, m.character_id, e.model AS embed_model
               FROM memory m LEFT JOIN memory_embedding e ON e.memory_id = m.id
               WHERE m.instance_id=? AND m.timeline_id=?
                 AND (e.memory_id IS NULL OR e.model <> ? OR (? > 0 AND e.dim <> ?))
               ORDER BY m.learned_world, m.id LIMIT ?""",
            (instance_id, timeline_id, str(model), int(dim), int(dim), int(limit)),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def memory_supersede(self, *, old_id: str, new_id: str) -> None:
        """记「新版取代旧版」：旧条目归档留档、新版版本号 +1（§六 整理结果固化并版本化）。"""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE memory SET superseded_by=?, state='archived' WHERE id=?", (str(new_id), str(old_id))
            )
            self._conn.execute(
                "UPDATE memory SET version=COALESCE(version, 1) + 1, supersedes=? WHERE id=?",
                (str(old_id), str(new_id)),
            )

    def memory_supersede_moments(
        self, instance_id: str, timeline_id: str, memory_ids: Iterable[str]
    ) -> dict[str, int]:
        """被替代条目的替代时刻（= 替代行的获知时间）。历史水位召回用它判断「当时还是当前版本」（§4.2）。"""
        wanted = [str(item) for item in memory_ids]
        if not wanted:
            return {}
        marks = ",".join("?" * len(wanted))
        rows = self._conn.execute(
            f"""SELECT m.id AS id, s.learned_world AS mark FROM memory m
                JOIN memory s ON s.instance_id = m.instance_id AND s.timeline_id = m.timeline_id
                             AND s.id = m.superseded_by
                WHERE m.instance_id=? AND m.timeline_id=? AND m.id IN ({marks})""",
            (instance_id, timeline_id, *wanted),
        ).fetchall()
        return {str(row["id"]): int(row["mark"] or 0) for row in rows}

    def memory_embedding_peek(
        self, instance_id: str, timeline_id: str, *, model: str = ""
    ) -> dict[str, Any] | None:
        """取一条已嵌入的条目当维度探测样本（§5.2 同名换维度：不比对就没法发现变了）。"""
        row = self._conn.execute(
            """SELECT m.* FROM memory_embedding e JOIN memory m ON m.id = e.memory_id
               WHERE e.instance_id=? AND e.timeline_id=? AND (?='' OR e.model=?)
               ORDER BY e.created_at DESC, m.id LIMIT 1""",
            (instance_id, timeline_id, str(model or ""), str(model or "")),
        ).fetchone()
        return _row_to_dict(row) if row is not None else None

    def memory_count(self, instance_id: str, timeline_id: str, character_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) n FROM memory WHERE instance_id=? AND timeline_id=? AND character_id=?",
            (instance_id, timeline_id, character_id),
        ).fetchone()
        return int(row["n"] if row is not None else 0)

    # ---------- 三层预算（§2.8） ----------

    def budget_usage(self, instance_id: str, *, bucket: int, timeline_id: str | None = None) -> dict[str, Any]:
        """已消耗 + 在途预占（按实例 / 线 / 任务汇总），预占未结算前也算占用。"""
        cond, args = "instance_id=? AND bucket=?", [instance_id, int(bucket)]
        if timeline_id:
            cond += " AND timeline_id=?"
            args.append(timeline_id)
        usage: dict[str, Any] = {"instance": 0, "timelines": {}, "tasks": {}}
        for row in self._conn.execute(
            f"""SELECT timeline_id, task, SUM(tokens) calls_tokens FROM call_ledger
                WHERE {cond} GROUP BY timeline_id, task""",
            args,
        ):
            tokens = int(row["calls_tokens"] or 0)
            usage["instance"] += tokens
            usage["timelines"][row["timeline_id"]] = usage["timelines"].get(row["timeline_id"], 0) + tokens
            usage["tasks"][f"{row['timeline_id']}|{row['task']}"] = tokens
        for row in self._conn.execute(
            f"""SELECT timeline_id, task, SUM(tokens_est) held FROM budget_reserve
                WHERE {cond} AND state='held' GROUP BY timeline_id, task""",
            args,
        ):
            held = int(row["held"] or 0)
            usage["instance"] += held
            usage["timelines"][row["timeline_id"]] = usage["timelines"].get(row["timeline_id"], 0) + held
            key = f"{row['timeline_id']}|{row['task']}"
            usage["tasks"][key] = usage["tasks"].get(key, 0) + held
        return usage

    def budget_reserve(
        self,
        *,
        instance_id: str,
        timeline_id: str,
        task: str,
        bucket: int,
        priority: int,
        tokens_est: int,
        limits: dict[str, int],
        reserved_for_higher: dict[int, int] | None = None,
    ) -> dict[str, Any] | None:
        """发起前原子预占；三层任何一层不够就拒绝（返回 None）。"""
        from .runtime.budget import decide

        with self._lock, self._conn:
            usage = self.budget_usage(instance_id, bucket=bucket)
            verdict = decide(
                usage=usage,
                timeline_id=timeline_id,
                task=task,
                priority=priority,
                tokens_est=max(0, int(tokens_est)),
                limits=limits,
                reserved_for_higher=reserved_for_higher or {},
            )
            if not verdict["ok"]:
                return {"ok": False, **verdict}
            ident = f"rs-{os.urandom(6).hex()}"
            self._conn.execute(
                """INSERT INTO budget_reserve(id, instance_id, timeline_id, task, bucket, priority,
                                             tokens_est, tokens_actual, state, outcome_kind, created_at)
                   VALUES(?,?,?,?,?,?,?,0,'held','',?)""",
                (ident, instance_id, timeline_id, task, int(bucket), int(priority), int(tokens_est), time.time()),
            )
        return {"ok": True, "id": ident, **verdict}

    def budget_settle(
        self, reserve_id: str, *, tokens_actual: int = 0, outcome: str = "ok", calls: int = 1
    ) -> dict[str, Any] | None:
        """结算：登记真实消耗（成功 / 失败 / 超时都算），释放未使用的预占。"""
        with self._lock, self._conn:
            row = self._conn.execute("SELECT * FROM budget_reserve WHERE id=?", (reserve_id,)).fetchone()
            if row is None or row["state"] != "held":
                return None
            self._conn.execute(
                "UPDATE budget_reserve SET state='settled', outcome_kind=?, tokens_actual=? WHERE id=?",
                (str(outcome), max(0, int(tokens_actual)), reserve_id),
            )
            # 同一事务内登记消耗（不调 call_ledger_add，避免嵌套事务）
            self._conn.execute(
                """INSERT INTO call_ledger(instance_id, timeline_id, task, bucket, calls, tokens)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(instance_id, timeline_id, task, bucket) DO UPDATE SET
                     calls = calls + excluded.calls, tokens = tokens + excluded.tokens""",
                (
                    row["instance_id"],
                    row["timeline_id"],
                    row["task"],
                    int(row["bucket"]),
                    max(1, int(calls)),
                    max(0, int(tokens_actual)),
                ),
            )
        return {
            "id": reserve_id,
            "task": row["task"],
            "calls": max(1, int(calls)),
            "tokens_actual": int(tokens_actual),
            "outcome": outcome,
        }

    def budget_release(self, reserve_id: str) -> bool:
        """失效任务释放未使用的预占（已发生的外部调用不回滚）。"""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE budget_reserve SET state='released', outcome_kind='cancelled' WHERE id=? AND state='held'", (reserve_id,)
            )
        return bool(cur.rowcount)

    def budget_policy_get(self, instance_id: str) -> dict[str, Any]:
        row = self._conn.execute("SELECT * FROM budget_policy WHERE instance_id=?", (instance_id,)).fetchone()
        if row is None:
            return {
                "instance_id": instance_id,
                "paused_tasks": [],
                "instance_tokens_per_day": None,
                "timeline_tokens_per_day": None,
                "task_tokens_per_day": None,
            }
        data = _row_to_dict(row)
        data["paused_tasks"] = json.loads(data.get("paused_tasks") or "[]")
        return data

    def budget_policy_set(self, instance_id: str, **fields: Any) -> dict[str, Any]:
        current = self.budget_policy_get(instance_id)
        current.update(fields)
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO budget_policy(instance_id, paused_tasks, instance_tokens_per_day,
                                            timeline_tokens_per_day, task_tokens_per_day, updated_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(instance_id) DO UPDATE SET
                     paused_tasks=excluded.paused_tasks,
                     instance_tokens_per_day=excluded.instance_tokens_per_day,
                     timeline_tokens_per_day=excluded.timeline_tokens_per_day,
                     task_tokens_per_day=excluded.task_tokens_per_day,
                     updated_at=excluded.updated_at""",
                (
                    instance_id,
                    json.dumps(sorted(current.get("paused_tasks") or []), ensure_ascii=False),
                    current.get("instance_tokens_per_day"),
                    current.get("timeline_tokens_per_day"),
                    current.get("task_tokens_per_day"),
                    time.time(),
                ),
            )
        return self.budget_policy_get(instance_id)

    def budget_rows(self, instance_id: str, *, bucket: int) -> list[dict[str, Any]]:
        """非内容性的预算账目（来源 / 阶段 / 调用次数 / token 量级 / 结果类别，无正文）。"""
        rows = self._conn.execute(
            """SELECT timeline_id, task, bucket, calls, tokens FROM call_ledger
               WHERE instance_id=? AND bucket=? ORDER BY task""",
            (instance_id, int(bucket)),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def call_ledger_add(
        self, instance_id: str, timeline_id: str, task: str, *, bucket: int, calls: int = 1, tokens: int = 0
    ) -> int:
        """调用账本（§2.8）：按实例 / 线 / 任务 / 现实日窗口记次数与量级，不记正文。"""
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO call_ledger(instance_id, timeline_id, task, bucket, calls, tokens)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(instance_id, timeline_id, task, bucket) DO UPDATE SET
                     calls = calls + excluded.calls, tokens = tokens + excluded.tokens""",
                (instance_id, timeline_id, task, int(bucket), int(calls), int(tokens)),
            )
            row = self._conn.execute(
                """SELECT calls FROM call_ledger WHERE instance_id=? AND timeline_id=? AND task=? AND bucket=?""",
                (instance_id, timeline_id, task, int(bucket)),
            ).fetchone()
        return int(row["calls"]) if row else 0

    def call_ledger_get(self, instance_id: str, timeline_id: str, task: str, *, bucket: int) -> int:
        row = self._conn.execute(
            """SELECT calls FROM call_ledger WHERE instance_id=? AND timeline_id=? AND task=? AND bucket=?""",
            (instance_id, timeline_id, task, int(bucket)),
        ).fetchone()
        return int(row["calls"]) if row else 0

    # ---------- 角色打算 ----------

    def intent_put(self, row: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO intent(instance_id, timeline_id, character_id, id, object, basis, strength,
                                      window_from, window_to, preconditions, effect, stage, note,
                                      source_world, updated_world)
                   VALUES(:instance_id, :timeline_id, :character_id, :id, :object, :basis, :strength,
                          :window_from, :window_to, :preconditions, :effect, :stage, :note,
                          :source_world, :updated_world)
                   ON CONFLICT(instance_id, timeline_id, character_id, id) DO UPDATE SET
                     object=:object, basis=:basis, strength=:strength, window_from=:window_from,
                     window_to=:window_to, preconditions=:preconditions, effect=:effect,
                     stage=:stage, note=:note, updated_world=:updated_world""",
                row,
            )

    def intent_list(
        self, instance_id: str, timeline_id: str, character_id: str | None = None
    ) -> list[dict[str, Any]]:
        if character_id is None:
            rows = self._conn.execute(
                """SELECT * FROM intent WHERE instance_id=? AND timeline_id=?
                   ORDER BY character_id, id""",
                (instance_id, timeline_id),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT * FROM intent WHERE instance_id=? AND timeline_id=? AND character_id=?
                   ORDER BY id""",
                (instance_id, timeline_id, character_id),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ---------- 事件 / 说法 / 获知 / 效果 ----------

    def event_window(
        self, instance_id: str, timeline_id: str, *, until: int, limit: int = 50
    ) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT * FROM event WHERE instance_id=? AND timeline_id=? AND world_seconds<=?
               ORDER BY world_seconds DESC, seq DESC LIMIT ?""",
            (instance_id, timeline_id, until, limit),
        ).fetchall()
        return [_row_to_dict(r) for r in reversed(rows)]

    def event_ids(self, instance_id: str, timeline_id: str) -> set[str]:
        rows = self._conn.execute(
            "SELECT id FROM event WHERE instance_id=? AND timeline_id=?", (instance_id, timeline_id)
        ).fetchall()
        return {str(r["id"]) for r in rows}

    def claim_list(
        self, instance_id: str, timeline_id: str, *, event_id: str | None = None
    ) -> list[dict[str, Any]]:
        if event_id is None:
            rows = self._conn.execute(
                "SELECT * FROM claim WHERE instance_id=? AND timeline_id=? ORDER BY earliest_world, id",
                (instance_id, timeline_id),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT * FROM claim WHERE instance_id=? AND timeline_id=? AND event_id=?
                   ORDER BY earliest_world, id""",
                (instance_id, timeline_id, event_id),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def knowledge_window(
        self, instance_id: str, timeline_id: str, character_id: str, *, until: int, limit: int = 40
    ) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT * FROM knowledge WHERE instance_id=? AND timeline_id=? AND character_id=?
               AND world_seconds<=? ORDER BY world_seconds DESC, id DESC LIMIT ?""",
            (instance_id, timeline_id, character_id, until, limit),
        ).fetchall()
        return [_row_to_dict(r) for r in reversed(rows)]

    def knowledge_ids(self, instance_id: str, timeline_id: str, character_id: str) -> set[str]:
        rows = self._conn.execute(
            "SELECT id FROM knowledge WHERE instance_id=? AND timeline_id=? AND character_id=?",
            (instance_id, timeline_id, character_id),
        ).fetchall()
        return {str(r["id"]) for r in rows}

    def effect_active_ids(self, instance_id: str, timeline_id: str) -> set[str]:
        rows = self._conn.execute(
            "SELECT id FROM effect_state WHERE instance_id=? AND timeline_id=? AND active=1",
            (instance_id, timeline_id),
        ).fetchall()
        return {str(r["id"]) for r in rows}

    def effect_window(
        self, instance_id: str, timeline_id: str, *, until: int, targets: Iterable[str] | None = None
    ) -> list[dict[str, Any]]:
        """仍有效的后果（§六）：过期或已解除的不再参与因果。"""
        rows = self._conn.execute(
            """SELECT * FROM effect_state WHERE instance_id=? AND timeline_id=? AND active=1
               AND from_world<=? ORDER BY from_world, seq, id""",
            (instance_id, timeline_id, until),
        ).fetchall()
        wanted = {str(item) for item in targets} if targets else None
        out = []
        for row in rows:
            item = _row_to_dict(row)
            if wanted is not None and str(item.get("target")) not in wanted:
                continue
            out.append(item)
        return out

    # ---------- 补卡 ----------

    def character_join_add(self, row: dict[str, Any]) -> None:
        payload = {
            "commit_id": "", "state": "active", "request_id": "", "revoked_world": None,
            **row,
        }
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO character_join(instance_id, timeline_id, character_id, joined_world,
                                              card, note, acquainted, created_real,
                                              commit_id, state, request_id, revoked_world)
                   VALUES(:instance_id, :timeline_id, :character_id, :joined_world, :card, :note,
                          :acquainted, :created_real, :commit_id, :state, :request_id, :revoked_world)
                   ON CONFLICT(instance_id, timeline_id, character_id) DO UPDATE SET
                     joined_world=:joined_world, card=:card, note=:note, acquainted=:acquainted,
                     commit_id=:commit_id, state=:state, request_id=:request_id,
                     revoked_world=:revoked_world""",
                payload,
            )

    def character_join_publish(
        self,
        join_row: dict[str, Any],
        *,
        units: list[dict[str, Any]],
        plan: dict[str, Any],
        setting: dict[str, Any],
    ) -> None:
        """补卡的原子发布（§3.7 第 6 条）：实例定义、线内成员资格、角色状态与日程一次落盘。

        定义写进实例设定快照（实例级不可变对象），成员资格只进本线——两者分开留痕。
        """
        payload = {"commit_id": "", "state": "active", "request_id": "", "revoked_world": None, **join_row}
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE instance SET setting=? WHERE id=?",
                (json.dumps(setting, ensure_ascii=False), str(join_row["instance_id"])),
            )
            self._conn.execute(
                """INSERT INTO character_join(instance_id, timeline_id, character_id, joined_world,
                                              card, note, acquainted, created_real,
                                              commit_id, state, request_id, revoked_world)
                   VALUES(:instance_id, :timeline_id, :character_id, :joined_world, :card, :note,
                          :acquainted, :created_real, :commit_id, :state, :request_id, :revoked_world)
                   ON CONFLICT(instance_id, timeline_id, character_id) DO UPDATE SET
                     joined_world=:joined_world, card=:card, note=:note, acquainted=:acquainted,
                     created_real=:created_real,
                     commit_id=:commit_id, state=:state, request_id=:request_id,
                     revoked_world=:revoked_world""",
                payload,
            )
            self._conn.executemany(
                """INSERT INTO unit(id, instance_id, timeline_id, character_id, mode, semantic, basis,
                                    confidence, stability, archived, consumed, updated_world)
                   VALUES(:id, :instance_id, :timeline_id, :character_id, :mode, :semantic, :basis,
                          :confidence, :stability, :archived, :consumed, :updated_world)
                   ON CONFLICT(instance_id, timeline_id, character_id, id) DO UPDATE SET
                     confidence=:confidence, stability=:stability, archived=:archived,
                     consumed=:consumed, updated_world=:updated_world""",
                units,
            )
            self._conn.execute(
                """INSERT OR IGNORE INTO life_plan(id, instance_id, timeline_id, character_id, day_index,
                                                   windows, state, created_world, note)
                   VALUES(:id, :instance_id, :timeline_id, :character_id, :day_index,
                          :windows, :state, :created_world, :note)""",
                plan,
            )

    def character_join_set_commit(self, instance_id: str, timeline_id: str, character_id: str, commit_id: str) -> None:
        """把加入提交记到成员资格行上（§3.7 三处留痕之三）。"""
        with self._lock, self._conn:
            self._conn.execute(
                """UPDATE character_join SET commit_id=? WHERE instance_id=? AND timeline_id=? AND character_id=?""",
                (str(commit_id), instance_id, timeline_id, character_id),
            )

    def character_join_by_request(self, instance_id: str, timeline_id: str, request_id: str) -> dict[str, Any] | None:
        """同一补卡请求的既往结果（幂等重试用，§3.7 末条）。"""
        if not str(request_id):
            return None
        row = self._conn.execute(
            """SELECT * FROM character_join WHERE instance_id=? AND timeline_id=? AND request_id=? LIMIT 1""",
            (instance_id, timeline_id, str(request_id)),
        ).fetchone()
        return _row_to_dict(row) if row is not None else None

    def character_join_revoke_missing(self, timeline_id: str, keep: set[str], *, moment: int) -> int:
        """回滚跨过补卡点：目标快照里没有的成员资格转为撤销（记录留着，不能复活，§3.7 末条）。

        按快照比对而不是比水位——补卡锚定的就是当时的已完成水位，回滚到补卡前的同一水位提交时
        两者相等，只有「目标提交里有没有这条成员资格」才是准的。
        """
        with self._lock, self._conn:
            if keep:
                marks = ",".join("?" for _ in keep)
                cursor = self._conn.execute(
                    f"""UPDATE character_join SET state='revoked', revoked_world=?
                        WHERE timeline_id=? AND state='active' AND character_id NOT IN ({marks})""",
                    [int(moment), timeline_id, *sorted(keep)],
                )
            else:
                cursor = self._conn.execute(
                    """UPDATE character_join SET state='revoked', revoked_world=?
                       WHERE timeline_id=? AND state='active'""",
                    (int(moment), timeline_id),
                )
            return int(cursor.rowcount)

    def character_join_ids(self, instance_id: str) -> set[str]:
        """该实例下所有补入过的角色标识（含已撤销的线级记录，跨线合并）。

        用来把「补入定义」与「初始定义」分开：两者都住在实例设定快照里，但只有前者受
        线级成员资格约束（§3.7 第 1 条）。
        """
        rows = self._conn.execute(
            "SELECT DISTINCT character_id FROM character_join WHERE instance_id=?", (str(instance_id),)
        ).fetchall()
        return {str(row["character_id"]) for row in rows}

    def character_membership(self, instance_id: str, timeline_id: str, character_id: str, *, until: int) -> str:
        """本线成员资格（§3.7 第 1 条）：member=初始定义 / joined=已补入 / revoked / unjoined=非成员。"""
        row = self._conn.execute(
            """SELECT * FROM character_join WHERE instance_id=? AND timeline_id=? AND character_id=?""",
            (instance_id, timeline_id, str(character_id)),
        ).fetchone()
        if row is None:
            return "unjoined"
        item = _row_to_dict(row) or {}
        if str(item.get("state") or "active") != "active":
            return "revoked"
        if int(item.get("joined_world") or 0) > int(until):
            return "revoked"  # 水位还没到她加入的那一刻：本线暂时不暴露
        return "joined"

    def character_join_list(
        self, instance_id: str, timeline_id: str, *, until: int | None = None, state: str | None = "active"
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM character_join WHERE instance_id=? AND timeline_id=?"
        args: list[Any] = [instance_id, timeline_id]
        if state is not None:
            sql += " AND state=?"
            args.append(str(state))
        if until is not None:
            sql += " AND joined_world<=?"
            args.append(int(until))
        rows = self._conn.execute(sql + " ORDER BY joined_world, character_id", args).fetchall()
        return [_row_to_dict(r) for r in rows]

    def clock_set_processed(self, timeline_id: str, processed_world: int, *, generation: int | None = None) -> bool:
        """推进水位：世代不符即拒（迟到任务不得写回旧水位，§2.6/§7.1）。"""
        with self._lock, self._conn:
            if generation is None:
                cursor = self._conn.execute(
                    "UPDATE timeline_clock SET processed_world=? WHERE timeline_id=? AND processed_world<?",
                    (processed_world, timeline_id, processed_world),
                )
            else:
                cursor = self._conn.execute(
                    """UPDATE timeline_clock SET processed_world=?
                       WHERE timeline_id=? AND generation=? AND processed_world<?""",
                    (processed_world, timeline_id, generation, processed_world),
                )
            return cursor.rowcount > 0

    def rate_add(
        self, timeline_id: str, *, input_real: float, effective_real: int, rate: int, seq: int
    ) -> int:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """INSERT INTO rate_command(timeline_id, input_real, effective_real, rate, seq, state)
                   VALUES(?,?,?,?,?, 'pending')""",
                (timeline_id, input_real, effective_real, rate, seq),
            )
            return int(cursor.lastrowid)

    def rate_pending(self, timeline_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM rate_command WHERE timeline_id=? AND state='pending' ORDER BY effective_real, seq",
            (timeline_id,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def rate_apply(self, ids: list[int]) -> None:
        if not ids:
            return
        with self._lock, self._conn:
            self._conn.executemany("UPDATE rate_command SET state='applied' WHERE id=?", [(i,) for i in ids])

    def rate_clear_settled(self, timeline_id: str) -> int:
        """清掉该线已结算（applied）与已取消（cancelled）的账本行；pending 一律不动。

        这两种状态没有读者：真值在 clock 行（当前倍率段）与提交快照（§5.1 / §七）里，
        账本只用来回答「还有哪些待生效」。留着只会随每次倍率变更无界积行。
        """
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "DELETE FROM rate_command WHERE timeline_id=? AND state IN ('applied','cancelled')",
                (timeline_id,),
            )
            return int(cursor.rowcount)

    def rate_cancel_pending(self, timeline_id: str) -> int:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE rate_command SET state='cancelled' WHERE timeline_id=? AND state='pending'",
                (timeline_id,),
            )
            return int(cursor.rowcount)

    # ---------- 运行层：角色状态 ----------

    def unit_put(self, row: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO unit(id, instance_id, timeline_id, character_id, mode, semantic, basis,
                                    confidence, stability, archived, consumed, updated_world)
                   VALUES(:id, :instance_id, :timeline_id, :character_id, :mode, :semantic, :basis,
                          :confidence, :stability, :archived, :consumed, :updated_world)
                   ON CONFLICT(instance_id, timeline_id, character_id, id) DO UPDATE SET
                     confidence=:confidence, stability=:stability, archived=:archived,
                     consumed=:consumed, updated_world=:updated_world""",
                row,
            )

    def unit_list(self, instance_id: str, timeline_id: str, character_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT * FROM unit WHERE instance_id=? AND timeline_id=? AND character_id=?
               ORDER BY archived, id""",
            (instance_id, timeline_id, character_id),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def plan_put(self, row: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT OR IGNORE INTO life_plan(id, instance_id, timeline_id, character_id, day_index,
                                                   windows, state, created_world, note)
                   VALUES(:id, :instance_id, :timeline_id, :character_id, :day_index,
                          :windows, :state, :created_world, :note)""",
                row,
            )

    def plan_get(self, instance_id: str, timeline_id: str, character_id: str, day_index: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            """SELECT * FROM life_plan WHERE instance_id=? AND timeline_id=? AND character_id=? AND day_index=?""",
            (instance_id, timeline_id, character_id, day_index),
        ).fetchone()
        return _row_to_dict(row) if row else None

    def plan_latest(self, instance_id: str, timeline_id: str, character_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            """SELECT * FROM life_plan WHERE instance_id=? AND timeline_id=? AND character_id=?
               ORDER BY day_index DESC LIMIT 1""",
            (instance_id, timeline_id, character_id),
        ).fetchone()
        return _row_to_dict(row) if row else None

    def experience_add(self, row: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT OR IGNORE INTO experience(id, instance_id, timeline_id, character_id, world_seconds,
                                                    kind, summary, source_ref, confidence)
                   VALUES(:id, :instance_id, :timeline_id, :character_id, :world_seconds,
                          :kind, :summary, :source_ref, :confidence)""",
                row,
            )

    def experience_window(
        self, instance_id: str, timeline_id: str, character_id: str, *, until: int, limit: int = 20
    ) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT * FROM experience WHERE instance_id=? AND timeline_id=? AND character_id=?
               AND world_seconds<=? ORDER BY world_seconds DESC LIMIT ?""",
            (instance_id, timeline_id, character_id, until, limit),
        ).fetchall()
        return [_row_to_dict(r) for r in reversed(rows)]

    def experience_added_since(
        self, instance_id: str, timeline_id: str, *, since: int, limit: int = 20
    ) -> list[dict[str, Any]]:
        """水位推进期间新增的经历（按角色分组由调用方处理）。"""
        rows = self._conn.execute(
            """SELECT * FROM experience WHERE instance_id=? AND timeline_id=? AND world_seconds>?
               ORDER BY world_seconds LIMIT ?""",
            (instance_id, timeline_id, since, limit),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def write_probe(self) -> None:
        """写盘自检：不可写时抛 sqlite3.Error（调用方据此进入 persistence_blocked）。"""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('write_probe', ?)", (str(time.time()),)
            )
