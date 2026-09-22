package id.mantau.agent.uplink

import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

data class JpegFrame(val bytes: ByteArray, val width: Int, val height: Int, val capturedAtMs: Long)

fun interface FrameTransport {
    fun post(url: String, body: ByteArray, headers: Map<String, String>): Boolean
}

class HttpFrameTransport : FrameTransport {
    override fun post(url: String, body: ByteArray, headers: Map<String, String>): Boolean {
        val connection = URL(url).openConnection() as HttpURLConnection
        return try {
            connection.requestMethod = "POST"
            connection.connectTimeout = 5_000
            connection.readTimeout = 5_000
            connection.doOutput = true
            headers.forEach(connection::setRequestProperty)
            connection.outputStream.use { it.write(body) }
            connection.responseCode in 200..299
        } catch (_: IOException) {
            false
        } finally {
            connection.disconnect()
        }
    }
}

class SignedFrameUploader(
    private val serverUrl: () -> String,
    private val agentId: () -> String,
    private val secret: () -> String,
    private val transport: FrameTransport = HttpFrameTransport(),
) {
    fun upload(cameraId: String, jpeg: ByteArray): Boolean {
        val signature = hmacSha256(
            secret().toByteArray(Charsets.UTF_8),
            cameraId.toByteArray(Charsets.UTF_8) + byteArrayOf('.'.code.toByte()) + jpeg,
        )
        return transport.post(
            "${serverUrl().trimEnd('/')}/cameras/$cameraId/frame",
            jpeg,
            mapOf(
                "Content-Type" to "image/jpeg",
                "X-Mantau-Agent" to agentId(),
                "X-Mantau-Signature" to signature,
            ),
        )
    }
}

class FrameUploadPolicy(
    fps: Double = 1.0,
    val maxWidth: Int = 640,
    val jpegQuality: Int = 65,
    val maxBytes: Int = 256 * 1024,
) {
    private val minimumIntervalMs = (1000.0 / fps).toLong().coerceAtLeast(1)
    private var lastAttemptMs = Long.MIN_VALUE
    var discarded: Long = 0
        private set

    init {
        require(fps > 0.0 && fps <= 2.0)
        require(maxWidth in 160..1280 && jpegQuality in 1..90 && maxBytes in 1..512 * 1024)
    }

    @Synchronized
    fun ready(nowMs: Long): Boolean =
        lastAttemptMs == Long.MIN_VALUE || nowMs - lastAttemptMs >= minimumIntervalMs

    @Synchronized
    fun admit(frame: JpegFrame, nowMs: Long): Boolean {
        if (frame.width > maxWidth || frame.bytes.size > maxBytes ||
            (lastAttemptMs != Long.MIN_VALUE && nowMs - lastAttemptMs < minimumIntervalMs)) {
            discarded++
            return false
        }
        lastAttemptMs = nowMs
        return true
    }
}

class LatestWorkQueue<T>(private val capacity: Int) {
    private val values = java.util.ArrayDeque<T>()
    private var closed = false
    var dropped: Long = 0
        private set

    init { require(capacity > 0) }

    @Synchronized
    fun offer(value: T) {
        if (closed) return
        while (values.size >= capacity) { values.removeFirst(); dropped++ }
        values.addLast(value)
        (this as java.lang.Object).notifyAll()
    }

    @Synchronized
    fun take(): T? {
        while (values.isEmpty() && !closed) (this as java.lang.Object).wait()
        return if (values.isEmpty()) null else values.removeFirst()
    }

    @Synchronized
    fun clear() { dropped += values.size; values.clear() }

    @Synchronized
    fun close() { closed = true; values.clear(); (this as java.lang.Object).notifyAll() }
}
