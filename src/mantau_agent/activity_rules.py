"""The activity rules this agent runs. Each Mantau feature beyond falls
(prolonged stillness, nocturnal movement, bathroom duration) is one
`mantau_core.activity.ActivityRule`; add it here to switch it on."""

from __future__ import annotations

from mantau_core.activity import ActivityRule, default_rules


def build_activity_rules() -> list[ActivityRule]:
    """Prolonged position, nocturnal movement and bathroom duration (see
    mantau_core.activity.rules). The saved per-camera DetectionSettings are
    applied on start by build_pipeline and on every apply_detection_settings."""
    return default_rules()
