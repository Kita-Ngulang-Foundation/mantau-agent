package id.mantau.agent.discovery

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.assertThrows
import org.junit.Test
import java.io.IOException

class DiscoveryTest {
    @Test
    fun `parser returns multiple matches and deduplicates hosts`() {
        val xml = """<?xml version="1.0"?>
            <s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
              xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
              <s:Body><d:ProbeMatches>
                <d:ProbeMatch><d:Types>dn:NetworkVideoTransmitter</d:Types>
                  <d:Scopes>onvif://www.onvif.org/name/Living%20Room</d:Scopes>
                  <d:XAddrs>http://192.168.1.42/onvif/device_service</d:XAddrs></d:ProbeMatch>
                <d:ProbeMatch><d:XAddrs>http://192.168.1.43/onvif/device_service</d:XAddrs></d:ProbeMatch>
                <d:ProbeMatch><d:XAddrs>http://192.168.1.42/duplicate</d:XAddrs></d:ProbeMatch>
              </d:ProbeMatches></s:Body>
            </s:Envelope>""".trimIndent().toByteArray()
        val parsed = WsDiscoveryParser.parse(xml)
        assertEquals(3, parsed.size)
        val unique = WsDiscoveryParser.deduplicate(parsed)
        assertEquals(listOf("192.168.1.42", "192.168.1.43"), unique.map { it.host })
        assertEquals("Living Room", unique.first().name)
    }

    @Test
    fun `parser rejects document types`() {
        assertThrows(IllegalArgumentException::class.java) {
            WsDiscoveryParser.parse("<!DOCTYPE x><x/>".toByteArray())
        }
    }

    @Test
    fun `multicast lock is always released after failure`() {
        val lease = FakeLease()
        assertThrows(IOException::class.java) {
            DiscoveryRunner(lease) { throw IOException("socket closed") }.run()
        }
        assertTrue(lease.acquired)
        assertTrue(lease.released)
        assertFalse(lease.isHeld)
    }

    @Test
    fun `multicast lock is released after cancelled empty discovery`() {
        val lease = FakeLease()
        assertEquals(emptyList<OnvifDevice>(), DiscoveryRunner(lease) { emptyList() }.run())
        assertTrue(lease.released)
    }

    private class FakeLease : MulticastLease {
        override var isHeld = false
        var acquired = false
        var released = false
        override fun acquire() { acquired = true; isHeld = true }
        override fun release() { released = true; isHeld = false }
    }
}

