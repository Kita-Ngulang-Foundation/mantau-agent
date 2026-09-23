package id.mantau.agent.inference

import id.mantau.agent.inference.fall.PersonObservation
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

/**
 * One frame's result: fall candidates plus who is where for the activity rules.
 * [people] is null when nothing is known about this frame (it was skipped), which is
 * different from an empty list (pose ran and saw nobody).
 */
data class FramePerception(
    val candidates: List<DetectionCandidate>,
    val people: List<PersonObservation>?,
)

interface MobileDetector : AutoCloseable {
    val backend: String
    val availability: DetectorAvailability
    fun benchmarkLatencyMs(): Double
    fun detect(frame: JpegFrame): List<DetectionCandidate> = perceive(frame).candidates
    fun perceive(frame: JpegFrame): FramePerception
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
    override fun perceive(frame: JpegFrame) = FramePerception(emptyList(), null)
}

