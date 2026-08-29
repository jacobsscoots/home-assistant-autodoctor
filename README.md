# Home Assistant AutoDoctor App Repository

This repository is a Home Assistant App (add-on) store repository containing **AutoDoctor**.

AutoDoctor watches Home Assistant's `system_log_event` stream, fingerprints and deduplicates
incidents, stores them persistently, and can optionally request read-only AI diagnoses. v0.1.2
adds fail-closed monthly AI budget accounting; automatic repair execution remains disabled.

See `autodoctor/README.md` and `autodoctor/DOCS.md` for details.

To add this repository to Home Assistant: Settings → Add-ons → Add-on Store → ⋮ → Repositories →
add `https://github.com/jacobsscoots/home-assistant-autodoctor`.
