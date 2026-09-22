package id.mantau.agent.uplink

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.time.Instant

class UplinkBehaviorTest {
    @get:Rule val temporary = TemporaryFolder()

    @Test
    fun `signed frame upload matches Python protocol and endpoint`() {
        var requestUrl = ""
        var requestHeaders = emptyMap<String, String>()
        val jpeg = byteArrayOf(1, 2, 3)
        val uploader = SignedFrameUploader(
            serverUrl = { "http://server" }, agentId = { "agent" }, secret = { "secret" },
            transport = FrameTransport { url, _, headers ->
                requestUrl = url; requestHeaders = headers; true
            },
        )
        assertTrue(uploader.upload("cam", jpeg))
        assertEquals("http://server/cameras/cam/frame", requestUrl)
        assertEquals("agent", requestHeaders["X-Mantau-Agent"])
        assertEquals(
            hmacSha256("secret".toByteArray(), "cam".toByteArray() + byteArrayOf('.'.code.toByte()) + jpeg),
            requestHeaders["X-Mantau-Signature"],
        )
    }

    @Test
    fun `cloud policy bounds fps width bytes and quality`() {
        val policy = FrameUploadPolicy(fps = 1.0, maxWidth = 640, jpegQuality = 65, maxBytes = 100)
        val valid = JpegFrame(ByteArray(80), 640, 360, 0)
        assertTrue(policy.ready(0))
        assertTrue(policy.admit(valid, 0))
        assertFalse(policy.ready(999))
        assertFalse(policy.admit(valid, 999))
        assertFalse(policy.admit(valid.copy(width = 641), 1_000))
        assertFalse(policy.admit(valid.copy(bytes = ByteArray(101)), 1_000))
        assertEquals(65, policy.jpegQuality)
        assertEquals(3, policy.discarded)
    }

    @Test
    fun `failed cloud frame is disposable and never retried`() {
        var attempts = 0
        val uploader = SignedFrameUploader(
            serverUrl = { "http://offline" }, agentId = { "agent" }, secret = { "secret" },
            transport = FrameTransport { _, _, _ -> attempts++; false },
        )
        assertFalse(uploader.upload("cam", byteArrayOf(1)))
        assertEquals(1, attempts)
    }

    @Test
    fun `cloud work queue is bounded and keeps the latest frames`() {
        val queue = LatestWorkQueue<Int>(2)
        queue.offer(1)
        queue.offer(2)
        queue.offer(3)
        assertEquals(1, queue.dropped)
        assertEquals(2, queue.take())
        assertEquals(3, queue.take())
        queue.close()
        assertEquals(null, queue.take())
    }

    @Test
    fun `durable event recovers after restart without changing envelope identity`() {
        val state = temporary.newFolder("uplink")
        val failed = mutableListOf<SignedEnvelope>()
        val first = DurableUplink(
            { "http://server" }, { "agent-1" }, { "secret" }, state,
            EnvelopeTransport { _, envelope -> failed += envelope; false },
        )
        val event = FallEvent(cameraId = "cam-1", occurredAt = Instant.parse("2026-09-22T08:00:00Z"), confidence = .9)
        assertFalse(first.sendEvent(event))
        assertEquals(1, first.depth())

        val recovered = mutableListOf<SignedEnvelope>()
        val second = DurableUplink(
            { "http://server" }, { "agent-1" }, { "secret" }, state,
            EnvelopeTransport { _, envelope -> recovered += envelope; true },
        )
        assertEquals(1, second.drain())
        assertEquals(0, second.depth())
        assertEquals(failed.single().sequence, recovered.single().sequence)
        assertEquals(failed.single().signature, recovered.single().signature)
    }
}
