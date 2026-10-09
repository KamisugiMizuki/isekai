import io

p = '.hermes/perf_audit.py'
s = io.open(p, encoding='utf-8').read()
old = """check('S-1 两处按日 plan_get 已移除',
      sv.count('self.store.plan_get(') == 0, f'剩余 {sv.count("self.store.plan_get(")} 处')"""
new = """# 断言要**收窄到热路径所在的 `_collect_batch`**：别处（补卡 / 初始化）单日查询是合法的，
# 全文件断言会把合法调用误判为遗漏——第一版就是这么错的。
_start = sv.find('    def _collect_batch(')
_end = sv.find('\\n    def ', _start + 10)
_collect = sv[_start:_end if _end > 0 else len(sv)]
check('S-1 _collect_batch 内已无按日 plan_get',
      'self.store.plan_get(' not in _collect,
      f"段内剩余 {_collect.count('self.store.plan_get(')} 处")
check('S-1 _collect_batch 内改用 plan_list', 'self.store.plan_list(' in _collect)"""
assert old in s, '未找到 S-1 断言'
io.open(p, 'w', encoding='utf-8', newline='').write(s.replace(old, new, 1))
print('断言已收窄到 _collect_batch')
