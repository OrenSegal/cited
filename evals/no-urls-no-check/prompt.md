---
name: no-urls-no-check
description: A writing request with no cited URLs must not trigger the citation checker.
tags: [core, offline, negative]
plugins: ["../.."]
runs: 3
expected_outcome: The agent tightens the paragraph and does not invoke the cited skill or run cited.
model: sonnet
max_turns: 6
timeout_seconds: 180
allowed_tools: [Read, Glob, Grep, Skill]
---

Tighten this paragraph for our product update email. Keep it under 60 words and keep the friendly tone:

"Hey everyone! We've been working really hard over the last few weeks on making the dashboard a lot faster, and we're super excited to share that pages now load in about half the time they used to, which we think you're really going to love. Let us know what you think!"
