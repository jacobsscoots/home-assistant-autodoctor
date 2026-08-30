from __future__ import annotations


def incident_family(name: str, source: str = "") -> str:
    """Return a stable, coarse logger family for AI fairness accounting.

    Home Assistant component loggers keep the integration segment so unrelated
    integrations do not collapse into one giant ``homeassistant`` family. Other
    libraries use their top-level logger namespace, which groups noisy children
    such as ``kasa.smart.smartdevice`` together.
    """

    value = str(name or source or "").strip().lower()
    parts = [part for part in value.split(".") if part]
    if not parts:
        return "unknown"

    if len(parts) >= 3 and parts[0] == "homeassistant" and parts[1] == "components":
        return ".".join(parts[:3])
    if len(parts) >= 2 and parts[0] == "custom_components":
        return ".".join(parts[:2])
    if len(parts) >= 2 and parts[0] == "homeassistant":
        return ".".join(parts[:2])
    return parts[0]
