package id.mantau.agent.rtsp

import java.time.Instant
import java.util.ArrayDeque

data class EncodedFrame(val bytes: ByteArray, val capturedAt: Instant, val rtpTimestamp: Long)

class LatestFrameBuffer(val capacity: Int = 2) {
    private val frames = ArrayDeque<EncodedFrame>()

    init {
        require(capacity > 0) { "Frame buffer capacity must be positive" }
    }

    @Synchronized
    fun offer(frame: EncodedFrame) {
        while (frames.size >= capacity) frames.removeFirst()
        frames.addLast(frame.copy(bytes = frame.bytes.copyOf()))
    }

    @Synchronized
    fun latest(): EncodedFrame? = frames.peekLast()?.let { it.copy(bytes = it.bytes.copyOf()) }

    @Synchronized
    fun snapshot(): List<EncodedFrame> = frames.map { it.copy(bytes = it.bytes.copyOf()) }

    @Synchronized
    fun clear() = frames.clear()

    @get:Synchronized
    val size: Int get() = frames.size
}

