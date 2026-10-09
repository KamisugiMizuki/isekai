import io

p = '.hermes/self_audit.py'
s = io.open(p, encoding='utf-8').read()
old = "check('A-7 回滚清单含 effect_superseded', 'effect_superseded' in dom_src.split('CLEARED_ON_ROLLBACK')[1][:2000])"
new = (
    "import re as _re\n"
    "_m = _re.search(r'CLEARED_ON_ROLLBACK[^\\n]*= \\((.*?)\\n\\)', dom_src, _re.DOTALL)\n"
    "check('A-7 回滚清单含 effect_superseded', bool(_m) and 'effect_superseded' in _m.group(1),\n"
    "      '精确解析元组体（首次出现可能是说明文字，不能按字符串截断）')"
)
assert old in s, '未找到待修正的检查'
io.open(p, 'w', encoding='utf-8', newline='').write(s.replace(old, new, 1))
print('已修正自审检查')
