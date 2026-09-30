package id.mantau.agent.inference

import android.app.ActivityManager
import android.content.Context
import android.os.Build
import android.os.PowerManager
import id.mantau.agent.BuildConfig
import id.mantau.agent.model.WirePayloads

enum class ThermalState(val wireName: String, val pressured: Boolean) {
    UNAVAILABLE("unavailable", false), NONE("none", false), LIGHT("light", false),
    MODERATE("moderate", false), SEVERE("severe", true), CRITICAL("critical", true),
    EMERGENCY("emergency", true), SHUTDOWN("shutdown", true);
}

fun interface ThermalStateProvider { fun current(): ThermalState }

class AndroidThermalStateProvider(context: Context) : ThermalStateProvider {
    private val power = context.getSystemService(Context.POWER_SERVICE) as PowerManager

    override fun current(): ThermalState {
        if (Build.VERSION.SDK_INT < 29) return ThermalState.UNAVAILABLE
        return when (power.currentThermalStatus) {
            PowerManager.THERMAL_STATUS_NONE -> ThermalState.NONE
            PowerManager.THERMAL_STATUS_LIGHT -> ThermalState.LIGHT
            PowerManager.THERMAL_STATUS_MODERATE -> ThermalState.MODERATE
            PowerManager.THERMAL_STATUS_SEVERE -> ThermalState.SEVERE
            PowerManager.THERMAL_STATUS_CRITICAL -> ThermalState.CRITICAL
            PowerManager.THERMAL_STATUS_EMERGENCY -> ThermalState.EMERGENCY
            PowerManager.THERMAL_STATUS_SHUTDOWN -> ThermalState.SHUTDOWN
            else -> ThermalState.UNAVAILABLE
        }
    }
}

/**
 * Device facts for the capability report. Detection runs on the server only (CLOUD), so the
 * report lists no detector backend and always recommends CLOUD.
 */
data class DeviceCapabilities(
    val architecture: String,
    val memoryBytes: Long,
    val gpu: String?,
    val nnapiAvailable: Boolean,
) {
    fun wireFacts(thermalState: ThermalState, serverInference: Boolean): WirePayloads.CapabilityFacts =
        WirePayloads.CapabilityFacts(
            architecture = architecture,
            cpu = Build.HARDWARE.ifBlank { Build.BOARD },
            memoryBytes = memoryBytes,
            softwareVersion = BuildConfig.VERSION_NAME,
            availableAccelerators = buildList {
                gpu?.let { add(it) }
                if (nnapiAvailable) add("android_nnapi_api")
            },
            recommendedMode = "CLOUD",
            supportedInferenceModes = SUPPORTED_MODES,
            recommendationReason = "thermal=${thermalState.wireName}; " +
                "server_inference=${if (serverInference) "available" else "unavailable"}; " +
                if (serverInference) "The server runs detection on uploaded frames."
                else "Server inference is unavailable; no detection until the server offers it.",
        )

    companion object {
        val SUPPORTED_MODES = listOf("AUTO", "CLOUD")

        fun read(context: Context): DeviceCapabilities {
            val activity = context.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
            val memory = ActivityManager.MemoryInfo().also(activity::getMemoryInfo).totalMem
            val gpu = activity.deviceConfigurationInfo.glEsVersion
                ?.takeUnless { it == "0.0" }?.let { "opengl_es_$it" }
            return DeviceCapabilities(
                architecture = Build.SUPPORTED_ABIS.firstOrNull() ?: "unknown",
                memoryBytes = memory,
                gpu = gpu,
                nnapiAvailable = Build.VERSION.SDK_INT >= 27,
            )
        }
    }
}
