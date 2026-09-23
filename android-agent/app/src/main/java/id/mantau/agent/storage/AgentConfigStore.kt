package id.mantau.agent.storage

import android.content.Context
import id.mantau.agent.model.AgentConfig
import id.mantau.agent.model.CameraConfig
import org.json.JSONObject
import java.util.UUID

class AgentConfigStore(
    context: Context,
    private val secrets: SecretStore = AndroidKeystoreSecretStore(context),
) {
    private val preferences = context.getSharedPreferences("agent_configuration", Context.MODE_PRIVATE)

    fun load(): AgentConfig {
        val fallbackId = preferences.getString("agent_id", null)
            ?: "agent-android-${UUID.randomUUID()}".also {
                preferences.edit().putString("agent_id", it).commit()
            }
        val raw = preferences.getString("configuration", null) ?: return AgentConfig(agentId = fallbackId)
        val json = JSONObject(raw)
        val cameraJson = json.optJSONObject("camera")
        return AgentConfig(
            serverUrl = json.optString("server_url"),
            agentId = json.optString("agent_id", fallbackId),
            name = json.optString("name", fallbackId),
            claimCode = json.optString("claim_code").takeIf(String::isNotBlank),
            camera = cameraJson?.let {
                CameraConfig(
                    cameraId = it.optString("camera_id", "cam-1"),
                    name = it.optString("name", "Home camera"),
                    host = it.getString("host"),
                    port = it.optInt("port", 554),
                    mainPath = it.optString("main_path", "/stream1"),
                    subPath = it.optString("sub_path").takeIf(String::isNotBlank),
                    username = it.optString("username").takeIf(String::isNotBlank),
                )
            },
            requestedInferenceMode = json.optString("requested_inference_mode", "AUTO"),
        ).also { it.validate() }
    }

    fun save(config: AgentConfig, agentSecret: String? = null, cameraPassword: String? = null) {
        config.validate()
        val camera = config.camera?.let {
            JSONObject()
                .put("camera_id", it.cameraId)
                .put("name", it.name)
                .put("host", it.host)
                .put("port", it.port)
                .put("main_path", it.mainPath)
                .put("sub_path", it.subPath ?: "")
                .put("username", it.username ?: "")
        }
        val json = JSONObject()
            .put("schema_version", 1)
            .put("server_url", config.serverUrl.trimEnd('/'))
            .put("agent_id", config.agentId)
            .put("name", config.name)
            .put("claim_code", config.claimCode ?: "")
            .put("camera", camera ?: JSONObject.NULL)
            .put("requested_inference_mode", config.requestedInferenceMode)
        check(preferences.edit().putString("agent_id", config.agentId)
            .putString("configuration", json.toString()).commit()) { "Could not persist configuration" }
        agentSecret?.let { secrets.put(AGENT_SECRET, it) }
        if (cameraPassword != null) secrets.put(CAMERA_PASSWORD, cameraPassword)
    }

    /**
     * Detection settings from the Mantau app, stored verbatim so they survive
     * restarts. The activity rules that use them run in the shared Python
     * engine; on Android they take effect once cloud inference is enabled.
     */
    fun saveDetectionSettings(settings: JSONObject) {
        check(preferences.edit().putString("detection_settings", settings.toString()).commit()) {
            "Could not persist detection settings"
        }
    }

    fun detectionSettings(): JSONObject? =
        preferences.getString("detection_settings", null)?.let(::JSONObject)

    fun agentSecret(): String? = secrets.get(AGENT_SECRET)
    fun cameraPassword(): String? = secrets.get(CAMERA_PASSWORD)

    companion object {
        const val AGENT_SECRET = "agent_secret"
        const val CAMERA_PASSWORD = "camera_password"
    }
}

