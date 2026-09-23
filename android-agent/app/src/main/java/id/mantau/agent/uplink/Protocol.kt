package id.mantau.agent.uplink

import org.json.JSONArray
import org.json.JSONObject
import java.math.BigDecimal
import java.math.MathContext
import java.math.RoundingMode
import java.time.Instant
import java.time.format.DateTimeFormatterBuilder
import java.util.UUID
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec
import kotlin.math.abs

data class FallEvent(
    val eventId: String = UUID.randomUUID().toString().replace("-", ""),
    val cameraId: String,
    val occurredAt: Instant,
    val confidence: Double,
    val trackId: Int? = null,
    val signals: Map<String, Double> = emptyMap(),
    /** mantau-core EventKind: fall, stillness, nocturnal_movement, bathroom_duration. */
    val kind: String = "fall",
    val severity: String = "critical",
    /** The household's zone id for activity events; left out of the payload when null. */
    val zoneId: String? = null,
) {
    init { require(confidence in 0.0..1.0) }

    fun toJson(): JSONObject = JSONObject()
        .put("event_id", eventId)
        .put("camera_id", cameraId)
        .put("kind", kind)
        .put("severity", severity)
        .put("occurred_at", formatInstant(occurredAt))
        .put("confidence", confidence)
        .put("track_id", trackId ?: JSONObject.NULL)
        .put("signals", JSONObject(signals))
        .put("clip", JSONObject.NULL)
        .also { json -> zoneId?.let { json.put("zone_id", it) } }
}

data class Heartbeat(
    val agentId: String,
    val cameraId: String?,
    val sentAt: Instant,
    val cameraReachable: Boolean,
    val detectorAlive: Boolean,
    val queueDepth: Int,
) {
    fun toJson(): JSONObject = JSONObject()
        .put("agent_id", agentId)
        .put("camera_id", cameraId ?: JSONObject.NULL)
        .put("sent_at", formatInstant(sentAt))
        .put("camera_reachable", cameraReachable)
        .put("detector_alive", detectorAlive)
        .put("queue_depth", queueDepth)
}

data class SignedEnvelope(
    val agentId: String,
    val sequence: Long,
    val sentAt: Instant,
    val kind: String,
    val payload: JSONObject,
    val signature: String,
) {
    fun toJson(): JSONObject = unsignedJson()
        .put("sig", signature)

    fun unsignedJson(): JSONObject = JSONObject()
        .put("agent_id", agentId)
        .put("seq", sequence)
        .put("sent_at", formatInstant(sentAt))
        .put("kind", kind)
        .put("payload", payload)

    companion object {
        fun signEvent(agentId: String, sequence: Long, sentAt: Instant, event: FallEvent, secret: String): SignedEnvelope =
            sign(agentId, sequence, sentAt, "fall_event", event.toJson(), secret)

        fun signHeartbeat(agentId: String, sequence: Long, sentAt: Instant, heartbeat: Heartbeat, secret: String): SignedEnvelope =
            sign(agentId, sequence, sentAt, "heartbeat", heartbeat.toJson(), secret)

        fun fromJson(json: JSONObject) = SignedEnvelope(
            agentId = json.getString("agent_id"),
            sequence = json.getLong("seq"),
            sentAt = Instant.parse(json.getString("sent_at")),
            kind = json.getString("kind"),
            payload = json.getJSONObject("payload"),
            signature = json.getString("sig"),
        )

        private fun sign(
            agentId: String,
            sequence: Long,
            sentAt: Instant,
            kind: String,
            payload: JSONObject,
            secret: String,
        ): SignedEnvelope {
            val unsigned = SignedEnvelope(agentId, sequence, sentAt, kind, payload, "")
            val signature = hmacSha256(secret.toByteArray(Charsets.UTF_8), CanonicalJson.encode(unsigned.unsignedJson()))
            return unsigned.copy(signature = signature)
        }
    }
}

/**
 * The exact bytes the server re-derives for a signature: sorted keys, no spaces, and
 * numbers written the way Python's json.dumps writes them. Envelopes are also sent
 * and stored in this form, so the server parses back the very values that were
 * signed (org.json would write 30.0 as 30 and 0.00012 as 1.2E-4).
 */
object CanonicalJson {
    fun encode(value: Any?): ByteArray = render(value).toByteArray(Charsets.UTF_8)

    fun string(value: Any?): String = render(value)

    /** Python's repr(float): the shortest digits that round-trip, positional for exponents -4..15. */
    fun pythonFloat(value: Double): String {
        require(value.isFinite()) { "JSON has no NaN or Infinity" }
        if (value == 0.0) return if (1.0 / value < 0) "-0.0" else "0.0"
        val exact = BigDecimal(value)
        val shortest = (1..17).asSequence()
            .map { exact.round(MathContext(it, RoundingMode.HALF_EVEN)) }
            .first { it.toDouble() == value }
            .stripTrailingZeros()
        val digits = shortest.unscaledValue().abs().toString()
        val exponent = digits.length - 1 - shortest.scale()
        val sign = if (value < 0) "-" else ""
        if (exponent in -4..15) {
            val plain = shortest.abs().toPlainString()
            return sign + if ('.' in plain) plain else "$plain.0"
        }
        val mantissa = if (digits.length == 1) digits else digits[0] + "." + digits.substring(1)
        val expSign = if (exponent < 0) "-" else "+"
        return sign + mantissa + "e" + expSign + abs(exponent).toString().padStart(2, '0')
    }

    private fun render(value: Any?): String = when (value) {
        null, JSONObject.NULL -> "null"
        is JSONObject -> value.keys().asSequence().toList().sorted().joinToString(",", "{", "}") {
            quote(it) + ":" + render(value.get(it))
        }
        is JSONArray -> (0 until value.length()).joinToString(",", "[", "]") { render(value.get(it)) }
        is String -> quote(value)
        is Boolean -> value.toString()
        is Double -> pythonFloat(value)
        is Float -> pythonFloat(value.toDouble())
        is Int, is Long, is Short, is Byte -> value.toString()
        is Number -> pythonFloat(value.toDouble())
        else -> quote(value.toString())
    }

    private fun quote(value: String): String = buildString {
        append('"')
        for (char in value) when (char) {
            '"' -> append("\\\"")
            '\\' -> append("\\\\")
            '\b' -> append("\\b")
            '\u000c' -> append("\\f")
            '\n' -> append("\\n")
            '\r' -> append("\\r")
            '\t' -> append("\\t")
            else -> if (char.code < 0x20 || char.code > 0x7e) append("\\u%04x".format(char.code)) else append(char)
        }
        append('"')
    }
}

fun hmacSha256(secret: ByteArray, message: ByteArray): String {
    val mac = Mac.getInstance("HmacSHA256")
    mac.init(SecretKeySpec(secret, "HmacSHA256"))
    return mac.doFinal(message).joinToString("") { "%02x".format(it) }
}

private val INSTANT_FORMATTER = DateTimeFormatterBuilder().appendInstant(6).toFormatter()
fun formatInstant(value: Instant): String = INSTANT_FORMATTER.format(value)
