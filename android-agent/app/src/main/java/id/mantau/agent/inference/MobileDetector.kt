package id.mantau.agent.inference

import id.mantau.agent.uplink.JpegFrame

data class DetectorAvailability(
    val available: Boolean,
    val reason: String,
    val delegates: List<String> = emptyList(),
)

data class DetectionCandidate(
    val confidence: Double,
    val trackId: Int? = null,
    val signals: Map<String, Double> = emptyMap(),
)

interface MobileDetector : AutoCloseable {
    val backend: String
    val availability: DetectorAvailability
    fun benchmarkLatencyMs(): Double
    fun detect(frame: JpegFrame): List<DetectionCandidate>
    override fun close() {}
}

class UnavailableMobileDetector : MobileDetector {
    override val backend = "unavailable"
    override val availability = DetectorAvailability(
        available = false,
        reason = "EDGE unavailable: repository has no fall_detection.tflite plus a matching " +
            "source URL, SPDX license, SHA-256, input tensor specification, and fall-output semantics. " +
            "Add those verified artifacts before enabling a mobile runtime.",
    )

    override fun benchmarkLatencyMs(): Double = error(availability.reason)
    override fun detect(frame: JpegFrame): List<DetectionCandidate> = emptyList()
}

