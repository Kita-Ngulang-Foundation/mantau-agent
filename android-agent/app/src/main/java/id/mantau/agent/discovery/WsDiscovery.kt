package id.mantau.agent.discovery

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.wifi.WifiManager
import id.mantau.agent.model.DiscoveredCamera
import org.w3c.dom.Element
import java.io.ByteArrayInputStream
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.Socket
import java.net.SocketTimeoutException
import java.net.URI
import java.net.URLDecoder
import java.time.Duration
import java.util.UUID
import javax.xml.parsers.DocumentBuilderFactory

data class OnvifDevice(val xaddrs: List<String>, val types: String = "", val scopes: String = "") {
    val host: String?
        get() = xaddrs.firstNotNullOfOrNull { runCatching { URI(it).host }.getOrNull() }

    val name: String?
        get() = scopes.split(Regex("\\s+")).firstOrNull { "/name/" in it }?.substringAfterLast("/name/")
            ?.let { URLDecoder.decode(it, Charsets.UTF_8.name()) }
}

object WsDiscoveryParser {
    fun parse(bytes: ByteArray): List<OnvifDevice> {
        require(!bytes.toString(Charsets.UTF_8).contains("<!DOCTYPE", ignoreCase = true)) {
            "DOCTYPE is not permitted in WS-Discovery responses"
        }
        val factory = DocumentBuilderFactory.newInstance().apply {
            isNamespaceAware = true
            runCatching { setFeature("http://apache.org/xml/features/disallow-doctype-decl", true) }
            runCatching { setFeature("http://xml.org/sax/features/external-general-entities", false) }
            runCatching { setFeature("http://xml.org/sax/features/external-parameter-entities", false) }
            runCatching { setAttribute("http://javax.xml.XMLConstants/property/accessExternalDTD", "") }
            runCatching { setAttribute("http://javax.xml.XMLConstants/property/accessExternalSchema", "") }
        }
        val document = factory.newDocumentBuilder().parse(ByteArrayInputStream(bytes))
        val matches = document.getElementsByTagNameNS("*", "ProbeMatch")
        return (0 until matches.length).mapNotNull { index ->
            val match = matches.item(index) as? Element ?: return@mapNotNull null
            val xaddrs = match.text("XAddrs").split(Regex("\\s+")).filter(String::isNotBlank)
            xaddrs.takeIf(List<String>::isNotEmpty)?.let {
                OnvifDevice(it, match.text("Types"), match.text("Scopes"))
            }
        }
    }

    fun deduplicate(devices: Iterable<OnvifDevice>): List<OnvifDevice> {
        val found = linkedMapOf<String, OnvifDevice>()
        for (device in devices) {
            val key = device.host?.lowercase() ?: device.xaddrs.firstOrNull()?.lowercase() ?: continue
            found.putIfAbsent(key, device)
        }
        return found.values.toList()
    }

    private fun Element.text(localName: String): String =
        getElementsByTagNameNS("*", localName).item(0)?.textContent?.trim().orEmpty()
}

interface MulticastLease {
    val isHeld: Boolean
    fun acquire()
    fun release()
}

fun interface DiscoveryTransport {
    fun discover(): List<OnvifDevice>
}

class DiscoveryRunner(
    private val lease: MulticastLease,
    private val transport: DiscoveryTransport,
) {
    fun run(): List<OnvifDevice> {
        lease.acquire()
        return try {
            WsDiscoveryParser.deduplicate(transport.discover())
        } finally {
            if (lease.isHeld) lease.release()
        }
    }
}

class AndroidWsDiscovery(private val context: Context) {
    fun discover(timeout: Duration = Duration.ofSeconds(3), cancelled: () -> Boolean = { false }): List<DiscoveredCamera> {
        val network = requireWifiNetwork()
        val wifi = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
        val lock = wifi.createMulticastLock("mantau-onvif-discovery").apply { setReferenceCounted(false) }
        val lease = object : MulticastLease {
            override val isHeld: Boolean get() = lock.isHeld
            override fun acquire() = lock.acquire()
            override fun release() = lock.release()
        }
        val devices = DiscoveryRunner(lease) {
            exchange(network, timeout, cancelled)
        }.run()
        return devices.mapNotNull { device ->
            val host = device.host ?: return@mapNotNull null
            val reachable = rtspReachable(network, host, 554, cancelled)
            DiscoveredCamera(
                host = host,
                name = device.name,
                reachable = reachable,
                failureReason = if (reachable) null else "RTSP port did not accept a local Wi-Fi connection",
            )
        }
    }

    @Suppress("DEPRECATION") // allNetworks is required to find Wi-Fi underneath a default VPN.
    private fun requireWifiNetwork(): Network {
        val manager = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        val network = manager.allNetworks.firstOrNull {
            manager.getNetworkCapabilities(it)?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) == true
        }
        return requireNotNull(network) { "ONVIF discovery requires an active local Wi-Fi network" }
    }

    private fun exchange(network: Network, timeout: Duration, cancelled: () -> Boolean): List<OnvifDevice> {
        val socket = DatagramSocket(null)
        return try {
            socket.reuseAddress = true
            socket.bind(InetSocketAddress(0))
            network.bindSocket(socket)
            socket.soTimeout = 200
            val probe = probeMessage()
            socket.send(DatagramPacket(probe, probe.size, InetAddress.getByName(MULTICAST_HOST), PORT))
            val deadline = System.nanoTime() + timeout.toNanos()
            val devices = mutableListOf<OnvifDevice>()
            while (!cancelled() && System.nanoTime() < deadline) {
                val buffer = ByteArray(65_535)
                try {
                    val packet = DatagramPacket(buffer, buffer.size)
                    socket.receive(packet)
                    runCatching {
                        WsDiscoveryParser.parse(packet.data.copyOfRange(packet.offset, packet.offset + packet.length))
                    }.getOrNull()?.let(devices::addAll)
                } catch (_: SocketTimeoutException) {
                    // Short receive timeouts make cancellation prompt while the overall deadline remains bounded.
                }
            }
            devices
        } finally {
            socket.close()
        }
    }

    private fun rtspReachable(network: Network, host: String, port: Int, cancelled: () -> Boolean): Boolean {
        if (cancelled()) return false
        val socket = Socket()
        return try {
            network.bindSocket(socket)
            socket.connect(InetSocketAddress(host, port), 750)
            true
        } catch (_: Exception) {
            false
        } finally {
            runCatching { socket.close() }
        }
    }

    private fun probeMessage(): ByteArray = """<?xml version="1.0" encoding="UTF-8"?>
        <e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
          xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
          xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
          xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
          <e:Header><w:MessageID>uuid:${UUID.randomUUID()}</w:MessageID>
          <w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
          <w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>
          <e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body>
        </e:Envelope>""".trimIndent().toByteArray()

    companion object {
        private const val MULTICAST_HOST = "239.255.255.250"
        private const val PORT = 3702
    }
}
