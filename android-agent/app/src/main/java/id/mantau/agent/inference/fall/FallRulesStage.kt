package id.mantau.agent.inference.fall

import id.mantau.agent.inference.DetectionCandidate

/**
 * Poses in, fall candidates out: the part of on-device detection after pose
 * estimation. Pure JVM so unit tests drive it with recorded pose sequences.
 *
 * Candidates carry the same fields and signal names as the Python agent's
 * `MediapipeDetector` events (confidence, track id, velocity, aspect_ratio,
 * torso_angle_deg), so both agents emit identical fall events.
 */
class FallRulesStage(config: FallConfig = FallConfig(), confirmer: FallConfirmer? = null) {
    private val rules = FallDetector(config, confirmer)
    private var lastTimestampMs = Long.MIN_VALUE

    /** Returns null when the timestamp does not increase (frame ignored). */
    fun update(people: List<PersonPose>, timestampMs: Long): List<DetectionCandidate>? {
        if (timestampMs <= lastTimestampMs) return null
        lastTimestampMs = timestampMs
        val (_, decisions) = rules.update(people, timestampMs)
        return decisions.map { toCandidate(it) }
    }

    companion object {
        fun toCandidate(decision: FallDecision) = DetectionCandidate(
            confidence = decision.confidence.coerceIn(0.0, 1.0),
            trackId = decision.trackId,
            signals = mapOf(
                "velocity" to decision.velocity,
                "aspect_ratio" to decision.aspectRatio,
                "torso_angle_deg" to decision.torsoAngleDeg,
            ),
        )
    }
}
