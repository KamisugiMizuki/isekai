import io

p = '.hermes/self_audit.py'
s = io.open(p, encoding='utf-8').read()
old = """    for idx in ('ix_effect_target', 'ix_effect_expiry', 'ix_effect_retire',
                'ix_reaction_timeline_stage'):
        check(f'索引 {idx} 存在', idx in names)"""
new = """    # S-4 索引瘦身之后，这里的断言必须反映**实测后的现实**，而不是旧声明：
    #   `ix_effect_target` 经 EXPLAIN QUERY PLAN 证明从未被命中 ⇒ 已删除（断言它**不在**）；
    #   `ix_effect_retire` / `ix_effect_superseded_by` 只服务 B-7，而 B-7 默认停用 ⇒ 默认不创建。
    for idx in ('ix_effect_expiry', 'ix_reaction_timeline_stage', 'ix_effect_active'):
        check(f'索引 {idx} 存在（实测在用）', idx in names)
    check('索引 ix_effect_target 已按实测删除', 'ix_effect_target' not in names)
    check('B-7 索引默认不创建（开关关）', 'ix_effect_retire' not in names)"""
assert old in s, '未找到索引自审段'
io.open(p, 'w', encoding='utf-8', newline='').write(s.replace(old, new, 1))
print('自审断言已更新为 S-4 之后的现实')
