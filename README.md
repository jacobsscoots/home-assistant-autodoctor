# Home Assistant AutoDoctor App Repository

This repository is a Home Assistant App (add-on) store repository containing **AutoDoctor**.

AutoDoctor watches Home Assistant's `system_log_event` stream, fingerprints and deduplicates
incidents, stores them persistently, and can optionally request read-only AI diagnoses. v0.1.5
adds a privacy-safe local memory/RAG layer on top of the existing fail-closed AI budget,
per-family fairness, startup backlog protection, and token/cost observability. Automatic repair
execution remains disabled.

## Local AutoDoctor memory

AutoDoctor keeps its long-term operational memory in `/data/autodoctor.db`. Before an AI diagnosis,
it can retrieve a small bounded set of relevant historical knowledge and observed topology locally.
The memory system includes:

- verified manual fixes and AI hypotheses stored separately from raw incidents;
- trust classes/scores so verified fixes outrank unverified AI guesses;
- expiry/freshness metadata and Home Assistant/AutoDoctor version provenance;
- persistent random entity pseudonyms, so the same real entity has a stable private alias across incidents/restarts;
- broad failure-pattern signatures above exact fingerprints;
- an evidence-only topology graph built from relationships actually observed in incidents;
- outcome feedback when a diagnosed incident continues, recurs heavily, or remains quiet;
- SQLite FTS5 retrieval with a local fallback when FTS5 is unavailable.

Historical memory is supplied to the AI as evidence, never as authority. AutoDoctor explicitly warns
the model not to copy an old fix blindly.

## Home Assistant incident & repair history

The repository's **Issues** tab is also used as a durable, sanitised history of Home Assistant
problems, diagnoses, fixes and verification results.

- [Master incident & repair history](https://github.com/jacobsscoots/home-assistant-autodoctor/issues/16)
- [Planned automatic GitHub Issues mirroring](https://github.com/jacobsscoots/home-assistant-autodoctor/issues/17)

Historical manual repairs are explicitly identified as manual. An issue must never claim
"fixed by AutoDoctor" unless AutoDoctor actually executed the repair and the result was verified.

A reusable incident template is available at
`.github/ISSUE_TEMPLATE/home-assistant-incident.md`.

See `autodoctor/README.md` and `autodoctor/DOCS.md` for details.

To add this repository to Home Assistant: Settings → Add-ons → Add-on Store → ⋮ → Repositories →
add `https://github.com/jacobsscoots/home-assistant-autodoctor`.
