package id.mantau.agent.inference.fall

import kotlin.math.abs
import kotlin.math.atan2
import kotlin.math.hypot

enum class Posture(val wire: String) {
    STANDING("standing"), SITTING("sitting"), LYING("lying"), UNKNOWN("unknown");

    companion object {
        fun of(wire: String) = entries.first { it.wire == wire }
    }
}

/** One tracked person in one frame; coordinates normalized to the frame (0..1, top-left). */
data class PersonObservation(
    val trackId: Int,
    val left: Double,
    val top: Double,
    val width: Double,
    val height: Double,
    val posture: Posture,
    /** Hip-centroid movement since this track's last observation, as a fraction of the frame diagonal. */
    val motion: Double,
    /** Mean visibility of shoulders and hips. */
    val confidence: Double,
) {
    /** Where the person stands on the floor plane: bottom-center of the box. */
    val anchorX: Double get() = left + width / 2
    val anchorY: Double get() = top + height
}

/**
 * Port of mantau-AI `mantau.api.streaming.ObservationTracker`: per-person observations
 * from the fall stage's tracks. Arithmetic mirrors numpy's float32/float64 mix exactly
 * (see [PersonPose]); the shared pose-sequence fixtures hold both to the same output.
 */
class ObservationTracker(
    private val aspectRatioHorizontal: Double,
    private val torsoAngleHorizontal: Double,
    private val trackMaxAge: Double,
) {
    private val lastSeen = LinkedHashMap<Int, Triple<Double, Double, Long>>()

    fun observe(
        people: List<PersonPose>, updates: List<TrackUpdate>, height: Int, width: Int, timestampMs: Long,
    ): List<PersonObservation> {
        check(people.size == updates.size)
        val diagonal = hypot(width.toDouble(), height.toDouble())
        val out = people.zip(updates).map { (person, update) ->
            val bbox = person.bbox
            val cx = person.centroidX * width
            val cy = person.centroidY * height
            val previous = lastSeen[update.trackId]
            val motion = if (previous == null) 0.0 else hypot(cx - previous.first, cy - previous.second) / diagonal
            lastSeen[update.trackId] = Triple(cx, cy, timestampMs)
            var visibility = -0.0f
            for (i in intArrayOf(PersonPose.L_SHOULDER, PersonPose.R_SHOULDER, PersonPose.L_HIP, PersonPose.R_HIP)) {
                visibility += person.landmarks[i][3]
            }
            PersonObservation(
                trackId = update.trackId,
                left = bbox[0].toDouble() / width,
                top = bbox[1].toDouble() / height,
                width = maxOf(bbox[2] - bbox[0], 0).toDouble() / width,
                height = maxOf(bbox[3] - bbox[1], 0).toDouble() / height,
                posture = posture(person, update.state),
                motion = motion,
                confidence = (visibility / 4f).coerceIn(0f, 1f).toDouble(),
            )
        }
        val maxAgeMs = trackMaxAge * 1000
        lastSeen.entries.removeIf { timestampMs - it.value.third > maxAgeMs }
        return out
    }

    fun posture(person: PersonPose, state: TrackState): Posture {
        val horizontal = person.aspectRatio >= aspectRatioHorizontal || person.torsoAngleDeg >= torsoAngleHorizontal
        if (state == TrackState.FALLEN || horizontal) return Posture.LYING
        val lm = person.landmarks
        val angles = mutableListOf<Double>()
        for ((hip, knee) in listOf(PersonPose.L_HIP to PersonPose.L_KNEE, PersonPose.R_HIP to PersonPose.R_KNEE)) {
            if (lm[hip][3] < 0.5f || lm[knee][3] < 0.5f) continue
            val dx = (lm[knee][0] - lm[hip][0]) * person.imageWidth.toFloat()
            val dy = (lm[knee][1] - lm[hip][1]) * person.imageHeight.toFloat()
            angles += Math.toDegrees(atan2(abs(dx).toDouble(), dy.toDouble()))
        }
        if (angles.isEmpty()) return Posture.UNKNOWN
        return if (angles.sum() / angles.size >= 50.0) Posture.SITTING else Posture.STANDING
    }
}
