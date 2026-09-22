package id.mantau.agent.storage

import android.content.Context
import id.mantau.agent.model.CameraConnectivity
import id.mantau.agent.model.HealthState
import id.mantau.agent.model.RuntimeStatus
import java.time.Instant

class RuntimeStatusStore(context: Context) {
    private val preferences = context.getSharedPreferences("runtime_status", Context.MODE_PRIVATE)

    @Synchronized
    fun read(): RuntimeStatus = RuntimeStatus(
        running = preferences.getBoolean("running", false),
        health = enumValueOr(HealthState.STOPPED, preferences.getString("health", null)),
        cameraConnectivity = enumValueOr(CameraConnectivity.UNKNOWN, preferences.getString("camera", null)),
        lastFrameAt = preferences.getString("last_frame", null)?.let(Instant::parse),
        lastControlContactAt = preferences.getString("last_control", null)?.let(Instant::parse),
        explanation = preferences.getString("explanation", null),
        rtspState = preferences.getString("rtsp_state", "stopped") ?: "stopped",
        reconnectCount = preferences.getInt("reconnect_count", 0),
        effectiveInferenceMode = preferences.getString("effective_mode", "CLOUD") ?: "CLOUD",
        inferenceExplanation = preferences.getString("inference_explanation", null),
        eventQueueDepth = preferences.getInt("event_queue_depth", 0),
        uploadedFrames = preferences.getLong("uploaded_frames", 0),
        discardedFrames = preferences.getLong("discarded_frames", 0),
        uploadFailures = preferences.getLong("upload_failures", 0),
        thermalState = preferences.getString("thermal_state", "unavailable") ?: "unavailable",
    )

    @Synchronized
    fun write(status: RuntimeStatus) {
        check(preferences.edit()
            .putBoolean("running", status.running)
            .putString("health", status.health.name)
            .putString("camera", status.cameraConnectivity.name)
            .putString("last_frame", status.lastFrameAt?.toString())
            .putString("last_control", status.lastControlContactAt?.toString())
            .putString("explanation", status.explanation)
            .putString("rtsp_state", status.rtspState)
            .putInt("reconnect_count", status.reconnectCount)
            .putString("effective_mode", status.effectiveInferenceMode)
            .putString("inference_explanation", status.inferenceExplanation)
            .putInt("event_queue_depth", status.eventQueueDepth)
            .putLong("uploaded_frames", status.uploadedFrames)
            .putLong("discarded_frames", status.discardedFrames)
            .putLong("upload_failures", status.uploadFailures)
            .putString("thermal_state", status.thermalState)
            .commit()) { "Could not persist runtime status" }
    }

    private inline fun <reified T : Enum<T>> enumValueOr(fallback: T, raw: String?): T =
        raw?.let { runCatching { enumValueOf<T>(it) }.getOrNull() } ?: fallback
}
