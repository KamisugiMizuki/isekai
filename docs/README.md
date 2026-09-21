# 设计文档地图

isekai 的根目标是完整、持续运行的异世界模拟。文档按产品边界组织；它们不是多套世界真值。

## WorldRuntime

世界事实、世界演化、时间、事件、认知、版本、会话核心、叙事中介和基础设施。

- [总纲](worldruntime/DESIGN.md)
- [世界运行层](worldruntime/WORLD_RUNTIME_SPEC.md)
- [WorldRuntime 对外接口](worldruntime/WORLD_RUNTIME_INTERFACE_SPEC.md)
- [世界设定层](worldruntime/WORLD_SETTING_SPEC.md)
- [世界事件引擎](worldruntime/EVENT_ENGINE_SPEC.md)
- [角色卡](worldruntime/CHARACTER_CARD_SPEC.md)
- [会话核心](worldruntime/SESSION_CORE_SPEC.md)
- [叙事中介层](worldruntime/NARRATIVE_LAYER_SPEC.md)
- [角色记忆](worldruntime/MEMORY_SPEC.md)
- [通道与 UMP](worldruntime/CHANNEL_PLUGIN_SPEC.md)
- [协议附录](worldruntime/CHANNEL_PROTOCOL_APPENDIX.md)
- [世界包附录](worldruntime/WORLD_PACKAGE_APPENDIX.md)
- [安卓一致性](worldruntime/ANDROID_SPEC.md)
- [历史存档](worldruntime/archive/)

## OC 故事层

普通 OC 用户体验与角色对话应用层。

- [普通 OC 用户评价草案](oc-story/USER_PERSPECTIVE_EVALUATION_DRAFT.md)
- [OC 故事层模块设计](oc-story/OC_STORY_LAYER_SPEC.md)

## Core Debugging

当前桌面壳、管理面和世界 / 角色生成工作区的官方参考调试外壳。它们消费 WorldRuntime 接口，不拥有世界真值，也不等于 TRPG 客户端。

- [桌面壳](core%20debugging/DESKTOP_SPEC.md)
- [生成工作区](core%20debugging/DESKTOP_GENERATION_WORKSPACE_SPEC.md)

## TRPG 客户端

玩家与 GM 的战役游玩、行动确认、场景视角和结果表达。客户端消费 TRPG Campaign Runtime、规则插件和 WorldRuntime 的稳定投影，不拥有规则或世界真值。

- [TRPG 客户端规范](trpg-client/TRPG_CLIENT_SPEC.md)

## TRPG 规则

- [规则插件协议](trpg-rules/TRPG_RULE_PLUGIN_SPEC.md)
- [TRPG GM 用户评价草案](trpg-rules/TRPG_GM_USER_EVALUATION_DRAFT.md)
- [TRPG 规则共用模块](trpg-rules/TRPG_RULE_COMMON_MODULE_SPEC.md)
- [TRPG 战役运行层](trpg-rules/TRPG_CAMPAIGN_RUNTIME_SPEC.md)
- [TRPG 规则层模块设计](trpg-rules/TRPG_RULES_LAYER_SPEC.md)

## Writing Assistant

高于 WorldRuntime 的故事约束与创作辅助模块；小说写作是主用途，GM 辅助是其中一种应用方式。

- [Writing Assistant 模块设计](writing-assistant/WRITING_ASSISTANT_SPEC.md)

## 阅读顺序

1. 先读 WorldRuntime 总纲、会话核心和叙事中介，确认世界真值、会话与表达边界。
2. 再读 Core Debugging 外壳，确认桌面壳和生成工作区如何消费核心接口。
3. 先读 TRPG 客户端规范，确认玩家 / GM 的产品流程与视角边界；再读规则插件协议、TRPG 战役运行层和规则共用模块，确认客户端消费的运行时契约；最后读 TRPG 规则层与 OC 故事评价，确认具体产品方案不反向拥有世界状态。

状态说明：评价草案用于判断产品需求，不代表其中提出的功能已经实现；SPEC 的实现状态以当前代码和行为测试为准。
