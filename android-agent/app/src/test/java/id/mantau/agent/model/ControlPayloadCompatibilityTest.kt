package id.mantau.agent.model

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.time.Instant

class ControlPayloadCompatibilityTest {
    private val fixtures = File(requireNotNull(System.getProperty("mantau.contract.fixtures")))

    @Test
    fun `command and result exactly round trip shared core fixtures`() {
        val commandFixture = fixture("command.json")
        val resultFixture = fixture("command_result.json")
        assertTrue(ControlCommand.fromJson(commandFixture).toJson().similar(commandFixture))
        assertTrue(CommandResult.fromJson(resultFixture).toJson().similar(resultFixture))
    }

    @Test
    fun `android capability report has the shared v1 field set`() {
        val coreKeys = fixture("capability_report.json").keySet()
        val payload = WirePayloads.capabilities(WirePayloads.CapabilityFacts(
            architecture = "arm64-v8a",
            cpu = "test-hardware",
            memoryBytes = 4_294_967_296,
            softwareVersion = "0.1.0",
        ))
        assertEquals(coreKeys, payload.keySet())
        assertEquals("android", payload.getString("platform"))
        assertEquals("CLOUD", payload.getString("recommended_mode"))
        assertEquals(listOf("AUTO", "CLOUD"), payload.getJSONArray("supported_inference_modes").toStringList())
        assertEquals(0, payload.getJSONArray("supported_detector_backends").length())
    }

    @Test
    fun `android status has the shared v1 field set and no secrets`() {
        val capability = WirePayloads.capabilities(WirePayloads.CapabilityFacts("arm64-v8a", "cpu", 1024, "0.1.0"))
        val status = WirePayloads.status(
            AgentConfig(agentId = "agent-android-1", name = "Spare phone"),
            RuntimeStatus(
                running = true,
                health = HealthState.ONLINE,
                cameraConnectivity = CameraConnectivity.CONNECTED,
                lastFrameAt = Instant.parse("2026-09-22T08:00:01Z"),
            ),
            enrolled = true,
            capabilities = capability,
        )
        assertEquals(fixture("agent_status.json").keySet(), status.keySet())
        assertEquals("android", status.getString("platform"))
        assertEquals(1, status.getInt("schema_version"))
        val lowered = status.toString().lowercase()
        listOf("password", "agent_secret", "private_key", "rtsp://", "credentials")
            .forEach { assertFalse(lowered.contains(it)) }
    }

    @Test
    fun `discovery result uses shared core fixture fields`() {
        val result = DiscoveredCamera("192.168.1.42", "Living room", subPath = "/stream2", reachable = true)
        assertEquals(fixture("discovery_result.json").keySet(), result.toJson().keySet())
    }

    private fun fixture(name: String) = JSONObject(File(fixtures, name).readText())

    private fun org.json.JSONArray.toStringList() = (0 until length()).map(::getString)
}
