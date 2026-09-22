package id.mantau.agent.inference

import id.mantau.agent.uplink.FallEvent
import java.time.Instant

class FallEventGate(
    private val cameraId: String,
    private val cooldownMs: Long = 30_000,
    private val dedupeMs: Long = 10_000,
) {
    private var lastEventAt = Long.MIN_VALUE
    private val tracks = linkedMapOf<Int, Long>()

    @Synchronized
    fun accept(candidate: DetectionCandidate, at: Instant): FallEvent? {
        require(candidate.confidence in 0.0..1.0)
        val now = at.toEpochMilli()
        if (lastEventAt != Long.MIN_VALUE && now - lastEventAt < cooldownMs) return null
        candidate.trackId?.let { track ->
            val previous = tracks[track]
            if (previous != null && now - previous < dedupeMs) return null
            tracks[track] = now
        }
        tracks.entries.removeIf { now - it.value > dedupeMs }
        lastEventAt = now
        return FallEvent(
            cameraId = cameraId,
            occurredAt = at,
            confidence = candidate.confidence,
            trackId = candidate.trackId,
            signals = candidate.signals,
        )
    }
}
