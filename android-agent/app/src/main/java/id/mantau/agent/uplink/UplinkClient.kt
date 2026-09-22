package id.mantau.agent.uplink

import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.time.Instant

fun interface EnvelopeTransport { fun post(serverUrl: String, envelope: SignedEnvelope): Boolean }

class HttpEnvelopeTransport : EnvelopeTransport {
    override fun post(serverUrl: String, envelope: SignedEnvelope): Boolean {
        val connection = URL(serverUrl.trimEnd('/') + "/ingest").openConnection() as HttpURLConnection
        return try {
            connection.requestMethod = "POST"
            connection.connectTimeout = 10_000
            connection.readTimeout = 10_000
            connection.doOutput = true
            connection.setRequestProperty("Content-Type", "application/json")
            connection.outputStream.use { it.write(envelope.toJson().toString().toByteArray(Charsets.UTF_8)) }
            connection.responseCode in 200..299
        } catch (_: IOException) {
            false
        } finally {
            connection.disconnect()
        }
    }
}

class DurableUplink(
    private val serverUrl: () -> String,
    private val agentId: () -> String,
    private val secret: () -> String,
    stateDirectory: File,
    private val transport: EnvelopeTransport = HttpEnvelopeTransport(),
) {
    private val sequence = SequenceStore(File(stateDirectory, "sequence.txt"))
    private val queue = DurableEnvelopeQueue(File(stateDirectory, "envelopes"))

    @Synchronized
    fun sendEvent(event: FallEvent): Boolean {
        val now = Instant.now()
        val envelope = SignedEnvelope.signEvent(agentId(), sequence.next(), now, event, secret())
        return persistAndSend(envelope)
    }

    @Synchronized
    fun sendHeartbeat(heartbeat: Heartbeat): Boolean {
        val envelope = SignedEnvelope.signHeartbeat(
            agentId(), sequence.next(), heartbeat.sentAt, heartbeat, secret(),
        )
        return persistAndSend(envelope)
    }

    @Synchronized
    fun drain(limit: Int = 50): Int {
        queue.evictExpired()
        var sent = 0
        for (envelope in queue.pending(limit)) {
            if (!transport.post(serverUrl(), envelope)) break
            queue.ack(envelope)
            sent++
        }
        return sent
    }

    fun depth(): Int = queue.depth()

    private fun persistAndSend(envelope: SignedEnvelope): Boolean {
        queue.put(envelope)
        if (!transport.post(serverUrl(), envelope)) return false
        queue.ack(envelope)
        drain()
        return true
    }
}
