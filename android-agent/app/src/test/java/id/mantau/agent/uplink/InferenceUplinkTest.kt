package id.mantau.agent.uplink

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File
import java.io.IOException
import java.util.Base64

/**
 * The Android server-inference client against mantau-core's v1 inference contract:
 * byte-identical signatures (golden fixture), result/capability parsing, and the same
 * retry/size/freshness rules as the Python agent.
 */
class InferenceUplinkTest {
    private val fixtures = System.getProperty("mantau.contract.fixtures")?.let(::File)
    private val secret = "agent-secret"
    private val capability = InferenceCapability(true, 1024, 10.0, 15.0, null)

    private class FakeTransport(val replies: MutableList<Any>) : InferenceTransport {
        val requests = mutableListOf<Pair<String, Map<String, String>>>()
        val bodies = mutableListOf<ByteArray>()
        override fun post(url: String, body: ByteArray, headers: Map<String, String>): HttpReply {
            requests += url to headers
            bodies += body
            return when (val next = replies.removeAt(0)) {
                is IOException -> throw next
                is HttpReply -> next
                else -> error("bad reply")
            }
        }
        override fun get(url: String): HttpReply = replies.removeAt(0) as HttpReply
    }

    private fun ok(frameId: String = "f", events: String = "[]", confirmations: String = "[]") = HttpReply(
        200,
        """{"schema_version":1,"frame_id":"$frameId","session_id":"s","processed":true,"duplicate":false,
           "reason":null,"events":$events,"confirmations":$confirmations,"people":1,"server_ms":3.0}""",
    )

    private fun uplink(transport: InferenceTransport, now: () -> Long = { 1_000_000L }, retries: Int = 1) =
        HttpInferenceUplink(
            serverUrl = { "http://server/" }, agentId = { "agent-1" }, secret = { secret },
            capability = capability, transport = transport, retries = retries, retryBaseMs = 1,
            wallClockMs = now, sleep = {},
        )

    @Test
    fun `signature matches the shared golden vector`() {
        val body = ByteArray(512) { (it % 256).toByte() }
        val signature = InferenceSigning.sign(
            "golden-example-shared-secret-do-not-use", "agent-7", "cam-1", "3f9c2a", "a1b2c3d4",
            12345, 1790000000123, listOf("e1", "e2"), body,
        )
        assertEquals("7e03aa80353050df19179cf0848439d9f682ebc3341956ec40faeadd55f2111f", signature)
    }

    @Test
    fun `signature matches mantau-core's fixture file`() {
        assumeTrue(fixtures?.isDirectory == true)
        val raw = JSONObject(File(fixtures, "inference_request.json").readText())
        val ids = raw.getJSONArray("event_ids").let { a -> List(a.length()) { a.getString(it) } }
        val signature = InferenceSigning.sign(
            raw.getString("test_secret"), raw.getString("agent_id"), raw.getString("camera_id"),
            raw.getString("session_id"), raw.getString("frame_id"), raw.getLong("ts_ms"),
            raw.getLong("captured_at_ms"), ids, Base64.getDecoder().decode(raw.getString("body_base64")),
        )
        assertEquals(raw.getString("signature"), signature)
    }

    @Test
    fun `result and capability fixtures parse`() {
        assumeTrue(fixtures?.isDirectory == true)
        val result = InferenceResult.parse(JSONObject(File(fixtures, "inference_result.json").readText()))
        assertTrue(result.processed)
        assertEquals(listOf("0f1e2d3c"), result.eventIds)
        assertEquals(InferenceConfirmation("e1", true, 0.88, null), result.confirmations.single())
        val cap = InferenceCapability.parse(JSONObject(File(fixtures, "inference_capability.json").readText()))
        assertEquals(InferenceCapability(true, 524288, 10.0, 15.0, null), cap)
    }

    @Test
    fun `submit sends a signed, correlated frame`() {
        val transport = FakeTransport(mutableListOf(ok(events = """[{"event_id":"srv-1"}]""")))
        val client = uplink(transport)
        assertTrue(client.submit(byteArrayOf(1, 2, 3), "cam-1", 999_000L, listOf("e1")))
        val (url, headers) = transport.requests.single()
        assertEquals("http://server/agents/agent-1/inference", url)
        assertEquals("e1", headers["X-Mantau-Event-Ids"])
        assertEquals("999000", headers["X-Mantau-Frame-Ts"])
        assertEquals("999000", headers["X-Mantau-Captured-At"])
        val expected = InferenceSigning.sign(
            secret, "agent-1", "cam-1", client.sessionId, headers.getValue("X-Mantau-Frame"),
            999_000L, 999_000L, listOf("e1"), byteArrayOf(1, 2, 3),
        )
        assertEquals(expected, headers["X-Mantau-Signature"])
        assertEquals(listOf("srv-1"), client.lastResult?.eventIds)
    }

    @Test
    fun `server errors and network failures are retried with the same frame id`() {
        val transport = FakeTransport(mutableListOf(IOException("reset"), HttpReply(503, "{}"), ok()))
        val client = uplink(transport, retries = 2)
        assertTrue(client.submit(byteArrayOf(1), "cam-1", 999_500L))
        assertEquals(3, transport.requests.size)
        assertEquals(1, transport.requests.map { it.second["X-Mantau-Frame"] }.toSet().size)
    }

    @Test
    fun `client errors are not retried`() {
        for (status in listOf(400, 401, 404, 413, 422, 429)) {
            val transport = FakeTransport(mutableListOf(HttpReply(status, "{}")))
            val client = uplink(transport)
            assertFalse(client.submit(byteArrayOf(1), "cam-1", 999_500L))
            assertEquals(1, transport.requests.size)
            assertEquals("HTTP $status", client.lastError)
        }
    }

    @Test
    fun `stale frames are dropped instead of retried`() {
        val transport = FakeTransport(mutableListOf(HttpReply(503, "{}")))
        assertFalse(uplink(transport, now = { 1_000_000L }, retries = 3).submit(byteArrayOf(1), "cam-1", 980_000L))
        assertEquals(1, transport.requests.size)
    }

    @Test
    fun `oversized frames are never sent`() {
        val transport = FakeTransport(mutableListOf())
        val client = uplink(transport)
        assertFalse(client.submit(ByteArray(1025), "cam-1", 999_000L))
        assertTrue(transport.requests.isEmpty())
        assertEquals("frame_too_large", client.lastError)
    }

    @Test
    fun `capability discovery tolerates missing servers`() {
        val available = FakeTransport(mutableListOf(HttpReply(200,
            """{"schema_version":1,"available":true,"detector":"mediapipe","max_frame_bytes":524288,
               "max_frame_age_s":10.0,"max_fps":15.0,"reason":null}""")))
        assertEquals(15.0, discoverInferenceCapability("http://s", available)!!.maxFps, 0.0)
        assertNull(discoverInferenceCapability("http://s", FakeTransport(mutableListOf(HttpReply(404, "")))))
        val down = object : InferenceTransport {
            override fun post(url: String, body: ByteArray, headers: Map<String, String>) = error("unused")
            override fun get(url: String): HttpReply = throw IOException("down")
        }
        assertNull(discoverInferenceCapability("http://s", down))
    }

    @Test
    fun `cloud upload rate is capped by the server`() {
        val policy = CloudUploadPolicy(30.0, capability)
        assertEquals(15.0, policy.fps, 0.0)
        assertTrue(policy.admit(0))
        assertFalse(policy.admit(50))
        assertTrue(policy.admit(67))
    }
}
