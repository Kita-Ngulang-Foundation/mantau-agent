package id.mantau.agent.inference

data class ModeSelection(val requested: String, val effective: String, val reason: String)

/**
 * Chooses the effective inference mode. Same rules as the Python agent's
 * `select_inference_mode`: an explicit choice is honored when it can run here; when it
 * cannot, the mode that still detects falls is used instead of running nothing, and only
 * when nothing can run does selection fail.
 *
 * - EDGE needs a detector that loaded and benchmarked.
 * - CLOUD needs server inference (`GET /inference/capability` said available).
 * - HYBRID needs both.
 * - AUTO prefers EDGE when the detector keeps up and there is no thermal pressure, then
 *   CLOUD, then a slow EDGE as the last option.
 */
class InferenceModePolicy(
    private val detectorAvailable: () -> Boolean,
    private val detectorFailure: () -> String?,
    private val cloudAvailable: () -> Boolean = { false },
    private val detectorFastEnough: () -> Boolean = { true },
    private val thermal: ThermalStateProvider,
) {
    fun select(requested: String): ModeSelection {
        require(requested in setOf("AUTO", "EDGE", "CLOUD", "HYBRID")) { "Unknown inference mode" }
        val edge = detectorAvailable()
        val cloud = cloudAvailable()
        if (!cloud && !edge) {
            throw UnsupportedOperationException(
                "No inference mode is available: ${detectorFailure() ?: "no on-device detector"}; " +
                    "server inference unavailable.",
            )
        }
        val pressure = thermal.current()
        val edgeProblem = detectorFailure() ?: "on-device detector unavailable"
        return when (requested) {
            "EDGE" -> when {
                !edge -> ModeSelection(requested, "CLOUD", "EDGE requested but $edgeProblem; falling back to CLOUD.")
                pressure.pressured && cloud ->
                    ModeSelection(requested, "CLOUD", "Thermal ${pressure.wireName}; safely fell back from EDGE to CLOUD.")
                else -> ModeSelection(requested, "EDGE", "Verified detector is available and thermal state is ${pressure.wireName}.")
            }
            "CLOUD" ->
                if (cloud) ModeSelection(requested, "CLOUD", "CLOUD explicitly selected; the server runs detection on sampled frames.")
                else ModeSelection(requested, "EDGE", "CLOUD requested but server inference is unavailable; falling back to EDGE.")
            "HYBRID" -> when {
                !edge -> ModeSelection(requested, "CLOUD", "HYBRID requested but $edgeProblem; falling back to CLOUD.")
                !cloud -> ModeSelection(requested, "EDGE", "HYBRID requested but server inference is unavailable; falling back to EDGE.")
                pressure.pressured -> ModeSelection(requested, "CLOUD", "Thermal ${pressure.wireName}; safely fell back from HYBRID to CLOUD.")
                else -> ModeSelection(requested, "HYBRID", "Local detections receive one rate-limited confirmation frame per event.")
            }
            else -> when {
                edge && detectorFastEnough() && !pressure.pressured ->
                    ModeSelection("AUTO", "EDGE", "AUTO selected verified EDGE detector.")
                cloud -> ModeSelection(
                    "AUTO", "CLOUD",
                    when {
                        !edge -> "$edgeProblem; AUTO selected CLOUD."
                        pressure.pressured -> "Thermal ${pressure.wireName}; AUTO selected CLOUD."
                        else -> "On-device detector is too slow; AUTO selected CLOUD."
                    },
                )
                else -> ModeSelection("AUTO", "EDGE", "Server inference unavailable; AUTO kept the on-device detector.")
            }
        }
    }
}

class HybridConfirmationPolicy(
    private val minIntervalMs: Long = 5_000,
) {
    private val confirmedEvents = LinkedHashSet<String>()
    private var lastUploadAt = Long.MIN_VALUE

    @Synchronized
    fun shouldUpload(eventId: String, nowMs: Long): Boolean {
        if (eventId in confirmedEvents) return false
        if (lastUploadAt != Long.MIN_VALUE && nowMs - lastUploadAt < minIntervalMs) return false
        confirmedEvents += eventId
        while (confirmedEvents.size > 256) confirmedEvents.remove(confirmedEvents.first())
        lastUploadAt = nowMs
        return true
    }
}
