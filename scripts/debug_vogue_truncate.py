"""Find the real text length catty received for the VOGUE prompt message."""
import glob
import json
import re
from pathlib import Path

files = sorted(glob.glob(r'D:\CattyQQAI\logs\bot_live*.log'), reverse=True)
all_lines = []
for f in files[:2]:
    with open(f, encoding='utf-8', errors='ignore') as fh:
        all_lines.extend(fh.readlines())

print('=== 993255714 @ 185840951 timeline (around VOGUE prompt) ===')
hits = []
for i, l in enumerate(all_lines):
    if '993255714' in l and '185840951' in l:
        hits.append((i, l))
for idx, l in hits[-30:]:
    txt = l.rstrip()
    if any(k in txt for k in [
        'handle_event:538', 'handle_chat enter', 'tool_call', 'tool_chat:',
        'imagegen:', 'message_sent', 'AI returned'
    ]):
        print(f'  i={idx}', txt[:500])

print()
print('=== conversation_feed.jsonl entry ===')
feed = Path(r'D:\CattyQQAI\logs\conversation_feed.jsonl')
if feed.exists():
    found = False
    for line in feed.read_text(encoding='utf-8', errors='ignore').splitlines()[-3000:]:
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if (str(obj.get('sender_id')) == '993255714'
                and 'group:185840951' in str(obj.get('scope', ''))
                and 'VOGUE' in str(obj.get('text', ''))):
            text = obj.get('text', '')
            print(f'  FOUND text_len={len(text)}')
            print(f'  text first 300: {text[:300]!r}')
            print(f'  text last 200: {text[-200:]!r}')
            print('--- FULL TEXT ---')
            print(text)
            print('--- END ---')
            found = True
            break
    if not found:
        print('  no feed entry matched VOGUE')

print()
print('=== raw message text in [message.group.normal] line (entire) ===')
for line in all_lines:
    if 'message.group.normal' in line and '993255714' in line and '185840951' in line and 'VOGUE' in line:
        print(f'  raw line len={len(line)}')
        print(f'  full: {line.rstrip()}')
        break
else:
    print('  no group message line found containing VOGUE')
