"""Temporary exact-source integration; removed before the feature PR."""
from pathlib import Path
import hashlib
import json

root = Path.cwd().resolve()
changes = json.loads(Path('.github/backup-integrate.json').read_text())
manifest = json.loads(Path('.github/backup-source-sha256.json').read_text())
prepared = {}
for name, change in changes.items():
    path = (root / name).resolve()
    if not path.is_relative_to(root) or path.is_symlink():
        raise SystemExit('Invalid source path')
    original = path.read_bytes()
    if hashlib.sha256(original).hexdigest() != change['before']:
        raise SystemExit('Source changed: ' + name)
    lines = original.decode().splitlines(keepends=True)
    for start, end, replacement in reversed(change['edits']):
        lines[start:end] = replacement.splitlines(keepends=True)
    updated = ''.join(lines).encode()
    if hashlib.sha256(updated).hexdigest() != change['after']:
        raise SystemExit('Prepared source hash mismatch: ' + name)
    prepared[path] = updated
for path, content in prepared.items():
    path.write_bytes(content)
for name, expected in manifest.items():
    actual = hashlib.sha256((root / name).read_bytes()).hexdigest()
    if actual != expected:
        raise SystemExit('Transferred source hash mismatch: ' + name)
print('All 21 feature files match the locally reviewed and tested source.')
