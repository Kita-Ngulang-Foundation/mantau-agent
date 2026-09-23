package id.mantau.agent.network

import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import java.net.InetAddress
import java.net.ServerSocket
import kotlin.concurrent.thread

/** Minimal one-request-at-a-time HTTP server (android.jar has no HttpServer). */
class ControlPlaneClientTest {
    private lateinit var server: ServerSocket
    private val seen = mutableListOf<Map<String, String?>>()
    @Volatile private var status = 201
    @Volatile private var response = "{}"

    @Before
    fun start() {
        server = ServerSocket(0, 5, InetAddress.getLoopbackAddress())
        thread(isDaemon = true) {
            while (!server.isClosed) {
                val socket = runCatching { server.accept() }.getOrNull() ?: break
                socket.use {
                    val reader = it.getInputStream().bufferedReader()
                    val path = reader.readLine().split(" ")[1]
                    val headers = mutableMapOf<String, String>()
                    var length = 0
                    while (true) {
                        val line = reader.readLine()
                        if (line.isNullOrEmpty()) break
                        val (name, value) = line.split(":", limit = 2).map(String::trim)
                        headers[name.lowercase()] = value
                        if (name.equals("Content-Length", ignoreCase = true)) length = value.toInt()
                    }
                    repeat(length) { reader.read() }
                    synchronized(seen) {
                        seen += mapOf(
                            "path" to path,
                            "id" to headers["x-mantau-agent-id"],
                            "secret" to headers["x-mantau-agent-secret"],
                        )
                    }
                    val body = response.toByteArray()
                    it.getOutputStream().apply {
                        val crlf = "\r\n"
                        write(("HTTP/1.1 $status X${crlf}Content-Type: application/json$crlf" +
                            "Content-Length: ${body.size}${crlf}Connection: close$crlf$crlf").toByteArray())
                        write(body)
                        flush()
                    }
                }
            }
        }
    }

    @After
    fun stop() = server.close()

    private val url get() = "http://127.0.0.1:${server.localPort}"

    @Test
    fun firstEnrollmentSendsNoProof() {
        response = """{"agent_id":"agent-1","secret":"s1","claim_code":"CODE1"}"""
        val enrollment = ControlPlaneClient().enroll(url, "agent-1")
        assertEquals("CODE1", enrollment.claimCode)
        assertNull(seen.single()["secret"])
    }

    @Test
    fun rotationProvesTheCurrentSecret() {
        response = """{"agent_id":"agent-1","secret":"s2"}"""
        val enrollment = ControlPlaneClient().enroll(url, "agent-1", currentSecret = "s1")
        assertEquals("s2", enrollment.secret)
        assertEquals("agent-1", seen.single()["id"])
        assertEquals("s1", seen.single()["secret"])
    }

    @Test
    fun takenIdIsReportedDistinctly() {
        status = 409
        response = """{"detail":"agent_id_taken"}"""
        try {
            ControlPlaneClient().enroll(url, "agent-1")
            fail("expected AgentIdTakenException")
        } catch (_: AgentIdTakenException) {
        }
    }

    @Test
    fun claimCodeRefreshUsesAgentCredentials() {
        response = """{"claim_code":"FRESH1","expires_at":"2026-09-23T00:00:00Z"}"""
        assertEquals("FRESH1", ControlPlaneClient().refreshClaimCode(url, "agent-1", "s1"))
        assertEquals("/agent-control/claim-code", seen.single()["path"])
        assertEquals("s1", seen.single()["secret"])
    }

    @Test
    fun claimedAgentGetsNoNewCode() {
        status = 409
        response = """{"detail":"agent_already_claimed"}"""
        try {
            ControlPlaneClient().refreshClaimCode(url, "agent-1", "s1")
            fail("expected AlreadyClaimedException")
        } catch (_: AlreadyClaimedException) {
        }
    }
}
