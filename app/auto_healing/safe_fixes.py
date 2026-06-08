from __future__ import annotations

from app.auto_healing.models import AutoHealingEvent, Classification


class RecommendationEngine:
    """Recommendation-only adapter kept for compatibility with older imports.

    Phase 2 never applies fixes. It only returns already computed dry-run
    recommendations from events that passed noise control.
    """

    def recommendations(self, events: list[AutoHealingEvent]) -> list[str]:
        result: list[str] = []
        for event in events:
            if event.classification != Classification.AUTO_HEALABLE_DRY_RUN:
                continue
            if not event.recommended_action:
                continue
            result.append(event.recommended_action)
        return sorted(set(result))
