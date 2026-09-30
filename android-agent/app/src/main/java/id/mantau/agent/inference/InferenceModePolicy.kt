package id.mantau.agent.inference

import id.mantau.agent.model.AgentConfig

data class ModeSelection(val requested: String, val effective: String, val reason: String)

/**
 * Resolves the requested inference mode. Detection runs on the server only, so every mode
 * (AUTO, EDGE, CLOUD, HYBRID; all four stay valid so stored configurations keep loading)
 * resolves to CLOUD. Without server inference (`GET /inference/capability` says unavailable)
 * there is no local fallback: frames are discarded and health is degraded until it returns.
 */
class InferenceModePolicy(
    private val cloudAvailable: () -> Boolean = { false },
) {
    fun select(requested: String): ModeSelection {
        require(requested in AgentConfig.INFERENCE_MODES) { "Unknown inference mode" }
        val reason = if (cloudAvailable()) {
            "Detection runs on the server; this device uploads sampled frames (CLOUD)."
        } else {
            "Server inference is unavailable; frames are discarded and nothing is detected " +
                "until the server offers it (no on-device detection)."
        }
        return ModeSelection(requested, "CLOUD", reason)
    }
}
