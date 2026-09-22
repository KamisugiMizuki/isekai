# 样例世界包：灰潮纪

随发行的一部样例世界（`DESKTOP_SPEC` §九 的待定项，2026-09-22 拍板：随发行一部样例包）。
它是一份**普通的创作产物**：结构与标识按 `WORLD_SETTING_SPEC` 与 `WORLD_PACKAGE_APPENDIX.md` 写成，
通过校验器与联合校验；既能用来第一次就把世界跑起来，也可以当手写世界包的形状参考。

| 文件 | 内容 |
|---|---|
| `huichao.json` | 世界包：三条公理、三种消息来源（信报 / 碑刻 / 盐户口传）、三条实情与三条说法、三名登记人物、两部史料传本、两个事件族（3 个模板 + 2 个固定节庆）、两类环境事实（潮位 / 风信）、两条生活线、两个角色类型、双谜题初始状态 |
| `huichao.card1.json` | 角色卡「堤禾」· 堤务吏（照看水位尺的低级执事，对议会的说法留一半） |
| `huichao.card2.json` | 角色卡「潮生」· 盐户（滩场晒盐，先开口的那一类） |

世界设定：潮水每三十日退一次，露出盐滩与旧堤；沿岸三城邦靠驿站信报和碑刻记着水位。
十二年前的北堤崩塌、三年前的刻线改动，是这个世界留给角色的两条线索。

## 用起来（CLI 路径，均已实测）

把三份文件放进创作目录（默认 `<根目录>/packages/`），然后：

```bash
cp examples/sample_world/huichao*.json packages/

# 包过校验、两张卡过联合校验（未通过一律不落盘）
python -m isekai_core.world_cli package validate --file packages/huichao.json
python -m isekai_core.world_cli card confirm --package packages/huichao.json --file packages/huichao.card1.json
python -m isekai_core.world_cli card confirm --package packages/huichao.json --file packages/huichao.card2.json

# 创建实例（创建即冻结，默认不激活）
python -m isekai_core.world_cli instance create --package packages/huichao.json \
  --card packages/huichao.card1.json,packages/huichao.card2.json --display-name 灰潮纪

# 取时间线标识，把这条线激活（不激活只能读历史）
python -m isekai_core.world_cli instance info --id <实例 id>
python -m isekai_core.world_cli runtime activate --id <实例 id> --timeline <时间线 id>
```

路径规则：`--package` / `--card` 认 `packages/x.json` 与裸文件名两种写法（都落在创作目录）；
`--file` 按当前目录的字面路径读，所以上面写的是 `packages/...`。

桌面壳的等价路径：管理页「导入世界包」→「导入角色卡」（两张都要）→「创建实例」→ 激活。
`tests/test_sample_world.py` 把这条链路（导入 → 审定 → 创建 → 激活 → 联络面「可联络」）跑成行为测试：
样例被改坏就会红。
