---
name: invalid-claims-file
description: Given a malformed claims list, the agent runs cited, reads its input errors (exit 2) and reports exactly what is wrong instead of claiming the citations were checked.
tags: [core, offline]
plugins: ["../.."]
runs: 3
expected_outcome: The agent reports that entry 1 has no source_url and entry 2's claim is not text, and does not say any citation was verified.
model: sonnet
max_turns: 12
timeout_seconds: 240
allowed_tools: [Read, Glob, Grep, Skill, Bash, Write]
---

Our pipeline produced this claims file and I want the citations verified before the report ships. Save it as claims.json and check it. Don't fix the file yourself, just tell me what you find.

```json
[
  {"id": "pricing", "claim": "The Pro plan costs $20 per month.", "source_url": "https://example.com/pricing"},
  {"id": "team", "claim": "The company has 45 employees."},
  {"id": "launch", "claim": 2019, "source_url": "https://example.com/about"}
]
```
