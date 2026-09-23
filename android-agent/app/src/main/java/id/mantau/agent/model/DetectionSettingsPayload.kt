package id.mantau.agent.model

import org.json.JSONObject

/**
 * The `apply_detection_settings` payload: `{"camera_id", "settings"}`.
 * Only the fields the agent must check are validated here; the full model is
 * validated by the server before it is ever queued.
 */
data class DetectionSettingsPayload(val cameraId: String, val version: Int, val raw: JSONObject) {
    companion object {
        fun parse(payload: JSONObject, expectedCameraId: String?): DetectionSettingsPayload {
            val cameraId = payload.getString("camera_id")
            require(expectedCameraId != null && cameraId == expectedCameraId) {
                "Settings are for a different camera"
            }
            val settings = payload.getJSONObject("settings")
            val version = settings.getInt("version")
            require(version >= 1) { "Invalid settings version" }
            return DetectionSettingsPayload(cameraId, version, settings)
        }
    }
}
