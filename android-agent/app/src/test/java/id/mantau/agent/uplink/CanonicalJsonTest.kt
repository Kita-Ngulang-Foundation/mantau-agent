package id.mantau.agent.uplink

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Test
import java.time.Instant

/** Numbers must reach the server exactly as Python's json.dumps writes them. */
class CanonicalJsonTest {
    private val secret = "golden-example-shared-secret-do-not-use"
    private val fixed = Instant.parse("2026-09-13T04:12:03.114000Z")

    @Test
    fun `floats render like Python repr`() {
        // Expected strings produced by Python's json.dumps.
        val cases = listOf(
            30.0 to "30.0", 0.00012 to "0.00012", 1.2e-05 to "1.2e-05", 0.0001 to "0.0001",
            123.456 to "123.456", 1e16 to "1e+16", 1.5e16 to "1.5e+16",
            9999999999999998.0 to "9999999999999998.0", -0.5 to "-0.5", 0.1 to "0.1",
            2.0 / 3 to "0.6666666666666666", 31.305000000000003 to "31.305000000000003",
            5e-324 to "5e-324", 1.7976931348623157e308 to "1.7976931348623157e+308",
            -2.5e-7 to "-2.5e-07", 100.0 to "100.0", 0.1 + 0.2 to "0.30000000000000004",
            0.0 to "0.0", -0.0 to "-0.0",
        )
        for ((value, python) in cases) assertEquals("$value", python, CanonicalJson.pythonFloat(value))
    }

    @Test
    fun `activity event signature matches the Python agent`() {
        val event = FallEvent(
            eventId = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4", cameraId = "cam-kamar-ibu", occurredAt = fixed,
            confidence = 0.87, trackId = 2,
            signals = mapOf("duration_s" to 30.0, "movement" to 1.2e-05, "confidence" to 0.87),
            kind = "stillness", severity = "warning", zoneId = "bed",
        )
        val envelope = SignedEnvelope.signEvent("agent-3f9a1c2b", 44, fixed, event, secret)
        // From mantau_core: Envelope.for_event(...).sign(secret) on the same event.
        assertEquals("7d3bf211e956c341b458386c9de2d626a354e6f77eadca1a59610ff2c59312b6", envelope.signature)
    }

    @Test
    fun `sent and stored bytes keep the signed number forms`() {
        val event = FallEvent(
            eventId = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4", cameraId = "cam-kamar-ibu", occurredAt = fixed,
            confidence = 0.87, trackId = 2, signals = mapOf("duration_s" to 30.0, "movement" to 1.2e-05),
            kind = "stillness", severity = "warning",
        )
        val body = CanonicalJson.string(SignedEnvelope.signEvent("agent-3f9a1c2b", 44, fixed, event, secret).toJson())
        assert("\"duration_s\":30.0" in body && "\"movement\":1.2e-05" in body) { body }
        // A queued envelope read back from disk is sent byte for byte the same.
        assertEquals(body, CanonicalJson.string(JSONObject(body)))
    }
}
