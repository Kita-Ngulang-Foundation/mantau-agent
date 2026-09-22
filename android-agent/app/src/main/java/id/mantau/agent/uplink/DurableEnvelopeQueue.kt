package id.mantau.agent.uplink

import java.io.File
import java.io.FileOutputStream
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.time.Duration
import java.time.Instant

object AtomicFiles {
    fun write(target: File, bytes: ByteArray) {
        target.parentFile?.mkdirs()
        val temporary = File(target.parentFile, ".${target.name}.tmp")
        FileOutputStream(temporary).use { output ->
            output.write(bytes)
            output.flush()
            output.fd.sync()
        }
        try {
            Files.move(
                temporary.toPath(), target.toPath(),
                StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING,
            )
        } catch (_: Exception) {
            Files.move(temporary.toPath(), target.toPath(), StandardCopyOption.REPLACE_EXISTING)
        }
    }
}

class SequenceStore(private val file: File) {
    private var next = read()

    @Synchronized
    fun next(): Long {
        val value = next
        next = Math.addExact(next, 1)
        AtomicFiles.write(file, next.toString().toByteArray(Charsets.US_ASCII))
        return value
    }

    private fun read(): Long {
        if (!file.exists()) return 0
        val value = file.readText().trim().toLongOrNull()
            ?: throw IllegalStateException("Sequence state is corrupt; refusing to reuse numbers")
        require(value >= 0) { "Sequence state is negative; refusing to reuse numbers" }
        return value
    }
}

class DurableEnvelopeQueue(
    private val directory: File,
    private val ttl: Duration = Duration.ofMinutes(5),
    private val maxItems: Int = 512,
) {
    init { require(maxItems > 0); directory.mkdirs() }

    @Synchronized
    fun put(envelope: SignedEnvelope) {
        evictExpired()
        if (depth() >= maxItems) evictOldestHeartbeat()
        check(depth() < maxItems) { "Durable event queue is full; no event was discarded" }
        AtomicFiles.write(file(envelope.sequence), envelope.toJson().toString().toByteArray(Charsets.UTF_8))
    }

    @Synchronized
    fun pending(limit: Int = 50): List<SignedEnvelope> = files().take(limit).map {
        SignedEnvelope.fromJson(org.json.JSONObject(it.readText()))
    }

    @Synchronized
    fun ack(envelope: SignedEnvelope) {
        check(!file(envelope.sequence).exists() || file(envelope.sequence).delete()) { "Could not acknowledge envelope" }
    }

    @Synchronized
    fun depth(): Int = files().size

    @Synchronized
    fun evictExpired(now: Instant = Instant.now()): Int {
        var removed = 0
        val cutoff = now.minus(ttl).toEpochMilli()
        files().filter { it.lastModified() < cutoff }.forEach { if (it.delete()) removed++ }
        return removed
    }

    private fun evictOldestHeartbeat() {
        files().firstOrNull { runCatching {
            org.json.JSONObject(it.readText()).getString("kind") == "heartbeat"
        }.getOrDefault(false) }?.delete()
    }

    private fun files(): List<File> = directory.listFiles { file -> file.extension == "json" }
        ?.sortedBy { it.nameWithoutExtension.toLongOrNull() ?: Long.MAX_VALUE }.orEmpty()

    private fun file(sequence: Long) = File(directory, "%020d.json".format(sequence))
}

