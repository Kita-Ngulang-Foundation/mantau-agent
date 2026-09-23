package id.mantau.agent.rtsp

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test
import java.time.Instant

class RtspBehaviorTest {
    @Test
    fun `silent camera fails boundedly so capture can reconnect`() {
        val server = java.net.ServerSocket(0)
        val finished = java.util.concurrent.CountDownLatch(1)
        val worker = Thread {
            server.accept().use { socket ->
                val reader = socket.getInputStream().bufferedReader()
                val output = socket.getOutputStream()
                repeat(4) {
                    val request = reader.readLine()
                    var cseq = "1"
                    while (true) {
                        val line = reader.readLine() ?: break
                        if (line.isEmpty()) break
                        if (line.startsWith("CSeq:", true)) cseq = line.substringAfter(':').trim()
                    }
                    val sdp = if (request.startsWith("DESCRIBE"))
                        "v=0\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\na=control:track1\r\n" else ""
                    output.write(("RTSP/1.0 200 OK\r\nCSeq: $cseq\r\nSession: test\r\nContent-Length: ${sdp.length}\r\n\r\n$sdp").toByteArray())
                    output.flush()
                }
                finished.await(5, java.util.concurrent.TimeUnit.SECONDS)
            }
        }
        worker.start()
        try {
            RtspClient(streamIdleTimeoutMs = 50).use { client ->
                assertThrows(RtspException::class.java) {
                    client.stream(id.mantau.agent.model.CameraConfig(host = "127.0.0.1", port = server.localPort),
                        null, LatestFrameBuffer(), { false })
                }
            }
        } finally {
            finished.countDown()
            server.close()
            worker.join(2_000)
        }
    }

    @Test
    fun `latest frame queue drops oldest at its bound`() {
        val queue = LatestFrameBuffer(2)
        (1..3).forEach { value ->
            queue.offer(EncodedFrame(byteArrayOf(value.toByte()), Instant.ofEpochSecond(value.toLong()), value.toLong()))
        }
        assertEquals(2, queue.size)
        assertEquals(listOf(2L, 3L), queue.snapshot().map { it.rtpTimestamp })
        assertArrayEquals(byteArrayOf(3), queue.latest()!!.bytes)
    }

    @Test
    fun `frame queue copies mutable input`() {
        val bytes = byteArrayOf(1, 2)
        val queue = LatestFrameBuffer(1)
        queue.offer(EncodedFrame(bytes, Instant.EPOCH, 1))
        bytes[0] = 9
        assertArrayEquals(byteArrayOf(1, 2), queue.latest()!!.bytes)
    }

    @Test
    fun `reconnect policy is capped and resettable`() {
        val policy = ReconnectPolicy(baseMs = 500, capMs = 2_000, random = { it })
        assertEquals(listOf(500L, 1_000L, 2_000L, 2_000L), List(4) { policy.nextDelayMs() })
        policy.reset()
        assertEquals(500L, policy.nextDelayMs())
    }

    @Test
    fun `rtsp lifecycle accepts reconnect path and rejects impossible path`() {
        val state = RtspStateMachine()
        state.transition(RtspState.CONNECTING)
        state.transition(RtspState.AUTHENTICATING)
        state.transition(RtspState.CONNECTED)
        state.transition(RtspState.STREAMING)
        state.transition(RtspState.BACKING_OFF)
        state.transition(RtspState.CONNECTING)
        state.transition(RtspState.STOPPED)
        assertEquals(RtspState.STOPPED, state.state)

        val invalid = RtspStateMachine()
        assertThrows(IllegalArgumentException::class.java) { invalid.transition(RtspState.STREAMING) }
    }
}
