package id.mantau.agent.inference

data class ModeSelection(val requested: String, val effective: String, val reason: String)

class InferenceModePolicy(
    private val detectorAvailable: () -> Boolean,
    private val detectorFailure: () -> String?,
    private val cloudAvailable: () -> Boolean = { true },
    private val confirmationAvailable: () -> Boolean = { false },
    private val thermal: ThermalStateProvider,
) {
    fun select(requested: String): ModeSelection {
        require(requested in setOf("AUTO", "EDGE", "CLOUD", "HYBRID")) { "Unknown inference mode" }
        if (!cloudAvailable() && !detectorAvailable()) throw UnsupportedOperationException("No inference mode is available")
        val pressure = thermal.current()
        return when (requested) {
            "CLOUD" -> {
                if (!cloudAvailable()) throw UnsupportedOperationException("CLOUD upload is unavailable")
                ModeSelection(requested, "CLOUD", "CLOUD explicitly selected; sampled JPEG frames are disposable.")
            }
            "EDGE" -> {
                if (!detectorAvailable()) throw UnsupportedOperationException(detectorFailure() ?: "EDGE detector unavailable")
                if (pressure.pressured) ModeSelection(requested, "CLOUD", "Thermal ${pressure.wireName}; safely fell back from EDGE to CLOUD.")
                else ModeSelection(requested, "EDGE", "Verified detector is available and thermal state is ${pressure.wireName}.")
            }
            "HYBRID" -> {
                if (!detectorAvailable()) throw UnsupportedOperationException(detectorFailure() ?: "HYBRID detector unavailable")
                if (!confirmationAvailable()) throw UnsupportedOperationException(
                    "HYBRID unavailable: server has no event-correlated inference confirmation contract.",
                )
                if (pressure.pressured) ModeSelection(requested, "CLOUD", "Thermal ${pressure.wireName}; safely fell back from HYBRID to CLOUD.")
                else ModeSelection(requested, "HYBRID", "Local detections receive one rate-limited confirmation frame per event.")
            }
            else -> when {
                detectorAvailable() && !pressure.pressured -> ModeSelection("AUTO", "EDGE", "AUTO selected verified EDGE detector.")
                cloudAvailable() -> ModeSelection(
                    "AUTO", "CLOUD",
                    detectorFailure() ?: if (pressure.pressured) "Thermal ${pressure.wireName}; AUTO selected CLOUD." else "AUTO selected CLOUD.",
                )
                else -> throw UnsupportedOperationException("AUTO found no supported mode")
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
