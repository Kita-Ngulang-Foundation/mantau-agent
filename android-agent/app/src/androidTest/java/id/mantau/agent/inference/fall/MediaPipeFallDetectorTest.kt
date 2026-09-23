package id.mantau.agent.inference.fall

import android.os.Bundle
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import id.mantau.agent.inference.FallEventGate
import id.mantau.agent.uplink.FallEvent
import id.mantau.agent.uplink.HttpEnvelopeTransport
import id.mantau.agent.uplink.JpegFrame
import id.mantau.agent.uplink.SignedEnvelope
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.time.Instant

/**
 * The real on-device path: bundled, hash-verified models loaded by MediaPipe Tasks
 * and ONNX Runtime for Android, fed recorded CCTV frames as JPEGs (as the RTSP decoder
 * produces them), through the event gate and the signer.
 *
 * Frames come from scripts/prepare-instrumentation-frames.sh (not committed); without
 * them only the model-loading checks run. Instrumentation arguments `e2eServer`,
 * `e2eAgentId`, `e2eSecret`, `e2eCameraId`, `e2eSeq` additionally post the detected
 * fall to a running mantau-server (from the emulator the host is 10.0.2.2).
 */
@RunWith(AndroidJUnit4::class)
class MediaPipeFallDetectorTest {
    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val testAssets = instrumentation.context.assets
    private val args: Bundle = InstrumentationRegistry.getArguments()

    private fun frames(clip: String): List<Pair<ByteArray, Long>>? {
        val names = testAssets.list("frames/$clip")?.filter { it.endsWith(".jpg") }?.sorted()
        if (names.isNullOrEmpty()) return null
        val (num, den) = testAssets.open("frames/$clip/fps.txt").use { it.readBytes().decodeToString() }
            .trim().split("/").map { it.toDouble() }
        val fps = num / den
        return names.mapIndexed { i, name ->
            testAssets.open("frames/$clip/$name").use { it.readBytes() } to (i * 1000.0 / fps).toLong()
        }
    }

    private fun run(clip: String, epochStartMs: Long, cameraId: String = "cam-1"): Pair<List<FallEvent>, Double>? {
        val frames = frames(clip) ?: return null
        val detector = MediaPipeFallDetector(instrumentation.targetContext)
        val gate = FallEventGate(cameraId)
        val events = mutableListOf<FallEvent>()
        val started = System.nanoTime()
        try {
            assertTrue(detector.availability.reason, detector.availability.available)
            for ((jpeg, t) in frames) {
                val at = epochStartMs + t
                val candidates = detector.detect(JpegFrame(jpeg, 0, 0, at))
                events += candidates.mapNotNull { gate.accept(it, Instant.ofEpochMilli(at)) }
            }
        } finally {
            detector.close()
        }
        val msPerFrame = (System.nanoTime() - started) / 1e6 / frames.size
        return events to msPerFrame
    }

    @Test
    fun verifiedModelsLoadAndBenchmark() {
        MediaPipeFallDetector(instrumentation.targetContext).use { detector ->
            assertTrue(detector.availability.reason, detector.availability.available)
            val latency = detector.benchmarkLatencyMs()
            assertTrue(latency > 0)
            println("MANTAU_ANDROID_BENCHMARK_MS=$latency")
        }
    }

    @Test
    fun realFallFramesProduceOneFallEvent() {
        val result = run("video1", 1_790_000_000_000L)
        assumeTrue("run scripts/prepare-instrumentation-frames.sh first", result != null)
        val (events, msPerFrame) = result!!
        println("MANTAU_ANDROID_VIDEO1_MS_PER_FRAME=$msPerFrame EVENTS=${events.map { it.confidence }}")
        assertEquals(1, events.size)
        assertEquals(setOf("velocity", "aspect_ratio", "torso_angle_deg"), events.single().signals.keys)
    }

    @Test
    fun walkingFramesProduceNoEvent() {
        val result = run("video5", 1_790_000_000_000L)
        assumeTrue("run scripts/prepare-instrumentation-frames.sh first", result != null)
        assertEquals(emptyList<FallEvent>(), result!!.first)
    }

    @Test
    fun detectedFallIsAcceptedByRunningServer() {
        val server = args.getString("e2eServer").orEmpty()
        assumeTrue("pass -e e2eServer to post to a live mantau-server", server.isNotBlank())
        val cameraId = args.getString("e2eCameraId") ?: "cam-1"
        val start = System.currentTimeMillis() - 5_000
        val result = run("video1", start, cameraId)
        assumeTrue(result != null)
        val event = result!!.first.single()
        val envelope = SignedEnvelope.signEvent(
            requireNotNull(args.getString("e2eAgentId")), requireNotNull(args.getString("e2eSeq")).toLong(),
            Instant.now(), event, requireNotNull(args.getString("e2eSecret")),
        )
        assertTrue(HttpEnvelopeTransport().post(server, envelope))
        println("MANTAU_ANDROID_E2E_EVENT_ID=${event.eventId}")
    }
}
