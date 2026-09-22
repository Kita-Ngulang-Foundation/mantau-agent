package id.mantau.agent.inference

import android.app.ActivityManager
import android.content.Context
import android.os.Build
import android.os.PowerManager
import id.mantau.agent.BuildConfig
import id.mantau.agent.model.WirePayloads

data class CapabilitySnapshot(
    val architecture: String,
    val memoryBytes: Long,
    val gpu: String?,
    val nnapiAvailable: Boolean,
    val delegates: List<String>,
    val detectorBackend: String?,
    val detectorAvailable: Boolean,
    val inferenceLatencyMs: Double?,
    val thermalState: ThermalState,
    val supportedModes: List<String>,
    val recommendedMode: String,
    val reason: String,
) {
    fun wireFacts(): WirePayloads.CapabilityFacts = WirePayloads.CapabilityFacts(
        architecture = architecture,
        cpu = Build.HARDWARE.ifBlank { Build.BOARD },
        memoryBytes = memoryBytes,
        softwareVersion = BuildConfig.VERSION_NAME,
        availableAccelerators = buildList {
            gpu?.let { add(it) }
            if (nnapiAvailable) add("android_nnapi_api")
            addAll(delegates)
        },
        supportedDetectorBackends = detectorBackend?.takeIf { detectorAvailable }?.let(::listOf) ?: emptyList(),
        recommendedMode = recommendedMode,
        supportedInferenceModes = supportedModes,
        recommendationReason = reason,
    )
}

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

class CapabilityBenchmark(
    private val context: Context,
    private val thermal: ThermalStateProvider = AndroidThermalStateProvider(context),
) {
    fun run(detector: MobileDetector): CapabilitySnapshot {
        val activity = context.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
        val memory = ActivityManager.MemoryInfo().also(activity::getMemoryInfo).totalMem
        val gpu = activity.deviceConfigurationInfo.glEsVersion
            ?.takeUnless { it == "0.0" }?.let { "opengl_es_$it" }
        val thermalState = thermal.current()
        val latency = if (detector.availability.available) runCatching { detector.benchmarkLatencyMs() }.getOrNull() else null
        val detectorReady = detector.availability.available && latency != null
        val supported = buildList {
            add("AUTO")
            add("CLOUD")
            if (detectorReady) add("EDGE")
            // HYBRID also requires an event-correlation/confirmation server contract.
        }
        val recommended = if (detectorReady && !thermalState.pressured) "EDGE" else "CLOUD"
        val reason = buildString {
            append("thermal=${thermalState.wireName}; ")
            append("detector=${if (detectorReady) detector.backend else "unavailable"}; ")
            append("inference_latency_ms=${latency?.let { "%.2f".format(java.util.Locale.US, it) } ?: "unavailable"}; ")
            if (!detectorReady) append(detector.availability.reason)
            else if (thermalState.pressured) append("Thermal pressure requires CLOUD fallback.")
            else append("Verified detector benchmark is available for EDGE.")
        }
        return CapabilitySnapshot(
            architecture = Build.SUPPORTED_ABIS.firstOrNull() ?: "unknown",
            memoryBytes = memory,
            gpu = gpu,
            nnapiAvailable = Build.VERSION.SDK_INT >= 27,
            delegates = detector.availability.delegates,
            detectorBackend = detector.backend,
            detectorAvailable = detectorReady,
            inferenceLatencyMs = latency,
            thermalState = thermalState,
            supportedModes = supported,
            recommendedMode = recommended,
            reason = reason,
        )
    }
}

