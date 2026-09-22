package id.mantau.agent.uplink

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.time.Instant

class GoldenProtocolTest {
    private val examples = File(requireNotNull(System.getProperty("mantau.protocol.examples")))
    private val secret = "golden-example-shared-secret-do-not-use"
    private val fixed = Instant.parse("2026-09-13T04:12:03.114000Z")

    @Test
    fun `fall envelope matches Python agent golden fixture byte for byte`() {
        val event = FallEvent(
            eventId = "ev-a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
            cameraId = "cam-kamar-ibu",
            occurredAt = fixed,
            confidence = 0.91,
            trackId = 3,
            signals = mapOf("velocity" to 0.52, "torso_angle_deg" to 84.0),
        )
        val actual = SignedEnvelope.signEvent("agent-3f9a1c2b", 42, fixed, event, secret).toJson()
        val golden = JSONObject(File(examples, "fall_event_envelope.json").readText())
        assertTrue(actual.similar(golden))
        assertEquals(golden.getString("sig"), actual.getString("sig"))
    }

    @Test
    fun `heartbeat envelope matches Python agent golden fixture byte for byte`() {
        val heartbeat = Heartbeat(
            agentId = "agent-3f9a1c2b", cameraId = "cam-kamar-ibu", sentAt = fixed,
            cameraReachable = true, detectorAlive = true, queueDepth = 0,
        )
        val actual = SignedEnvelope.signHeartbeat("agent-3f9a1c2b", 43, fixed, heartbeat, secret).toJson()
        val golden = JSONObject(File(examples, "heartbeat_envelope.json").readText())
        assertTrue(actual.similar(golden))
        assertEquals(golden.getString("sig"), actual.getString("sig"))
    }
}

