# Contributing

Thanks for helping improve AutoDoctor.

## Before changing code

- Keep changes focused and avoid unrelated refactors.
- Preserve safe defaults: destructive or higher-risk Home Assistant changes must remain approval-required.
- Do not add real Home Assistant tokens, API keys, private entity names, hostnames, IP addresses, or personal diagnostic data to tests, examples, issues, or commits.
- Prefer stdlib or existing dependencies unless a new dependency clearly reduces risk or complexity.

## Local checks

From the repository root, run the same focused checks used by CI where possible:

```bash
python -m compileall -q autodoctor/app autodoctor/tests
python -m pytest -q autodoctor/tests
```

For changes that affect the container build, also run:

```bash
docker build --build-arg BUILD_VERSION="$(python3 - <<'PY'
from pathlib import Path
for line in Path('autodoctor/config.yaml').read_text().splitlines():
    if line.startswith('version:'):
        print(line.split(':', 1)[1].strip().strip('"\''))
        break
else:
    raise SystemExit('version not found')
PY
)" --build-arg BUILD_ARCH=amd64 autodoctor
```

## Pull requests

A good PR should include:

- a short explanation of the problem and the smallest safe fix
- focused tests for changed behaviour
- any user-visible configuration or documentation updates
- verification notes, including what was not tested

Avoid committing generated logs, secrets, databases, or local Home Assistant data.
