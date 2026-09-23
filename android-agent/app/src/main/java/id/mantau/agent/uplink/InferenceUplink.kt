package id.mantau.agent.uplink

import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.util.UUID

/**
 * Server inference: upload sampled frames to `POST /agents/{id}/inference`; the server
 * runs the same fall detector. The wire contract (headers, versioned HMAC message,
 * idempotency, correlation, retention) is mantau-core `contracts/inference.py`, pinned by
 * the shared `inference_request.json` fixture. `/cameras/{id}/frame` stays live view only.
 */
object InferenceSigning {
    private val PREFIX = "mantau-inference-v1".toByteArray(Charsets.UTF_8)
    const val MAX_EVENT_IDS = 8

    fun message(
        agentId: String, cameraId: String, sessionId: String, frameId: String,
        tsMs: Long, capturedAtMs: Long, eventIds: List<String>, body: ByteArray,
    ): ByteArray {
        val fields = listOf(agentId, cameraId, sessionId, frameId, tsMs.toString(),
            capturedAtMs.toString(), eventIds.joinToString(","))
        val newline = byteArrayOf('\n'.code.toByte())
        return PREFIX + newline + fields.joinToString("\n").toByteArray(Charsets.UTF_8) + newline + body
    }

    fun sign(
        secret: String, agentId: String, cameraId: String, sessionId: String, frameId: String,
        tsMs: Long, capturedAtMs: Long, eventIds: List<String>, body: ByteArray,
    ): String = hmacSha256(
        secret.toByteArray(Charsets.UTF_8),
        message(agentId, cameraId, sessionId, frameId, tsMs, capturedAtMs, eventIds, body),
    )
}

data class InferenceCapability(
    val available: Boolean,
    val maxFrameBytes: Int,
    val maxFrameAgeS: Double,
    val maxFps: Double,
    val reason: String?,
) {
    companion object {
        fun parse(json: JSONObject) = InferenceCapability(
            available = json.getBoolean("available"),
            maxFrameBytes = json.getInt("max_frame_bytes"),
            maxFrameAgeS = json.getDouble("max_frame_age_s"),
            maxFps = json.getDouble("max_fps"),
            reason = json.optString("reason").takeIf { !json.isNull("reason") && it.isNotEmpty() },
        )
    }
}

data class InferenceConfirmation(val eventId: String, val confirmed: Boolean, val confidence: Double, val reason: String?)

data class InferenceResult(
    val frameId: String,
    val processed: Boolean,
    val duplicate: Boolean,
    val reason: String?,
    val eventIds: List<String>,
    val confirmations: List<InferenceConfirmation>,
) {
    companion object {
        fun parse(json: JSONObject): InferenceResult {
            val events = json.getJSONArray("events")
            val confirmations = json.getJSONArray("confirmations")
            return InferenceResult(
                frameId = json.getString("frame_id"),
                processed = json.getBoolean("processed"),
                duplicate = json.optBoolean("duplicate", false),
                reason = json.optString("reason").takeIf { !json.isNull("reason") && it.isNotEmpty() },
                eventIds = List(events.length()) { events.getJSONObject(it).getString("event_id") },
                confirmations = List(confirmations.length()) { i ->
                    val c = confirmations.getJSONObject(i)
                    InferenceConfirmation(
                        c.getString("event_id"), c.getBoolean("confirmed"), c.getDouble("confidence"),
                        c.optString("reason").takeIf { !c.isNull("reason") && it.isNotEmpty() },
                    )
                },
            )
        }
    }
}

data class HttpReply(val status: Int, val body: String)

interface InferenceTransport {
    /** Throws IOException on a network failure. */
    fun post(url: String, body: ByteArray, headers: Map<String, String>): HttpReply
    fun get(url: String): HttpReply
}

class HttpInferenceTransport : InferenceTransport {
    override fun post(url: String, body: ByteArray, headers: Map<String, String>): HttpReply =
        request(url) { connection ->
            connection.requestMethod = "POST"
            connection.doOutput = true
            headers.forEach(connection::setRequestProperty)
            connection.outputStream.use { it.write(body) }
        }

    override fun get(url: String): HttpReply = request(url) { it.requestMethod = "GET" }

    private fun request(url: String, prepare: (HttpURLConnection) -> Unit): HttpReply {
        val connection = URL(url).openConnection() as HttpURLConnection
        try {
            connection.connectTimeout = 5_000
            connection.readTimeout = 10_000
            prepare(connection)
            val status = connection.responseCode
            val stream = if (status in 200..299) connection.inputStream else connection.errorStream
            return HttpReply(status, stream?.use { it.readBytes().decodeToString() }.orEmpty())
        } finally {
            connection.disconnect()
        }
    }
}

/** Reads `GET /inference/capability`; null when the server does not offer it or is unreachable. */
fun discoverInferenceCapability(serverUrl: String, transport: InferenceTransport): InferenceCapability? =
    try {
        val reply = transport.get("${serverUrl.trimEnd('/')}/inference/capability")
        if (reply.status == 200) InferenceCapability.parse(JSONObject(reply.body)) else null
    } catch (_: IOException) {
        null
    } catch (_: org.json.JSONException) {
        null
    }

/**
 * Same behavior as the Python agent's `HttpInferenceUplink`: signed, size-limited, retried
 * at most [retries] times with the same frame id and only while the frame is fresh, then
 * dropped. Frames are never queued durably.
 */
class HttpInferenceUplink(
    private val serverUrl: () -> String,
    private val agentId: () -> String,
    private val secret: () -> String,
    val capability: InferenceCapability,
    private val transport: InferenceTransport = HttpInferenceTransport(),
    private val retries: Int = 1,
    private val retryBaseMs: Long = 100,
    private val wallClockMs: () -> Long = System::currentTimeMillis,
    private val sleep: (Long) -> Unit = Thread::sleep,
) {
    val sessionId: String = UUID.randomUUID().toString().replace("-", "")
    @Volatile var lastResult: InferenceResult? = null
        private set
    @Volatile var lastError: String? = null
        private set
    var accepted = 0L
        private set
    var failed = 0L
        private set

    /** Android frames carry their capture time, so it is both the stream and wall clock. */
    @Synchronized
    fun submit(jpeg: ByteArray, cameraId: String, capturedAtMs: Long, eventIds: List<String> = emptyList()): Boolean {
        if (jpeg.size > capability.maxFrameBytes) {
            failed++
            lastError = "frame_too_large"
            return false
        }
        val ids = eventIds.take(InferenceSigning.MAX_EVENT_IDS)
        val frameId = UUID.randomUUID().toString().replace("-", "")
        val agent = agentId()
        val headers = buildMap {
            put("Content-Type", "image/jpeg")
            put("X-Mantau-Agent", agent)
            put("X-Mantau-Camera", cameraId)
            put("X-Mantau-Session", sessionId)
            put("X-Mantau-Frame", frameId)
            put("X-Mantau-Frame-Ts", capturedAtMs.toString())
            put("X-Mantau-Captured-At", capturedAtMs.toString())
            put("X-Mantau-Signature", InferenceSigning.sign(
                secret(), agent, cameraId, sessionId, frameId, capturedAtMs, capturedAtMs, ids, jpeg))
            if (ids.isNotEmpty()) put("X-Mantau-Event-Ids", ids.joinToString(","))
        }
        val url = "${serverUrl().trimEnd('/')}/agents/$agent/inference"
        for (attempt in 0..retries) {
            if (attempt > 0) {
                if (wallClockMs() - capturedAtMs >= capability.maxFrameAgeS * 1000) break
                sleep(retryBaseMs shl (attempt - 1))
            }
            val reply = try {
                transport.post(url, jpeg, headers)
            } catch (exception: IOException) {
                lastError = exception::class.java.simpleName
                continue
            }
            if (reply.status == 200) {
                val result = try {
                    InferenceResult.parse(JSONObject(reply.body))
                } catch (_: org.json.JSONException) {
                    lastError = "invalid_response"
                    break
                }
                lastResult = result
                lastError = null
                accepted++
                return result.processed
            }
            lastError = "HTTP ${reply.status}"
            if (reply.status !in RETRYABLE) break
        }
        failed++
        return false
    }

    companion object {
        private val RETRYABLE = setOf(500, 502, 503, 504)
    }
}

/** Minimum spacing between inference uploads, capped by the server's advertised rate. */
class CloudUploadPolicy(requestedFps: Double, capability: InferenceCapability) {
    val fps: Double = minOf(requestedFps, capability.maxFps).takeIf { it > 0 } ?: capability.maxFps
    private val intervalMs = (1000.0 / fps).toLong().coerceAtLeast(1)
    private var lastMs = Long.MIN_VALUE

    @Synchronized
    fun admit(nowMs: Long): Boolean {
        if (lastMs != Long.MIN_VALUE && nowMs - lastMs < intervalMs) return false
        lastMs = nowMs
        return true
    }
}
