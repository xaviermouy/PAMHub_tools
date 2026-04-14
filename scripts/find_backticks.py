import re
content = open(r'C:\Users\xavier.mouy\Documents\GitHub\PAMHub_tools\scripts\audio_qc_basics_UI.html', encoding='utf-8').read()
line55 = content.split('\n')[54]

# Find \u not followed by 4 hex digits (malformed unicode escape)
bad_unicode = [(m.start(), line55[max(0,m.start()-10):m.start()+20])
               for m in re.finditer(r'\\u(?![0-9a-fA-F]{4})', line55)]
print(f"Malformed \\u escapes: {len(bad_unicode)}")
for pos, ctx in bad_unicode[:20]:
    print(f"  pos {pos}: {repr(ctx)}")

# Also check for \u{ style
unicode_point = list(re.finditer(r'\\u\{[^}]*\}', line55))
print(f"\n\\u{{}} style escapes: {len(unicode_point)}")

# Show all actual \u sequences in line
all_unicode = [(m.start(), line55[m.start():m.start()+8])
               for m in re.finditer(r'\\u', line55)]
print(f"\nAll \\u occurrences: {len(all_unicode)}")
for pos, ctx in all_unicode[:30]:
    print(f"  pos {pos}: {repr(ctx)}")
