# Home Assistant AutoDoctor App Repository

This repository is a Home Assistant App (add-on) store repository containing a single
app: **AutoDoctor**.

AutoDoctor watches Home Assistant's `system_log_event` stream, deduplicates incidents
by fingerprint, stores them, and (optionally, when explicitly configured) asks an AI
provider to investigate and propose safe repairs. Automatic repair execution is not
implemented in v0.1.

See `autodoctor/README.md` and `autodoctor/DOCS.md` for details.

To add this repository to your Home Assistant instance: Settings → Add-ons → Add-on
Store → ⋮ → Repositories → add `https://github.com/jacobsscoots/home-assistant-autodoctor`.
