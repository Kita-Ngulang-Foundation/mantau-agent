package id.mantau.agent.model

import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class AgentConfigTest {
    @Test
    fun `substream is preferred and camera validates`() {
        val camera = CameraConfig(host = "192.168.1.42", subPath = "/stream2")
        AgentConfig(
            serverUrl = "https://control.example.test",
            agentId = "agent-android-1",
            name = "Spare phone",
            camera = camera,
        ).validate(requireCamera = true)
        assertEquals("/stream2", camera.preferredPath())
    }

    @Test
    fun `main stream is fallback and invalid config fails closed`() {
        assertEquals("/stream1", CameraConfig(host = "camera.local").preferredPath())
        assertThrows(IllegalArgumentException::class.java) {
            CameraConfig(host = "rtsp://camera.local", port = 70_000).validate()
        }
        assertThrows(IllegalArgumentException::class.java) {
            AgentConfig(serverUrl = "file:///tmp/server", agentId = "agent-1").validate()
        }
    }

    @Test
    fun `shared inference modes are accepted and unknown modes are rejected`() {
        AgentConfig(agentId = "agent-1", requestedInferenceMode = "EDGE").validate()
        assertThrows(IllegalArgumentException::class.java) {
            AgentConfig(agentId = "agent-1", requestedInferenceMode = "MAGIC").validate()
        }
    }
}
