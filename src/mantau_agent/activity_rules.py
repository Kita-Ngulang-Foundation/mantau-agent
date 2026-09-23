"""The activity rules this agent runs. Each Mantau feature beyond falls
(prolonged stillness, nocturnal movement, bathroom duration) is one
`mantau_core.activity.ActivityRule`; add it here to switch it on."""

from __future__ import annotations

from mantau_core.activity import ActivityRule


def build_activity_rules() -> list[ActivityRule]:
    return []
