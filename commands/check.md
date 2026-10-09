---
description: Check that every claim in a claims file (or in the artifact you just wrote) is on the page it cites
argument-hint: "[claims.json | artifact file] [--strict] [--no-wayback]"
allowed-tools: Bash(cited:*), Read, Glob
---

Verify cited claims with the `cited` tool before anything ships. Arguments: `$ARGUMENTS`

1. Work out the input.
   - If the arguments name a `.json` file that is an array of `{id, claim, source_url}` objects, use it as is.
   - If they name another file (a report, a README, a lead list), or are empty and you have just written an artifact that cites URLs, extract every claim that is attributed to a URL into a JSON array of `{"id": "<stable key>", "claim": "<the exact sentence as written>", "source_url": "<the URL>"}`. Keep the claim text exactly as it appears in the artifact; do not reword it to match the page. Write the array to `cited-claims.json` next to the artifact (ask before writing if you are unsure where).
   - If there are no URL-attributed claims, say so and stop.
2. Run `cited <claims file> --json` plus any flags from the arguments (`--strict`, `--no-wayback`, `--cache DIR`, `--offline`). If the shell says `cited` is not found (the plugin's `bin/` is not on PATH, as in `claude plugin eval` runs), run `"${CLAUDE_PLUGIN_ROOT}/bin/cited"` with the same arguments instead; do not search the filesystem for it. If `HTTPS_PROXY` is set, as it is in Claude Code's sandboxed Bash, add `--proxy-from-env`: the sandbox has no direct route out, so without it every source fails to resolve and comes back `broken`. This is how cited reaches the network there, not a weaker flag.
3. Report from the JSON, not from memory:
   - one line: "N of M claims verified against source", then the counts per tier;
   - a table of every claim that is not `verified`: id, tier, and the `note` (which names the missing numbers or names);
   - what `summary.exit_code` means (`cited --help` lists the codes). An invalid claims file prints no JSON: show the problems cited listed on stderr.
4. For each `unsupported` or `broken` claim, propose a fix: drop it, find a source that says it, or tighten the wording to what the page says. For `low_match`, say a human must check it against the page. Do not edit the artifact unless asked.

Do not mark a claim verified yourself, and do not re-run with weaker flags (`--allow-private-addresses`, `--allow-host`) to make something pass.
