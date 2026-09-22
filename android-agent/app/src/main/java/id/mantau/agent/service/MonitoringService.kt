package id.mantau.agent.service

import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import id.mantau.agent.MainActivity
import id.mantau.agent.R
import id.mantau.agent.model.HealthState
import id.mantau.agent.model.RuntimeStatus
import id.mantau.agent.storage.RuntimeStatusStore

class MonitoringService : Service() {
    private var engine: MonitoringEngine? = null
    private var wakeLock: PowerManager.WakeLock? = null

    override fun onCreate() {
        super.onCreate()
        createChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            stopMonitoring()
            stopSelf()
            return START_NOT_STICKY
        }
        return try {
            promote(RuntimeStatus(running = true, health = HealthState.ONLINE, explanation = "Starting monitoring…"))
            acquireWakeLock()
            if (engine == null) engine = MonitoringEngine(applicationContext, ::updateNotification).also { it.start() }
            START_STICKY
        } catch (exception: Exception) {
            RuntimeStatusStore(this).write(RuntimeStatus(
                running = false,
                health = HealthState.DEGRADED,
                explanation = "Foreground service failed (${exception::class.java.simpleName}).",
            ))
            stopMonitoring()
            stopSelf()
            START_NOT_STICKY
        }
    }

    override fun onDestroy() {
        stopMonitoring()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun promote(status: RuntimeStatus) {
        val notification = notification(status)
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE)
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
    }

    private fun updateNotification(status: RuntimeStatus) {
        (getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager)
            .notify(NOTIFICATION_ID, notification(status))
    }

    private fun notification(status: RuntimeStatus): Notification {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val stop = PendingIntent.getService(
            this, 1, Intent(this, MonitoringService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val summary = when {
            status.cameraConnectivity.wireValue == "connected" -> "Camera connected · monitoring locally"
            !status.explanation.isNullOrBlank() -> status.explanation
            else -> "Connecting to camera"
        }
        return Notification.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_agent)
            .setContentTitle("Mantau Agent is running")
            .setContentText(summary)
            .setContentIntent(open)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setCategory(Notification.CATEGORY_SERVICE)
            .addAction(Notification.Action.Builder(null, "Stop", stop).build())
            .build()
    }

    private fun createChannel() {
        val channel = NotificationChannel(
            CHANNEL_ID, getString(R.string.monitoring_channel), NotificationManager.IMPORTANCE_LOW,
        ).apply { description = "Persistent status for local CCTV monitoring" }
        (getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager).createNotificationChannel(channel)
    }

    @SuppressLint("WakelockTimeout")
    private fun acquireWakeLock() {
        if (wakeLock?.isHeld == true) return
        wakeLock = (getSystemService(Context.POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "MantauAgent:Monitoring")
            .apply { setReferenceCounted(false); acquire() }
    }

    private fun stopMonitoring() {
        engine?.close()
        engine = null
        wakeLock?.let { if (it.isHeld) it.release() }
        wakeLock = null
        stopForeground(STOP_FOREGROUND_REMOVE)
    }

    companion object {
        const val ACTION_STOP = "id.mantau.agent.STOP"
        private const val CHANNEL_ID = "mantau-monitoring"
        private const val NOTIFICATION_ID = 2401
    }
}
