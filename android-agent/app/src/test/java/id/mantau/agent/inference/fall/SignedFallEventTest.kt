package id.mantau.agent.inference.fall

import id.mantau.agent.inference.FallEventGate
import id.mantau.agent.uplink.CanonicalJson
import id.mantau.agent.uplink.FallEvent
import id.mantau.agent.uplink.HttpEnvelopeTransport
import id.mantau.agent.uplink.SignedEnvelope
import id.mantau.agent.uplink.hmacSha256
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.time.Instant

/**
 * Recorded poses of a real fall, through the on-device rules and classifier, the event
 * gate and the signer: the Android agent emits one signed `fall_event` shaped exactly
 * like the Python agent's.
 *
 * With `-Pmantau.e2e.server=<url> -Pmantau.e2e.agentId=<id> -Pmantau.e2e.secret=<secret>
 * -Pmantau.e2e.cameraId=<camera>` the envelope is also posted to a running mantau-server.
 */
class SignedFallEventTest {
    private fun fallEvents(sequenceName: String, cameraId: String, epochStartMs: Long): List<FallEvent> {
        val sequence = PoseSequences.load(sequenceName)
        val gate = FallEventGate(cameraId)
        return PoseSequences.classifier().use { classifier ->
            val stage = FallRulesStage(PoseSequences.config(sequence), classifier)
            PoseSequences.frames(sequence).flatMap { (t, people) ->
                val at = epochStartMs + t
                stage.update(people, at).orEmpty().mapNotNull { gate.accept(it, Instant.ofEpochMilli(at)) }
            }
        }
    }

    @Test
    fun recordedFallBecomesOneSignedEventWithPythonShapedPayload() {
        val start = 1_790_000_000_000L
        val events = fallEvents("fall_ybclass_video1", "cam-1", start)
        assertEquals(1, events.size)
        val expected = PoseSequences.load("fall_ybclass_video1").getJSONObject("expected")
            .getJSONObject("with_classifier").getJSONArray("events").getJSONObject(0)
        val event = events.single()
        assertEquals(expected.getDouble("confidence"), event.confidence, 0.0)
        assertEquals(expected.getInt("track_id"), event.trackId)
        assertEquals(Instant.ofEpochMilli(start + expected.getLong("timestamp_ms")), event.occurredAt)

        val envelope = SignedEnvelope.signEvent("agent-1", 7, Instant.ofEpochMilli(start + 3000), event, "s3cret")
        val payload = envelope.toJson().getJSONObject("payload")
        assertEquals("fall_event", envelope.kind)
        assertEquals("fall", payload.getString("kind"))
        assertEquals("critical", payload.getString("severity"))
        assertEquals(setOf("velocity", "aspect_ratio", "torso_angle_deg"),
            payload.getJSONObject("signals").keySet())
        assertEquals(hmacSha256("s3cret".toByteArray(), CanonicalJson.encode(envelope.unsignedJson())),
            envelope.signature)
    }

    @Test
    fun walkingProducesNoEvent() {
        assertTrue(fallEvents("walk_ybclass_video5", "cam-1", 1_790_000_000_000L).isEmpty())
    }

    @Test
    fun postsToRunningServerWhenConfigured() {
        val server = System.getProperty("mantau.e2e.server").orEmpty()
        assumeTrue("set -Pmantau.e2e.server to post to a live mantau-server", server.isNotBlank())
        val agentId = System.getProperty("mantau.e2e.agentId")
        val secret = System.getProperty("mantau.e2e.secret")
        val cameraId = System.getProperty("mantau.e2e.cameraId") ?: "cam-1"
        val now = System.currentTimeMillis()
        val event = fallEvents("fall_urfall_02", cameraId, now - 10_000).single()
        val sequence = System.getProperty("mantau.e2e.seq")?.toLong() ?: (now / 1000)
        val envelope = SignedEnvelope.signEvent(agentId, sequence, Instant.now(), event, secret)
        assertTrue("server rejected the Android agent's signed event", HttpEnvelopeTransport().post(server, envelope))
        println("E2E_ANDROID_EVENT_ID=${event.eventId}")
    }
}
