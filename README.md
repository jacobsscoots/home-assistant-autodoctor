# Home Assistant AutoDoctor App Repository

This repository is a Home Assistant App (add-on) store repository containing **AutoDoctor**.

AutoDoctor watches Home Assistant's `system_log_event` stream, fingerprints and deduplicates
incidents, stores them persistently, and can optionally request read-only AI diagnoses. v0.1.3
adds fail-closed monthly AI budget accounting, per-family AI fairness, startup backlog protection,
and safer token/cost observability. Automatic repair execution remains disabled.

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
