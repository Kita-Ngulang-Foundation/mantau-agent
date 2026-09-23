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
    val facts: CapabilityFacts? = null,
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
    fun run(detector: MobileDetector, cloudAvailable: Boolean = false): CapabilitySnapshot {
        val activity = context.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
        val memory = ActivityManager.MemoryInfo().also(activity::getMemoryInfo).totalMem
        val gpu = activity.deviceConfigurationInfo.glEsVersion
            ?.takeUnless { it == "0.0" }?.let { "opengl_es_$it" }
        val latency = if (detector.availability.available) runCatching { detector.benchmarkLatencyMs() }.getOrNull() else null
        return CapabilityFacts(
            architecture = Build.SUPPORTED_ABIS.firstOrNull() ?: "unknown",
            memoryBytes = memory,
            gpu = gpu,
            nnapiAvailable = Build.VERSION.SDK_INT >= 27,
            delegates = detector.availability.delegates,
            detectorBackend = detector.backend,
            detectorAvailable = detector.availability.available && latency != null,
            detectorReason = detector.availability.reason,
            inferenceLatencyMs = latency,
        ).snapshot(thermal.current(), cloudAvailable)
    }
}

/** Device and detector facts measured once; modes are re-derived when cloud or thermal state changes. */
data class CapabilityFacts(
    val architecture: String,
    val memoryBytes: Long,
    val gpu: String?,
    val nnapiAvailable: Boolean,
    val delegates: List<String>,
    val detectorBackend: String?,
    val detectorAvailable: Boolean,
    val detectorReason: String,
    val inferenceLatencyMs: Double?,
) {
    /** EDGE keeps up when one frame takes at most 1000 / [EDGE_MIN_FPS] ms. */
    val detectorFastEnough: Boolean
        get() = detectorAvailable && (inferenceLatencyMs ?: Double.MAX_VALUE) <= 1000.0 / EDGE_MIN_FPS

    fun snapshot(thermalState: ThermalState, cloudAvailable: Boolean): CapabilitySnapshot {
        val supported = buildList {
            add("AUTO")
            if (detectorAvailable) add("EDGE")
            if (cloudAvailable) add("CLOUD")
            if (detectorAvailable && cloudAvailable) add("HYBRID")
        }
        val recommended = when {
            detectorFastEnough && !thermalState.pressured -> "EDGE"
            cloudAvailable -> "CLOUD"
            detectorAvailable -> "EDGE"
            else -> "AUTO"
        }
        val reason = buildString {
            append("thermal=${thermalState.wireName}; ")
            append("detector=${if (detectorAvailable) detectorBackend else "unavailable"}; ")
            append("inference_latency_ms=${inferenceLatencyMs?.let { "%.2f".format(java.util.Locale.US, it) } ?: "unavailable"}; ")
            append("server_inference=${if (cloudAvailable) "available" else "unavailable"}; ")
            append(
                when {
                    !detectorAvailable && !cloudAvailable -> "$detectorReason No inference mode can run."
                    !detectorAvailable -> "$detectorReason Using server inference."
                    !detectorFastEnough && cloudAvailable -> "On-device detector is below ${EDGE_MIN_FPS.toInt()} fps; using server inference."
                    thermalState.pressured && cloudAvailable -> "Thermal pressure requires CLOUD fallback."
                    else -> "Verified detector benchmark is available for EDGE."
                },
            )
        }
        return CapabilitySnapshot(
            architecture, memoryBytes, gpu, nnapiAvailable, delegates, detectorBackend,
            detectorAvailable, inferenceLatencyMs, thermalState, supported, recommended, reason,
            facts = this,
        )
    }

    companion object {
        /** Same floor the fall rules need to track a person through a fall (mantau-AI EVALUATION.md). */
        const val EDGE_MIN_FPS = 10.0
    }
}
