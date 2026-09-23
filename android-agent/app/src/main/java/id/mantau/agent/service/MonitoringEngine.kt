package id.mantau.agent.service

import android.content.Context
import android.os.SystemClock
import id.mantau.agent.discovery.AndroidWsDiscovery
import id.mantau.agent.inference.AndroidH264JpegDecoder
import id.mantau.agent.inference.AndroidThermalStateProvider
import id.mantau.agent.inference.CapabilityBenchmark
import id.mantau.agent.inference.FallEventGate
import id.mantau.agent.inference.HybridConfirmationPolicy
import id.mantau.agent.inference.InferenceModePolicy
import id.mantau.agent.inference.ModeSelection
import id.mantau.agent.inference.fall.MediaPipeFallDetector
import id.mantau.agent.model.AgentConfig
import id.mantau.agent.model.CameraConfig
import id.mantau.agent.model.CameraConnectivity
import id.mantau.agent.model.DetectionSettingsPayload
import id.mantau.agent.model.CommandResult
import id.mantau.agent.model.ControlCommand
import id.mantau.agent.model.HealthState
import id.mantau.agent.model.RuntimeStatus
import id.mantau.agent.model.WirePayloads
import id.mantau.agent.model.optNullableString
import id.mantau.agent.network.CommandLedger
import id.mantau.agent.network.ControlPlaneClient
import id.mantau.agent.rtsp.LatestFrameBuffer
import id.mantau.agent.rtsp.ReconnectPolicy
import id.mantau.agent.rtsp.RtspAuthenticationException
import id.mantau.agent.rtsp.RtspClient
import id.mantau.agent.rtsp.RtspException
import id.mantau.agent.rtsp.RtspState
import id.mantau.agent.rtsp.VideoFormat
import id.mantau.agent.storage.AgentConfigStore
import id.mantau.agent.storage.AndroidKeystoreSecretStore
import id.mantau.agent.storage.RuntimeStatusStore
import id.mantau.agent.uplink.DurableUplink
import id.mantau.agent.uplink.FrameUploadPolicy
import id.mantau.agent.uplink.Heartbeat
import id.mantau.agent.uplink.LatestWorkQueue
import id.mantau.agent.uplink.SignedFrameUploader
import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong

class MonitoringEngine(
    private val context: Context,
    private val onStatus: (RuntimeStatus) -> Unit = {},
) : AutoCloseable {
    private val running = AtomicBoolean(false)
    private val lifecycle = MonitoringLifecycle()
    private val configStore = AgentConfigStore(context)
    private val statusStore = RuntimeStatusStore(context)
    private val secrets = AndroidKeystoreSecretStore(context)
    private val ledger = CommandLedger(context, secrets)
    private val control = ControlPlaneClient()
    private val discovery = AndroidWsDiscovery(context)
    private val frames = LatestFrameBuffer(2)
    private val detector = MediaPipeFallDetector(context)
    private val thermal = AndroidThermalStateProvider(context)
    private val capabilities = CapabilityBenchmark(context, thermal).run(detector)
    @Volatile private var detectorFailure: String? = null
    private val modePolicy = InferenceModePolicy(
        detectorAvailable = { capabilities.detectorAvailable && detectorFailure == null },
        detectorFailure = { detectorFailure ?: detector.availability.reason },
        thermal = thermal,
    )
    @Volatile private var selection = ModeSelection("AUTO", "CLOUD", capabilities.reason)
    private val frameQueue = LatestWorkQueue<id.mantau.agent.uplink.JpegFrame>(2)
    private val uploadPolicy = FrameUploadPolicy()
    private val frameUploader = SignedFrameUploader(
        serverUrl = { configStore.load().serverUrl },
        agentId = { configStore.load().agentId },
        secret = { requireNotNull(configStore.agentSecret()) },
    )
    private val durableUplink = DurableUplink(
        serverUrl = { configStore.load().serverUrl },
        agentId = { configStore.load().agentId },
        secret = { requireNotNull(configStore.agentSecret()) },
        stateDirectory = java.io.File(context.filesDir, "uplink"),
    )
    private val hybridPolicy = HybridConfirmationPolicy()
    private val uploadedFrames = AtomicLong()
    private val discardedFrames = AtomicLong()
    private val uploadFailures = AtomicLong()
    private var executor = Executors.newFixedThreadPool(4)
    @Volatile private var activeRtsp: RtspClient? = null
    @Volatile private var runtime = RuntimeStatus()
    private var lastPersistedFrameSecond = -1L

    @Synchronized
    fun start() {
        if (!lifecycle.beginStart()) return
        if (executor.isShutdown) executor = Executors.newFixedThreadPool(4)
        running.set(true)
        val config = configStore.load()
        selection = safeSelection(config.requestedInferenceMode)
        publish(RuntimeStatus(
            running = true,
            health = HealthState.ONLINE,
            rtspState = "connecting",
            effectiveInferenceMode = selection.effective,
            inferenceExplanation = selection.reason,
            thermalState = thermal.current().wireName,
        ))
        executor.submit(::captureLoop)
        executor.submit(::inferenceLoop)
        executor.submit(::uplinkLoop)
        executor.submit(::controlLoop)
        lifecycle.markRunning()
    }

    @Synchronized
    fun stop() {
        if (!lifecycle.beginStop()) return
        running.set(false)
        activeRtsp?.close()
        frameQueue.close()
        executor.shutdownNow()
        executor.awaitTermination(3, TimeUnit.SECONDS)
        publish(runtime.copy(
            running = false,
            health = HealthState.STOPPED,
            cameraConnectivity = CameraConnectivity.DISCONNECTED,
            rtspState = "stopped",
            explanation = null,
        ))
        lifecycle.markStopped()
    }

    override fun close() = stop()

    fun restartCapture() {
        activeRtsp?.close()
    }

    private fun captureLoop() {
        val reconnect = ReconnectPolicy()
        var reconnects = 0
        while (running.get()) {
            val config = safeConfig() ?: break
            val camera = config.camera
            if (camera == null) {
                publish(runtime.copy(
                    health = HealthState.DEGRADED,
                    cameraConnectivity = CameraConnectivity.UNKNOWN,
                    rtspState = "stopped",
                    explanation = "Camera configuration is required.",
                ))
                interruptibleSleep(1_000)
                continue
            }
            val rtsp = RtspClient()
            val decoder = AndroidH264JpegDecoder(uploadPolicy.maxWidth, uploadPolicy.jpegQuality)
            activeRtsp = rtsp
            try {
                rtsp.stream(camera, configStore.cameraPassword(), frames, { !running.get() }, object : RtspClient.Listener {
                    override fun onState(state: RtspState) {
                        publish(runtime.copy(
                            rtspState = state.name.lowercase(),
                            cameraConnectivity = if (state == RtspState.STREAMING) CameraConnectivity.CONNECTED
                                else runtime.cameraConnectivity,
                        ))
                    }

                    override fun onFormat(format: VideoFormat) {
                        decoder.configure(format)
                    }

                    override fun onFrame(frame: id.mantau.agent.rtsp.EncodedFrame) {
                        reconnect.reset()
                        // Feed every H.264 access unit; dropping compressed reference
                        // frames before decoding corrupts subsequent P/B frames.
                        decoder.decode(frame)?.let(frameQueue::offer)
                        val second = frame.capturedAt.epochSecond
                        if (second != lastPersistedFrameSecond) {
                            lastPersistedFrameSecond = second
                            publish(runtime.copy(
                                health = HealthState.ONLINE,
                                cameraConnectivity = CameraConnectivity.CONNECTED,
                                lastFrameAt = frame.capturedAt,
                                explanation = null,
                                rtspState = "streaming",
                            ))
                        }
                    }
                })
            } catch (exception: Exception) {
                if (!running.get()) break
                reconnects++
                publish(runtime.copy(
                    health = HealthState.DEGRADED,
                    cameraConnectivity = CameraConnectivity.DISCONNECTED,
                    rtspState = "backing_off",
                    reconnectCount = reconnects,
                    explanation = safeFailure("Camera connection failed", exception),
                ))
            } finally {
                decoder.close()
                frameQueue.clear()
                rtsp.close()
                if (activeRtsp === rtsp) activeRtsp = null
            }
            if (!running.get()) break
            interruptibleSleep(reconnect.nextDelayMs())
        }
    }

    private fun inferenceLoop() {
        var gateCameraId: String? = null
        var eventGate: FallEventGate? = null
        try {
            while (running.get()) {
                val jpeg = frameQueue.take() ?: break
                if (Instant.now().toEpochMilli() - jpeg.capturedAtMs > 5_000) {
                    discardedFrames.incrementAndGet()
                    continue
                }
                val mode = selection.effective
                if (mode == "CLOUD" && !uploadPolicy.ready(SystemClock.elapsedRealtime())) {
                    discardedFrames.incrementAndGet()
                    continue
                }
                try {
                    val config = configStore.load()
                    val camera = config.camera ?: continue
                    if (mode == "CLOUD") {
                        if (!uploadPolicy.admit(jpeg, SystemClock.elapsedRealtime())) {
                            discardedFrames.incrementAndGet()
                        } else if (frameUploader.upload(camera.cameraId, jpeg.bytes)) {
                            uploadedFrames.incrementAndGet()
                        } else {
                            // Frames are deliberately disposable: never enter the event spool.
                            uploadFailures.incrementAndGet()
                            discardedFrames.incrementAndGet()
                        }
                        continue
                    }

                    if (gateCameraId != camera.cameraId) {
                        gateCameraId = camera.cameraId
                        eventGate = FallEventGate(camera.cameraId)
                    }
                    val events = detector.detect(jpeg).mapNotNull { eventGate?.accept(it, Instant.ofEpochMilli(jpeg.capturedAtMs)) }
                    for (event in events) {
                        durableUplink.sendEvent(event)
                        if (mode == "HYBRID" && hybridPolicy.shouldUpload(event.eventId, jpeg.capturedAtMs)) {
                            // This branch remains unreachable until a server event-correlation contract exists.
                            if (!frameUploader.upload(camera.cameraId, jpeg.bytes)) uploadFailures.incrementAndGet()
                        }
                    }
                } catch (exception: Exception) {
                    detectorFailure = "Detector/decoder failure (${exception::class.java.simpleName}); using CLOUD."
                    selection = ModeSelection(selection.requested, "CLOUD", detectorFailure!!)
                    publish(runtime.copy(
                        effectiveInferenceMode = "CLOUD",
                        inferenceExplanation = detectorFailure,
                        health = HealthState.DEGRADED,
                    ))
                }
            }
        } finally {
            detector.close()
        }
    }

    private fun uplinkLoop() {
        var nextHeartbeatAt = 0L
        while (running.get()) {
            val config = safeConfig()
            val secret = runCatching { configStore.agentSecret() }.getOrNull()
            if (config != null && config.serverUrl.isNotBlank() && !secret.isNullOrBlank()) {
                val now = SystemClock.elapsedRealtime()
                if (now >= nextHeartbeatAt) {
                    if (config.requestedInferenceMode == "AUTO" || selection.effective in setOf("EDGE", "HYBRID")) {
                        selection = safeSelection(config.requestedInferenceMode)
                    }
                    val heartbeat = Heartbeat(
                        agentId = config.agentId,
                        cameraId = config.camera?.cameraId,
                        sentAt = Instant.now(),
                        cameraReachable = runtime.cameraConnectivity == CameraConnectivity.CONNECTED,
                        detectorAlive = selection.effective != "CLOUD" && detectorFailure == null,
                        queueDepth = durableUplink.depth(),
                    )
                    runCatching { durableUplink.sendHeartbeat(heartbeat) }
                    nextHeartbeatAt = now + HEARTBEAT_INTERVAL_MS
                } else {
                    runCatching { durableUplink.drain() }
                }
            }
            publish(runtime.copy(
                effectiveInferenceMode = selection.effective,
                inferenceExplanation = selection.reason,
                eventQueueDepth = durableUplink.depth(),
                uploadedFrames = uploadedFrames.get(),
                discardedFrames = discardedFrames.get() + frameQueue.dropped,
                uploadFailures = uploadFailures.get(),
                thermalState = thermal.current().wireName,
            ))
            interruptibleSleep(UPLINK_TICK_MS)
        }
    }

    private fun controlLoop() {
        while (running.get()) {
            val config = safeConfig()
            val secret = runCatching { configStore.agentSecret() }.getOrNull()
            if (config == null || config.serverUrl.isBlank() || secret.isNullOrBlank()) {
                publish(runtime.copy(
                    health = HealthState.DEGRADED,
                    explanation = "Control-plane enrollment is required.",
                ))
                interruptibleSleep(POLL_INTERVAL_MS)
                continue
            }
            try {
                val status = WirePayloads.status(
                    config, runtime, enrolled = true,
                    capabilities = WirePayloads.capabilities(capabilities.wireFacts()),
                )
                val command = control.poll(config.serverUrl, config.agentId, secret, status)
                if (!running.get()) break
                publish(runtime.copy(lastControlContactAt = Instant.now(), explanation = cameraExplanation()))
                if (command != null) handleCommand(config, secret, command)
            } catch (exception: Exception) {
                if (running.get()) publish(runtime.copy(
                    health = HealthState.DEGRADED,
                    explanation = safeFailure("Control-plane connection failed", exception),
                ))
            }
            interruptibleSleep(POLL_INTERVAL_MS)
        }
    }

    private fun handleCommand(config: AgentConfig, secret: String, delivered: ControlCommand) {
        ledger.completed(delivered.commandId)?.let {
            control.submitResult(config.serverUrl, config.agentId, secret, it)
            return
        }
        val recovered = ledger.inflight(delivered.commandId)
        val command = recovered ?: delivered.also(ledger::putInflight)
        if (recovered == null && Instant.parse(command.expiresAt).isBefore(Instant.now())) {
            val expired = CommandResult(
                command.commandId, "failed", "expired", "Command expired before execution.",
            )
            ledger.putCompleted(expired)
            control.submitResult(config.serverUrl, config.agentId, secret, expired)
            return
        }
        control.submitResult(
            config.serverUrl, config.agentId, secret,
            CommandResult(command.commandId, "running", completedAt = null),
        )
        val result = try {
            execute(command)
        } catch (exception: Exception) {
            CommandResult(
                command.commandId,
                "failed",
                classifyFailure(exception),
                safeFailure("Command failed", exception),
            )
        }
        ledger.putCompleted(result)
        control.submitResult(config.serverUrl, config.agentId, secret, result)
    }

    private fun execute(command: ControlCommand): CommandResult = when (command.type) {
        "discover" -> {
            val cameras = discovery.discover(cancelled = { !running.get() })
            val array = JSONArray().also { target -> cameras.forEach { target.put(it.toJson()) } }
            CommandResult(command.commandId, "succeeded", message = "Discovery completed.", data = JSONObject().put("cameras", array))
        }
        "camera_test" -> {
            val (camera, password) = cameraFromPayload(command.payload)
            RtspClient().use { it.captureOne(camera, password, cancelled = { !running.get() }) }
            CommandResult(command.commandId, "succeeded", message = "Camera connection succeeded.", data = JSONObject().put("success", true))
        }
        "configure_camera" -> {
            val (camera, password) = cameraFromPayload(command.payload)
            RtspClient().use { it.captureOne(camera, password, cancelled = { !running.get() }) }
            val current = configStore.load()
            configStore.save(current.copy(camera = camera), cameraPassword = password ?: "")
            restartCapture()
            CommandResult(command.commandId, "succeeded", message = "Camera configuration saved; capture restarted.")
        }
        "set_inference_mode" -> {
            val mode = command.payload.getString("mode")
            val selected = modePolicy.select(mode)
            val current = configStore.load()
            configStore.save(current.copy(requestedInferenceMode = mode))
            selection = selected
            frameQueue.clear()
            publish(runtime.copy(
                effectiveInferenceMode = selected.effective,
                inferenceExplanation = selected.reason,
            ))
            CommandResult(
                command.commandId, "succeeded", message = "Inference mode updated.",
                data = JSONObject().put("effective_mode", selected.effective).put("reason", selected.reason),
            )
        }
        "restart", "reconfigure" -> {
            restartCapture()
            CommandResult(
                command.commandId, "succeeded", message = "Android monitoring session restarted.",
                data = JSONObject().put("restart_requested", true),
            )
        }
        "apply_detection_settings" -> {
            val settings = DetectionSettingsPayload.parse(
                command.payload,
                expectedCameraId = configStore.load().camera?.cameraId,
            )
            configStore.saveDetectionSettings(settings.raw)
            CommandResult(
                command.commandId, "succeeded", message = "Detection settings stored.",
                data = JSONObject()
                    .put("camera_id", settings.cameraId)
                    .put("detection_settings_version", settings.version),
            )
        }
        else -> throw UnsupportedOperationException("Unsupported command")
    }

    private fun cameraFromPayload(payload: JSONObject): Pair<CameraConfig, String?> {
        val camera = CameraConfig(
            cameraId = payload.optString("camera_id", "cam-1"),
            name = payload.optString("name", "Home camera"),
            host = payload.getString("host"),
            port = payload.optInt("port", 554),
            mainPath = payload.optString("main_path", "/stream1"),
            subPath = payload.optNullableString("sub_path")?.takeIf(String::isNotBlank),
            username = payload.optNullableString("username")?.takeIf(String::isNotBlank),
        )
        camera.validate()
        return camera to payload.optNullableString("password")
    }

    private fun safeConfig(): AgentConfig? = try {
        configStore.load()
    } catch (exception: Exception) {
        publish(runtime.copy(
            health = HealthState.DEGRADED,
            explanation = safeFailure("Stored configuration is invalid", exception),
        ))
        null
    }

    private fun safeSelection(requested: String): ModeSelection = try {
        modePolicy.select(requested)
    } catch (exception: UnsupportedOperationException) {
        val fallback = modePolicy.select("CLOUD")
        fallback.copy(requested = requested, reason = "${exception.message} Falling back safely to CLOUD.")
    }

    private fun cameraExplanation(): String? =
        if (runtime.cameraConnectivity == CameraConnectivity.DISCONNECTED) runtime.explanation else null

    @Synchronized
    private fun publish(status: RuntimeStatus) {
        runtime = status
        runCatching { statusStore.write(status) }
        onStatus(status)
    }

    private fun interruptibleSleep(durationMs: Long) {
        if (durationMs <= 0) return
        try {
            Thread.sleep(durationMs)
        } catch (_: InterruptedException) {
            Thread.currentThread().interrupt()
        }
    }

    private fun classifyFailure(exception: Exception): String = when (exception) {
        is IllegalArgumentException, is org.json.JSONException -> "invalid_request"
        is UnsupportedOperationException -> "unsupported"
        is RtspAuthenticationException -> "authentication_failed"
        is RtspException, is java.io.IOException -> "camera_unreachable"
        else -> "execution_failed"
    }

    private fun safeFailure(prefix: String, exception: Exception): String = "$prefix (${exception::class.java.simpleName})."

    companion object {
        private const val POLL_INTERVAL_MS = 5_000L
        private const val HEARTBEAT_INTERVAL_MS = 30_000L
        private const val UPLINK_TICK_MS = 2_000L
    }
}
