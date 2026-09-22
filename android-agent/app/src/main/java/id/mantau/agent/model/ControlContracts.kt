package id.mantau.agent.model

import android.app.ActivityManager
import android.content.Context
import android.os.Build
import id.mantau.agent.BuildConfig
import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant

data class RuntimeStatus(
    val running: Boolean = false,
    val health: HealthState = HealthState.STOPPED,
    val cameraConnectivity: CameraConnectivity = CameraConnectivity.UNKNOWN,
    val lastFrameAt: Instant? = null,
    val lastControlContactAt: Instant? = null,
    val explanation: String? = null,
    val rtspState: String = "stopped",
    val reconnectCount: Int = 0,
)

data class ControlCommand(
    val commandId: String,
    val type: String,
    val state: String,
    val payload: JSONObject,
    val createdAt: String,
    val expiresAt: String,
) {
    fun toJson(): JSONObject = JSONObject()
        .put("schema_version", 1)
        .put("command_id", commandId)
        .put("command_type", type)
        .put("state", state)
        .put("payload", payload)
        .put("created_at", createdAt)
        .put("expires_at", expiresAt)

    companion object {
        fun fromJson(value: JSONObject) = ControlCommand(
            commandId = value.getString("command_id"),
            type = value.getString("command_type"),
            state = value.getString("state"),
            payload = value.optJSONObject("payload") ?: JSONObject(),
            createdAt = value.getString("created_at"),
            expiresAt = value.getString("expires_at"),
        )
    }
}

data class CommandResult(
    val commandId: String,
    val state: String,
    val failureReason: String? = null,
    val message: String? = null,
    val data: JSONObject = JSONObject(),
    val completedAt: String? = Instant.now().toString(),
) {
    fun toJson(): JSONObject = JSONObject()
        .put("schema_version", 1)
        .put("command_id", commandId)
        .put("state", state)
        .put("failure_reason", failureReason ?: JSONObject.NULL)
        .put("message", message ?: JSONObject.NULL)
        .put("data", data)
        .put("completed_at", completedAt ?: JSONObject.NULL)

    companion object {
        fun fromJson(value: JSONObject) = CommandResult(
            commandId = value.getString("command_id"),
            state = value.getString("state"),
            failureReason = value.optNullableString("failure_reason"),
            message = value.optNullableString("message"),
            data = value.optJSONObject("data") ?: JSONObject(),
            completedAt = value.optNullableString("completed_at"),
        )
    }
}

data class DiscoveredCamera(
    val host: String,
    val name: String? = null,
    val port: Int = 554,
    val mainPath: String = "/stream1",
    val subPath: String? = null,
    val reachable: Boolean = false,
    val failureReason: String? = null,
) {
    fun toJson(): JSONObject = JSONObject()
        .put("schema_version", 1)
        .put("host", host)
        .put("name", name ?: JSONObject.NULL)
        .put("port", port)
        .put("main_path", mainPath)
        .put("sub_path", subPath ?: JSONObject.NULL)
        .put("rtsp_reachable", reachable)
        .put("failure_reason", failureReason ?: JSONObject.NULL)
}

object WirePayloads {
    data class CapabilityFacts(
        val architecture: String,
        val cpu: String,
        val memoryBytes: Long,
        val softwareVersion: String,
    )

    fun capabilities(context: Context): JSONObject {
        val memory = (context.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager)
            .let { manager -> ActivityManager.MemoryInfo().also(manager::getMemoryInfo).totalMem }
        return capabilities(CapabilityFacts(
            architecture = Build.SUPPORTED_ABIS.firstOrNull() ?: "unknown",
            cpu = Build.HARDWARE.ifBlank { Build.BOARD },
            memoryBytes = memory,
            softwareVersion = BuildConfig.VERSION_NAME,
        ))
    }

    fun capabilities(facts: CapabilityFacts): JSONObject {
        return JSONObject()
            .put("schema_version", 1)
            .put("platform", "android")
            .put("architecture", facts.architecture)
            .put("cpu", facts.cpu)
            .put("memory_bytes", facts.memoryBytes)
            .put("available_accelerators", JSONArray())
            .put("supported_detector_backends", JSONArray())
            .put("software_version", facts.softwareVersion)
            .put("recommended_mode", "AUTO")
            .put("supported_inference_modes", JSONArray().put("AUTO"))
            .put("recommendation_reason", "This release captures CCTV locally; inference is not included.")
    }

    fun status(context: Context, config: AgentConfig, runtime: RuntimeStatus, enrolled: Boolean): JSONObject =
        status(config, runtime, enrolled, capabilities(context))

    fun status(config: AgentConfig, runtime: RuntimeStatus, enrolled: Boolean, capabilities: JSONObject): JSONObject =
        JSONObject()
            .put("schema_version", 1)
            .put("agent_id", config.agentId)
            .put("name", config.name)
            .put("platform", "android")
            .put("claim_status", if (enrolled) "pending" else "unclaimed")
            .put("setup_status", when {
                !enrolled -> "not_started"
                config.camera == null -> "waiting_for_agent"
                runtime.running -> "active"
                else -> "configuring_camera"
            })
            .put("health_state", runtime.health.wireValue)
            .put("requested_inference_mode", "AUTO")
            .put("effective_inference_mode", "AUTO")
            .put("capabilities", capabilities)
            .put("camera_connectivity", runtime.cameraConnectivity.wireValue)
            .put("last_heartbeat_at", runtime.lastControlContactAt?.toString() ?: JSONObject.NULL)
            .put("last_frame_at", runtime.lastFrameAt?.toString() ?: JSONObject.NULL)
            .put("health_explanation", runtime.explanation ?: JSONObject.NULL)
}

fun JSONObject.optNullableString(name: String): String? =
    if (!has(name) || isNull(name)) null else getString(name)
