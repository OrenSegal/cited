---
name: Bug report
about: A wrong tier, a crash, or a flag that does not do what the docs say
labels: bug
---

<!-- Security problems (for example a URL that reaches a private address) go
through the private advisory form, not here. See SECURITY.md. -->

**What happened**

**What you expected**

**To reproduce**

A minimal claims file (one or two entries) and the command you ran:

```json
[{"claim": "...", "source_url": "https://..."}]
```

```text
cited claims.json --json
```

Output, with `--json` if it is a wrong tier:

```text
```

**Environment**

- cited version (`cited --version`):
- Python version (`python3 --version`):
- OS:
- Run through: Claude Code plugin / `bin/cited` / `verify_claims.py` directly
