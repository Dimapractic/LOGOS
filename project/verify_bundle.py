"""Проверка файлов исходного комплекта без сторонних зависимостей."""
import hashlib
import json
import sys
from pathlib import Path

root=Path(__file__).resolve().parent.parent
manifest=json.loads((root/'SHA256SUMS.json').read_text(encoding='utf-8'))
errors=[]
for name,expected in manifest.items():
    path=root/name
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
        errors.append(name)
if errors:
    print('Missing or changed files:')
    print('\n'.join(errors))
    sys.exit(1)
print('OK:',len(manifest),'files verified.')
