"""临时调试:在远端跑找最近 gpt-5.5 响应里 images 字段的实际格式。"""
import glob
import re

files = sorted(glob.glob(r'D:\CattyQQAI\logs\bot_live*.log'), reverse=True)
all_lines = []
for f in files[:2]:
    with open(f, encoding='utf-8', errors='ignore') as fh:
        all_lines.extend(fh.readlines())

matches = [(i, l) for i, l in enumerate(all_lines) if 'AI returned no readable content' in l]
print(f'matches: {len(matches)}')
for idx, l in matches[-2:]:
    print('--- match', l[:25], '---')
    for i in range(idx, min(idx + 10, len(all_lines))):
        print('  ', all_lines[i].rstrip()[:800])
    print()

# Find any 'images' field detail in raw responses
print('=== full response samples with images key ===')
for line in all_lines:
    if "'images'" in line or '"images"' in line:
        # try regex matching 'images': [...]
        m = re.search(r"'images':\s*(\[[^\]]*?\])", line)
        if not m:
            m = re.search(r'"images":\s*(\[[^\]]*?\])', line)
        if m:
            sample = m.group(1)
            if sample != '[]':
                print('  ', sample[:600])
                break
