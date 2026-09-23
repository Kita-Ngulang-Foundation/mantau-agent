package id.mantau.agent

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Typeface
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.InputType
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import id.mantau.agent.discovery.AndroidWsDiscovery
import id.mantau.agent.model.AgentConfig
import id.mantau.agent.model.CameraConfig
import id.mantau.agent.model.DiscoveredCamera
import id.mantau.agent.network.AlreadyClaimedException
import id.mantau.agent.network.ControlPlaneClient
import id.mantau.agent.service.MonitoringService
import id.mantau.agent.storage.AgentConfigStore
import id.mantau.agent.storage.RuntimeStatusStore
import java.util.concurrent.Executors

class MainActivity : Activity() {
    private lateinit var store: AgentConfigStore
    private lateinit var runtimeStore: RuntimeStatusStore
    private val worker = Executors.newSingleThreadExecutor()
    private val handler = Handler(Looper.getMainLooper())

    private lateinit var server: EditText
    private lateinit var agentId: TextView
    private lateinit var agentName: EditText
    private lateinit var claimCode: TextView
    private lateinit var cameraHost: EditText
    private lateinit var cameraPort: EditText
    private lateinit var mainPath: EditText
    private lateinit var subPath: EditText
    private lateinit var username: EditText
    private lateinit var password: EditText
    private lateinit var status: TextView
    private var startAfterPermission = false

    private val refresh = object : Runnable {
        override fun run() {
            val current = runCatching { runtimeStore.read() }.getOrNull()
            if (current != null) {
                status.text = buildString {
                    append(if (current.running) "Service running" else "Service stopped")
                    append("\nCamera: ${current.cameraConnectivity.wireValue} (${current.rtspState})")
                    append("\nInference: ${current.effectiveInferenceMode} · thermal ${current.thermalState}")
                    current.inferenceExplanation?.let { append("\n$it") }
                    current.lastFrameAt?.let { append("\nLast frame: $it") }
                    current.lastControlContactAt?.let { append("\nControl plane: $it") }
                    append("\nFrames uploaded/discarded: ${current.uploadedFrames}/${current.discardedFrames}")
                    append(" · failures ${current.uploadFailures}")
                    append("\nDurable event queue: ${current.eventQueueDepth}")
                    if (current.reconnectCount > 0) append("\nReconnects: ${current.reconnectCount}")
                    current.explanation?.let { append("\n$it") }
                }
            }
            handler.postDelayed(this, 1_000)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        store = AgentConfigStore(this)
        runtimeStore = RuntimeStatusStore(this)
        setContentView(buildUi())
        loadUi()
        handler.post(refresh)
    }

    override fun onDestroy() {
        handler.removeCallbacksAndMessages(null)
        worker.shutdownNow()
        super.onDestroy()
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == NOTIFICATION_PERMISSION && startAfterPermission) {
            startAfterPermission = false
            startMonitoring()
            if (grantResults.firstOrNull() != PackageManager.PERMISSION_GRANTED) {
                showMessage("Monitoring started. Android may hide the notification from the drawer because notification permission was denied; it remains visible in Active apps.")
            }
        }
    }

    private fun buildUi(): ScrollView {
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(20), dp(20), dp(32))
        }
        root.addView(TextView(this).apply {
            text = "Mantau Agent"
            textSize = 28f
            setTypeface(typeface, Typeface.BOLD)
        })
        root.addView(TextView(this).apply {
            text = "This phone stays at home and connects to CCTV over local Wi‑Fi. It is separate from the Mantau family app."
            textSize = 15f
            setPadding(0, dp(4), 0, dp(16))
        })

        root.section("Control plane")
        server = root.field("Server URL", InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI)
        agentId = TextView(this).also { root.addView(it, matchWrap()) }
        agentName = root.field("Agent name")
        claimCode = TextView(this).apply {
            textSize = 24f
            typeface = Typeface.MONOSPACE
            setTextIsSelectable(true)
            setPadding(0, dp(8), 0, dp(8))
        }.also { root.addView(it, matchWrap()) }
        root.addView(button("Enroll / get claim code") { enrollOrRefreshClaimCode() })
        root.addView(button("Rotate agent key") { confirmRotation() })

        root.section("Camera (manual fallback)")
        cameraHost = root.field("Camera IP or hostname")
        cameraPort = root.field("RTSP port", InputType.TYPE_CLASS_NUMBER)
        mainPath = root.field("Main stream path")
        subPath = root.field("Preferred low-bitrate substream path")
        username = root.field("Camera username")
        password = root.field(
            "Camera password (leave blank to keep saved value)",
            InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD,
        )
        root.addView(button("Discover ONVIF cameras on Wi‑Fi") { discover() })
        root.addView(button("Save configuration") { saveFromUi(showConfirmation = true) })

        root.section("Monitoring")
        val actions = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        actions.addView(button("Start") { requestNotificationThenStart() }, LinearLayout.LayoutParams(0, dp(48), 1f))
        actions.addView(button("Stop") { stopMonitoring() }, LinearLayout.LayoutParams(0, dp(48), 1f).apply { marginStart = dp(8) })
        root.addView(actions, matchWrap())
        status = TextView(this).apply {
            textSize = 14f
            setPadding(0, dp(12), 0, 0)
        }.also { root.addView(it, matchWrap()) }
        return ScrollView(this).apply { addView(root) }
    }

    private fun loadUi() {
        val config = store.load()
        server.setText(config.serverUrl.ifBlank { "http://192.168.1.2:8100" })
        agentId.text = "Agent ID: ${config.agentId}"
        agentName.setText(config.name)
        claimCode.text = config.claimCode?.let { "Claim code: $it" } ?: "Not enrolled"
        config.camera?.let {
            cameraHost.setText(it.host)
            cameraPort.setText(it.port.toString())
            mainPath.setText(it.mainPath)
            subPath.setText(it.subPath.orEmpty())
            username.setText(it.username.orEmpty())
        } ?: run {
            cameraPort.setText("554")
            mainPath.setText("/stream1")
        }
    }

    private fun saveFromUi(showConfirmation: Boolean): AgentConfig {
        val existing = store.load()
        val host = cameraHost.text.toString().trim()
        val camera = host.takeIf(String::isNotBlank)?.let {
            CameraConfig(
                cameraId = existing.camera?.cameraId ?: "${existing.agentId}-cam",
                name = existing.camera?.name ?: "Home camera",
                host = host,
                port = cameraPort.text.toString().toIntOrNull() ?: 554,
                mainPath = mainPath.text.toString().trim().ifBlank { "/stream1" },
                subPath = subPath.text.toString().trim().takeIf(String::isNotBlank),
                username = username.text.toString().trim().takeIf(String::isNotBlank),
            )
        }
        val config = existing.copy(
            serverUrl = server.text.toString().trim().trimEnd('/'),
            name = agentName.text.toString().trim(),
            camera = camera,
        )
        config.validate()
        val enteredPassword = password.text.toString()
        store.save(config, cameraPassword = enteredPassword.takeIf(String::isNotEmpty))
        password.text.clear()
        if (showConfirmation) showMessage("Configuration saved. Restart monitoring to apply camera changes.")
        return config
    }

    /**
     * First use enrolls. Afterwards only the claim code is refreshed, with the
     * agent's own secret -- the secret itself never changes here.
     */
    private fun enrollOrRefreshClaimCode() {
        val secret = store.agentSecret()
        if (secret == null) {
            enroll(currentSecret = null)
            return
        }
        val config = runCatching { saveFromUi(showConfirmation = false) }
            .getOrElse { showError("Cannot refresh claim code", it); return }
        status.text = "Requesting a claim code…"
        worker.submit {
            runCatching { ControlPlaneClient().refreshClaimCode(config.serverUrl, config.agentId, secret) }
                .onSuccess { code ->
                    store.save(config.copy(claimCode = code))
                    runOnUiThread {
                        claimCode.text = "Claim code: $code"
                        showMessage("Enter this claim code in the Mantau app within a few minutes. It works once.")
                    }
                }
                .onFailure { error ->
                    runOnUiThread {
                        if (error is AlreadyClaimedException) {
                            store.save(config.copy(claimCode = null))
                            claimCode.text = "Claimed by a household"
                            showMessage("This agent already belongs to a household. Remove it in the Mantau app to claim it again.")
                        } else {
                            showError("Claim code refresh failed", error)
                        }
                    }
                }
        }
    }

    private fun confirmRotation() {
        val secret = store.agentSecret() ?: run {
            showMessage("Enroll this agent first.")
            return
        }
        AlertDialog.Builder(this)
            .setTitle("Rotate agent key?")
            .setMessage("The agent gets a new secret and the old one stops working immediately. Household ownership does not change.")
            .setPositiveButton("Rotate") { _, _ -> enroll(currentSecret = secret) }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun enroll(currentSecret: String?) {
        val config = runCatching { saveFromUi(showConfirmation = false) }
            .getOrElse { showError("Cannot enroll", it); return }
        if (config.serverUrl.isBlank()) {
            showMessage("Enter the Mantau server URL first.")
            return
        }
        status.text = if (currentSecret == null) "Enrolling…" else "Rotating key…"
        worker.submit {
            runCatching { ControlPlaneClient().enroll(config.serverUrl, config.agentId, currentSecret) }
                .onSuccess { enrollment ->
                    store.save(
                        config.copy(claimCode = enrollment.claimCode ?: config.claimCode),
                        agentSecret = enrollment.secret,
                    )
                    runOnUiThread {
                        enrollment.claimCode?.let { claimCode.text = "Claim code: $it" }
                        showMessage(
                            if (currentSecret == null) "Enrollment complete. Enter the claim code in the Mantau app."
                            else "Agent key rotated. Restart monitoring to use it."
                        )
                    }
                }
                .onFailure { runOnUiThread { showError("Enrollment failed", it) } }
        }
    }

    private fun discover() {
        status.text = "Discovering on local Wi‑Fi for up to 3 seconds…"
        worker.submit {
            runCatching {
                AndroidWsDiscovery(applicationContext).discover(
                    cancelled = { Thread.currentThread().isInterrupted },
                )
            }
                .onSuccess { cameras -> runOnUiThread { chooseCamera(cameras) } }
                .onFailure { runOnUiThread { showError("Discovery failed", it) } }
        }
    }

    private fun chooseCamera(cameras: List<DiscoveredCamera>) {
        if (cameras.isEmpty()) {
            showMessage("No ONVIF camera was found. Enter its IP and RTSP paths manually.")
            return
        }
        if (cameras.size == 1) {
            applyCamera(cameras.single())
            showMessage("Found ${cameras.single().host}. Confirm its RTSP paths and credentials, then save.")
            return
        }
        val labels = cameras.map { camera ->
            "${camera.name ?: camera.host} · ${camera.host} · ${if (camera.reachable) "RTSP reachable" else "RTSP not verified"}"
        }.toTypedArray()
        AlertDialog.Builder(this)
            .setTitle("Select a camera")
            .setItems(labels) { _, index -> applyCamera(cameras[index]) }
            .setNegativeButton("Use manual address", null)
            .show()
    }

    private fun applyCamera(camera: DiscoveredCamera) {
        cameraHost.setText(camera.host)
        cameraPort.setText(camera.port.toString())
        mainPath.setText(camera.mainPath)
        subPath.setText(camera.subPath.orEmpty())
    }

    private fun requestNotificationThenStart() {
        val config = runCatching { saveFromUi(showConfirmation = false).also { it.validate(requireCamera = false) } }
            .getOrElse { showError("Cannot start", it); return }
        if (config.serverUrl.isBlank() || store.agentSecret().isNullOrBlank()) {
            showMessage("Enroll this agent with the control plane before starting monitoring.")
            return
        }
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            startAfterPermission = true
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), NOTIFICATION_PERMISSION)
        } else {
            startMonitoring()
        }
    }

    private fun startMonitoring() {
        try {
            startForegroundService(Intent(this, MonitoringService::class.java))
        } catch (exception: Exception) {
            RuntimeStatusStore(this).write(id.mantau.agent.model.RuntimeStatus(
                health = id.mantau.agent.model.HealthState.DEGRADED,
                explanation = "Could not start foreground service (${exception::class.java.simpleName}).",
            ))
            showError("Could not start monitoring", exception)
        }
    }

    private fun stopMonitoring() {
        stopService(Intent(this, MonitoringService::class.java))
    }

    private fun showError(prefix: String, error: Throwable) {
        showMessage("$prefix (${error::class.java.simpleName}). Check the values and connection.")
    }

    private fun showMessage(message: String) {
        AlertDialog.Builder(this).setMessage(message).setPositiveButton("OK", null).show()
    }

    private fun LinearLayout.section(title: String) {
        addView(TextView(this@MainActivity).apply {
            text = title
            textSize = 19f
            setTypeface(typeface, Typeface.BOLD)
            setPadding(0, dp(18), 0, dp(4))
        }, matchWrap())
    }

    private fun LinearLayout.field(hintText: String, type: Int = InputType.TYPE_CLASS_TEXT): EditText {
        val field = EditText(this@MainActivity).apply {
            hint = hintText
            inputType = type
            isSingleLine = true
        }
        addView(field, matchWrap())
        return field
    }

    private fun button(label: String, action: () -> Unit) = Button(this).apply {
        text = label
        setOnClickListener { action() }
    }

    private fun matchWrap() = ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
    private fun dp(value: Int) = (value * resources.displayMetrics.density).toInt()

    companion object { private const val NOTIFICATION_PERMISSION = 42 }
}
