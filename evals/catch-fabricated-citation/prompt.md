---
name: catch-fabricated-citation
description: Asked to check a research note's citations before it goes to a team, the agent runs cited against the recorded pages in ./source-cache (seeded by scaffold.sh, so the case needs --scaffold and no network) and reports the wrong date, the dead link and the paraphrase instead of vouching for every claim, with the skill's "N of M claims verified against source" line.
tags: [core, offline]
plugins: ["../.."]
runs: 3
expected_outcome: Claims 1 and 2 verified; claim 3 not on the page (the RFC is from June 1999, not 2001); claim 4 needs human review (paraphrase); claim 5 broken (the RFC number does not exist).
model: sonnet
max_turns: 20
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill, Bash, Write]
---

I'm about to send this note to my team. Before I do, check that each claim is actually on the page it cites, and tell me which ones I can't ship as written. This machine is offline, but our research tool saved a copy of every cited page in ./source-cache (it's a cited cache), so check against those.

---
Reserved domain names, a quick note

1. "To safely satisfy these needs, four domain names are reserved as top level domains." Source: https://www.rfc-editor.org/rfc/rfc2606.txt
2. ".invalid" is intended for use in online construction of domain names that are sure to be invalid. Source: https://www.rfc-editor.org/rfc/rfc2606.html
3. RFC 2606 was published in June 2001. Source: https://www.rfc-editor.org/rfc/rfc2606.txt
4. The RFC sets aside four top level names so that testers and documentation writers have safe choices. Source: https://www.rfc-editor.org/rfc/rfc2606.txt
5. Example domains are reserved for documentation. Source: https://www.rfc-editor.org/rfc/rfc99999.txt
---
