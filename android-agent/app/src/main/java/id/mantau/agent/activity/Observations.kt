package id.mantau.agent.activity

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
