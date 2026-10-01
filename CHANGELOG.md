# Changelog

## 0.2.0

- Renamed from verify-before-ship to cited. The old GitHub URL redirects.
- Now a valid Claude Code plugin: `.claude-plugin/plugin.json`, with the skill at `skills/cited/`.
- The script and references moved with the skill to `skills/cited/scripts/` and `skills/cited/references/`.
- CI runs `claude plugin validate`.

## 0.1.0

- Containment check with tiers `verified`, `low_match`, `snippet_only`, `unsupported`, `broken`.
