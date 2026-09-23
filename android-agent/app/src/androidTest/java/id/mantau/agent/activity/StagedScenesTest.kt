package id.mantau.agent.activity

import android.os.Bundle
import android.util.Base64
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import id.mantau.agent.inference.FallEventGate
import id.mantau.agent.inference.fall.MediaPipeFallDetector
import id.mantau.agent.uplink.FallEvent
import id.mantau.agent.uplink.HttpEnvelopeTransport
import id.mantau.agent.uplink.JpegFrame
import id.mantau.agent.uplink.SignedEnvelope
import org.json.JSONObject
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.time.Instant

/**
 * Staged activity scenes (integration/stage_activity_clips.py, not committed) through
 * the real on-device path: MediaPipe pose with the idle gate, the fall rules and the
 * activity rules, exactly as MonitoringEngine wires them. Each scene is a list of
 * distinct frames and how long each is on screen; held frames are replayed twice a
 * second, like a still camera, stamped so the scene ends now.
 *
 * Arguments: `e2eScene` (still, sit, door, night) and `e2eSettings` (base64 of the
 * camera's DetectionSettings JSON). With `e2eServer`, `e2eAgentId`, `e2eSecret`,
 * `e2eCameraId`, `e2eSeq` every event is also signed and posted to a running
 * mantau-server, as the durable uplink would.
 */
@RunWith(AndroidJUnit4::class)
class StagedScenesTest {
    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val testAssets = instrumentation.context.assets
    private val args: Bundle = InstrumentationRegistry.getArguments()

    private fun frames(scene: String): List<Pair<String, Long>>? {
        val manifest = runCatching {
            testAssets.open("staged/$scene/manifest.csv").use { it.readBytes().decodeToString() }
        }.getOrNull() ?: return null
        val out = mutableListOf<Pair<String, Long>>()
        var t = 0L
        for (line in manifest.lines().filter { it.isNotBlank() }) {
            val (file, repeat, step) = line.split(",")
            repeat(repeat.toInt()) {
                out += file to t
                t += step.toLong()
            }
        }
        return out
    }

    @Test
    fun stagedSceneRaisesItsEvents() {
        val scene = args.getString("e2eScene").orEmpty()
        assumeTrue("pass -e e2eScene to replay a staged scene", scene.isNotBlank())
        val frames = frames(scene)
        assumeTrue("run integration/stage_activity_clips.py first", frames != null)
        val settings = ActivitySettings.parse(JSONObject(
            Base64.decode(requireNotNull(args.getString("e2eSettings")), Base64.DEFAULT).decodeToString(),
        ))
        val cameraId = args.getString("e2eCameraId") ?: "cam-1"
        val start = System.currentTimeMillis() - frames!!.last().second - 1_000

        val detector = MediaPipeFallDetector(instrumentation.targetContext)
        val gate = FallEventGate(cameraId)
        val activity = ActivityEngine(settings)
        val events = mutableListOf<FallEvent>()
        val cache = HashMap<String, ByteArray>()
        try {
            assertTrue(detector.availability.reason, detector.availability.available)
            for ((file, t) in frames) {
                val at = start + t
                val jpeg = cache.getOrPut(file) { testAssets.open("staged/$scene/$file").use { it.readBytes() } }
                val perception = detector.perceive(JpegFrame(jpeg, 0, 0, at))
                events += perception.candidates.mapNotNull { gate.accept(it, Instant.ofEpochMilli(at)) }
                perception.people?.let { people ->
                    events += activity.update(FrameObservation(cameraId, Instant.ofEpochMilli(at), people))
                        .map { it.toFallEvent() }
                }
            }
        } finally {
            detector.close()
        }
        val summary = events.map { "${it.kind}/${it.severity}@${(it.occurredAt.toEpochMilli() - start) / 1000.0}" }
        println("MANTAU_STAGED_$scene=$summary")

        val server = args.getString("e2eServer").orEmpty()
        if (server.isBlank()) return
        var sequence = requireNotNull(args.getString("e2eSeq")).toLong()
        for (event in events) {
            val envelope = SignedEnvelope.signEvent(
                requireNotNull(args.getString("e2eAgentId")), sequence++, Instant.now(), event,
                requireNotNull(args.getString("e2eSecret")),
            )
            assertTrue(HttpEnvelopeTransport().post(server, envelope))
        }
    }
}
