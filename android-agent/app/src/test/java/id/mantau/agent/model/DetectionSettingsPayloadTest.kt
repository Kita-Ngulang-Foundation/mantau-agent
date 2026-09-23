package id.mantau.agent.model

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Test

class DetectionSettingsPayloadTest {
    private fun payload(camera: String = "cam-1", version: Int = 3) = JSONObject()
        .put("camera_id", camera)
        .put("settings", JSONObject().put("version", version).put("timezone", "Asia/Jakarta"))

    @Test
    fun acceptsSettingsForTheConfiguredCamera() {
        val parsed = DetectionSettingsPayload.parse(payload(), expectedCameraId = "cam-1")
        assertEquals(3, parsed.version)
        assertEquals("Asia/Jakarta", parsed.raw.getString("timezone"))
    }

    @Test(expected = IllegalArgumentException::class)
    fun rejectsAnotherCamera() {
        DetectionSettingsPayload.parse(payload(camera = "cam-9"), expectedCameraId = "cam-1")
    }

    @Test(expected = IllegalArgumentException::class)
    fun rejectsWhenNoCameraIsConfigured() {
        DetectionSettingsPayload.parse(payload(), expectedCameraId = null)
    }

    @Test(expected = IllegalArgumentException::class)
    fun rejectsAnInvalidVersion() {
        DetectionSettingsPayload.parse(payload(version = 0), expectedCameraId = "cam-1")
    }
}
