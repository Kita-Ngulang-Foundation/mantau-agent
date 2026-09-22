package id.mantau.agent.model

import java.net.URI

data class CameraConfig(
    val cameraId: String = "cam-1",
    val name: String = "Home camera",
    val host: String,
    val port: Int = 554,
    val mainPath: String = "/stream1",
    val subPath: String? = null,
    val username: String? = null,
) {
    fun preferredPath(): String = subPath?.takeIf { it.isNotBlank() } ?: mainPath

    fun validate() {
        require(host.isNotBlank()) { "Camera host is required" }
        require(host.none { it.isWhitespace() } && !host.contains("/") && !host.contains("@")) {
            "Camera host must be a hostname or IP address"
        }
        require(port in 1..65535) { "RTSP port must be between 1 and 65535" }
        require(mainPath.startsWith('/')) { "Main RTSP path must start with /" }
        require(subPath == null || subPath.startsWith('/')) { "Substream path must start with /" }
    }
}

data class AgentConfig(
    val serverUrl: String = "",
    val agentId: String,
    val name: String = agentId,
    val claimCode: String? = null,
    val camera: CameraConfig? = null,
    val requestedInferenceMode: String = "AUTO",
) {
    fun validate(requireCamera: Boolean = false) {
        require(agentId.matches(Regex("[A-Za-z0-9._-]{3,128}"))) { "Agent ID is invalid" }
        require(name.isNotBlank()) { "Agent name is required" }
        if (serverUrl.isNotBlank()) {
            val uri = URI(serverUrl)
            require(uri.scheme in setOf("http", "https") && !uri.host.isNullOrBlank()) {
                "Server URL must be an http(s) URL"
            }
        }
        require(requestedInferenceMode == "AUTO") {
            "This Android build supports AUTO only; inference is not included"
        }
        if (requireCamera) requireNotNull(camera) { "Camera configuration is required" }
        camera?.validate()
    }
}

data class Enrollment(val agentId: String, val secret: String, val claimCode: String?)

enum class CameraConnectivity(val wireValue: String) {
    CONNECTED("connected"), DISCONNECTED("disconnected"), UNKNOWN("unknown")
}

enum class HealthState(val wireValue: String) {
    ONLINE("online"), DEGRADED("degraded"), STOPPED("stopped")
}

