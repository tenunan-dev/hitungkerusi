# Migration inventory schema validation

`build_analytics_migration_inventory.py` validates these Draft 2020-12 schemas
only with the standards-complete Python `jsonschema` package (version 4 or
newer, with `Draft202012Validator`). It deliberately has no reduced internal
validator: if that dependency is absent, generation stops before writing any
evidence artifact.

The generator is Python 3.9-compatible. Its tests use temporary synthetic
trees only and do not create a production inventory.

## Isolated Python 3.9 setup

Use `/usr/bin/python3`; do not install packages globally or into the OPS
checkout. The pinned dependency is deliberately kept in
`migration/schema/requirements-py39.txt`.

```sh
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m venv /private/tmp/ge16-task31-py39
PYTHONDONTWRITEBYTECODE=1 /private/tmp/ge16-task31-py39/bin/python -m pip install -r migration/schema/requirements-py39.txt
PYTHONDONTWRITEBYTECODE=1 /private/tmp/ge16-task31-py39/bin/python -m unittest discover -s tests -v
```

Validation requires that setup because there is intentionally no partial
fallback. The generator detects tracked, ignored, and untracked paths from a
Git work tree using read-only Git queries; a non-Git source remains
`unresolved`.
