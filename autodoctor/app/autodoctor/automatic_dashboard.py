from __future__ import annotations

from .control_dashboard import ControlDashboard


class AutomaticControlDashboard(ControlDashboard):
    """Compatibility class; the shared renderer now uses actual executor health."""
