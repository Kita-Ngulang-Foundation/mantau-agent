package id.mantau.agent.rtsp

import id.mantau.agent.BuildConfig
import id.mantau.agent.model.CameraConfig
import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.ByteArrayOutputStream
import java.io.EOFException
import java.io.IOException
import java.net.InetSocketAddress
import java.net.Socket
import java.net.SocketTimeoutException
import java.net.URI
import java.security.MessageDigest
import java.time.Duration
import java.time.Instant
import java.util.Base64
import java.util.Locale
import java.util.UUID
import java.util.concurrent.atomic.AtomicBoolean

class RtspException(message: String) : IOException(message)
class RtspAuthenticationException(message: String) : IOException(message)

class RtspClient(private val connectTimeoutMs: Int = 5_000,
                 private val streamIdleTimeoutMs: Long = 10_000) : AutoCloseable {
    @Volatile private var activeSocket: Socket? = null

    fun stream(
        camera: CameraConfig,
        password: String?,
        frames: LatestFrameBuffer,
        cancelled: () -> Boolean,
        listener: Listener = Listener.NONE,
    ) {
        camera.validate()
        val socket = Socket()
        activeSocket = socket
        try {
            listener.onState(RtspState.CONNECTING)
            socket.connect(InetSocketAddress(camera.host, camera.port), connectTimeoutMs)
            socket.soTimeout = 1_000
            socket.tcpNoDelay = true
            val input = BufferedInputStream(socket.getInputStream(), 64 * 1024)
            val output = BufferedOutputStream(socket.getOutputStream())
            val connection = Connection(input, output, camera.username, password, listener)
            val streamUrl = "rtsp://${camera.host}:${camera.port}${camera.preferredPath()}"

            connection.requestAuthenticated("OPTIONS", streamUrl)
            listener.onState(RtspState.CONNECTED)
            val describe = connection.requestAuthenticated(
                "DESCRIBE", streamUrl, mapOf("Accept" to "application/sdp"),
            )
            val selectedTrack = selectH264Track(describe.body, describe.headers["content-base"] ?: streamUrl)
            listener.onFormat(selectedTrack.format)
            val setup = connection.requestAuthenticated(
                "SETUP",
                selectedTrack.url,
                mapOf("Transport" to "RTP/AVP/TCP;unicast;interleaved=0-1"),
            )
            connection.session = setup.headers["session"]?.substringBefore(';')?.trim()
                ?: throw RtspException("RTSP SETUP response omitted Session")
            connection.requestAuthenticated("PLAY", streamUrl)
            listener.onState(RtspState.STREAMING)
            receiveInterleaved(input, frames, cancelled, listener)
        } finally {
            activeSocket = null
            runCatching { socket.close() }
        }
    }

    fun captureOne(
        camera: CameraConfig,
        password: String?,
        timeout: Duration = Duration.ofSeconds(8),
        cancelled: () -> Boolean = { false },
    ): EncodedFrame {
        val frames = LatestFrameBuffer(1)
        val deadline = System.nanoTime() + timeout.toNanos()
        stream(camera, password, frames, { cancelled() || frames.size > 0 || System.nanoTime() >= deadline })
        return frames.latest() ?: throw RtspException("No H.264 frame arrived before timeout")
    }

    override fun close() {
        runCatching { activeSocket?.close() }
        activeSocket = null
    }

    private fun receiveInterleaved(
        input: BufferedInputStream,
        frames: LatestFrameBuffer,
        cancelled: () -> Boolean,
        listener: Listener,
    ) {
        var lastFrameAt = System.nanoTime()
        val assembler = H264FrameAssembler(frames, onFrame = {
            lastFrameAt = System.nanoTime()
            listener.onFrame(it)
        })
        while (!cancelled()) {
            if (System.nanoTime() - lastFrameAt >= streamIdleTimeoutMs * 1_000_000) {
                throw RtspException("Camera stopped delivering video frames")
            }
            val first = try {
                input.read()
            } catch (_: SocketTimeoutException) {
                continue
            }
            if (first < 0) throw EOFException("RTSP stream closed")
            if (first != '$'.code) {
                discardRtspMessage(input)
                continue
            }
            val channel = input.read()
            val lengthHigh = input.read()
            val lengthLow = input.read()
            if (channel < 0 || lengthHigh < 0 || lengthLow < 0) throw EOFException("Truncated interleaved header")
            val length = lengthHigh shl 8 or lengthLow
            val packet = input.readExactly(length)
            if (channel == 0) assembler.accept(packet)
        }
    }

    private fun discardRtspMessage(input: BufferedInputStream) {
        var matched = 0
        val delimiter = byteArrayOf('\r'.code.toByte(), '\n'.code.toByte(), '\r'.code.toByte(), '\n'.code.toByte())
        while (true) {
            val current = input.read()
            if (current < 0) return
            matched = if (current.toByte() == delimiter[matched]) matched + 1 else 0
            if (matched == delimiter.size) return
        }
    }

    private data class SelectedTrack(val url: String, val format: VideoFormat)

    private fun selectH264Track(sdp: ByteArray, baseUrl: String): SelectedTrack {
        val lines = sdp.toString(Charsets.UTF_8).lines().map(String::trim)
        var video = false
        var h264 = false
        var control: String? = null
        var width = 640
        var height = 480
        var sps: ByteArray? = null
        var pps: ByteArray? = null
        fun selected(): SelectedTrack? = if (video && h264 && !control.isNullOrBlank()) {
            SelectedTrack(resolveControl(baseUrl, control!!), VideoFormat(width, height, sps, pps))
        } else null
        for (line in lines + "m=end") {
            if (line.startsWith("m=")) {
                selected()?.let { return it }
                video = line.startsWith("m=video")
                h264 = false
                control = null
                width = 640
                height = 480
                sps = null
                pps = null
            } else if (video && line.startsWith("a=rtpmap:") && line.contains("H264", ignoreCase = true)) {
                h264 = true
            } else if (video && line.startsWith("a=control:")) {
                control = line.substringAfter("a=control:").trim()
            } else if (video && line.startsWith("a=framesize:")) {
                Regex("(\\d+)[-x](\\d+)").find(line.substringAfter(' '))?.let {
                    width = it.groupValues[1].toInt()
                    height = it.groupValues[2].toInt()
                }
            } else if (video && line.startsWith("a=fmtp:") && "sprop-parameter-sets=" in line) {
                val encoded = line.substringAfter("sprop-parameter-sets=").substringBefore(';').split(',')
                sps = encoded.getOrNull(0)?.let(::decodeCodecData)
                pps = encoded.getOrNull(1)?.let(::decodeCodecData)
            }
        }
        throw RtspException("Camera SDP has no supported H.264 video track")
    }

    private fun decodeCodecData(value: String): ByteArray? = runCatching {
        byteArrayOf(0, 0, 0, 1) + Base64.getDecoder().decode(value.trim())
    }.getOrNull()

    private fun resolveControl(base: String, control: String): String {
        if (control.startsWith("rtsp://", ignoreCase = true)) return control
        val uri = URI(base)
        if (control.startsWith('/')) return "${uri.scheme}://${uri.authority}$control"
        return base.trimEnd('/') + "/" + control
    }

    interface Listener {
        fun onState(state: RtspState) {}
        fun onFormat(format: VideoFormat) {}
        fun onFrame(frame: EncodedFrame) {}

        companion object { val NONE = object : Listener {} }
    }

    private class Connection(
        private val input: BufferedInputStream,
        private val output: BufferedOutputStream,
        private val username: String?,
        private val password: String?,
        private val listener: Listener,
    ) {
        var session: String? = null
        private var sequence = 1
        private var auth: Auth? = null

        fun requestAuthenticated(method: String, url: String, headers: Map<String, String> = emptyMap()): Response {
            var response = request(method, url, headers)
            if (response.status == 401) {
                if (username.isNullOrEmpty()) throw RtspAuthenticationException("Camera requires credentials")
                listener.onState(RtspState.AUTHENTICATING)
                val challenge = response.headers["www-authenticate"]
                    ?: throw RtspAuthenticationException("Camera omitted authentication challenge")
                auth = Auth.fromChallenge(challenge, username, password.orEmpty())
                response = request(method, url, headers)
            }
            if (response.status !in 200..299) {
                if (response.status == 401) throw RtspAuthenticationException("Camera rejected credentials")
                throw RtspException("RTSP $method returned ${response.status}")
            }
            return response
        }

        private fun request(method: String, url: String, headers: Map<String, String>): Response {
            val requestHeaders = linkedMapOf(
                "CSeq" to sequence++.toString(),
                "User-Agent" to "Mantau-Android-Agent/${BuildConfig.VERSION_NAME}",
            )
            session?.let { requestHeaders["Session"] = it }
            auth?.header(method, url)?.let { requestHeaders["Authorization"] = it }
            requestHeaders.putAll(headers)
            val text = buildString {
                append("$method $url RTSP/1.0\r\n")
                requestHeaders.forEach { (name, value) -> append("$name: $value\r\n") }
                append("\r\n")
            }
            output.write(text.toByteArray(Charsets.UTF_8))
            output.flush()
            return readResponse(input)
        }
    }

    private data class Response(val status: Int, val headers: Map<String, String>, val body: ByteArray)

    private sealed interface Auth {
        fun header(method: String, url: String): String

        data class Basic(private val token: String) : Auth {
            override fun header(method: String, url: String) = "Basic $token"
        }

        class Digest(
            private val username: String,
            private val password: String,
            private val realm: String,
            private val nonce: String,
            private val opaque: String?,
            private val qop: String?,
        ) : Auth {
            private var nonceCount = 0

            override fun header(method: String, url: String): String {
                nonceCount++
                val nc = "%08x".format(Locale.US, nonceCount)
                val cnonce = UUID.randomUUID().toString().replace("-", "")
                val ha1 = md5("$username:$realm:$password")
                val ha2 = md5("$method:$url")
                val response = if (qop == "auth") md5("$ha1:$nonce:$nc:$cnonce:auth:$ha2")
                    else md5("$ha1:$nonce:$ha2")
                return buildString {
                    append("Digest username=\"").append(escape(username)).append("\"")
                    append(", realm=\"").append(escape(realm)).append("\"")
                    append(", nonce=\"").append(escape(nonce)).append("\"")
                    append(", uri=\"").append(escape(url)).append("\"")
                    append(", response=\"").append(response).append("\"")
                    append(", algorithm=MD5")
                    if (qop == "auth") append(", qop=auth, nc=$nc, cnonce=\"$cnonce\"")
                    opaque?.let { append(", opaque=\"").append(escape(it)).append("\"") }
                }
            }
        }

        companion object {
            fun fromChallenge(challenge: String, username: String, password: String): Auth {
                if (challenge.startsWith("Basic", ignoreCase = true)) {
                    return Basic(Base64.getEncoder().encodeToString("$username:$password".toByteArray()))
                }
                if (!challenge.startsWith("Digest", ignoreCase = true)) {
                    throw RtspAuthenticationException("Unsupported RTSP authentication scheme")
                }
                val params = Regex("([A-Za-z0-9_-]+)=(?:\\\"([^\\\"]*)\\\"|([^,\\s]+))")
                    .findAll(challenge).associate { match ->
                        match.groupValues[1].lowercase() to (match.groupValues[2].ifEmpty { match.groupValues[3] })
                    }
                val algorithm = params["algorithm"] ?: "MD5"
                if (!algorithm.equals("MD5", ignoreCase = true)) {
                    throw RtspAuthenticationException("Unsupported RTSP digest algorithm")
                }
                val qop = params["qop"]?.split(',')?.map(String::trim)?.firstOrNull { it == "auth" }
                return Digest(
                    username, password,
                    params["realm"] ?: throw RtspAuthenticationException("Digest realm is missing"),
                    params["nonce"] ?: throw RtspAuthenticationException("Digest nonce is missing"),
                    params["opaque"], qop,
                )
            }
        }
    }

    companion object {
        private fun readResponse(input: BufferedInputStream): Response {
            val statusLine = input.readAsciiLine() ?: throw EOFException("No RTSP response")
            val status = Regex("RTSP/\\d\\.\\d\\s+(\\d{3})").find(statusLine)?.groupValues?.get(1)?.toInt()
                ?: throw RtspException("Malformed RTSP status line")
            val headers = linkedMapOf<String, String>()
            while (true) {
                val line = input.readAsciiLine() ?: throw EOFException("Truncated RTSP headers")
                if (line.isEmpty()) break
                val separator = line.indexOf(':')
                if (separator > 0) headers[line.substring(0, separator).trim().lowercase()] = line.substring(separator + 1).trim()
            }
            val length = headers["content-length"]?.toIntOrNull()?.coerceAtLeast(0) ?: 0
            return Response(status, headers, input.readExactly(length))
        }

        private fun BufferedInputStream.readAsciiLine(): String? {
            val bytes = ByteArrayOutputStream()
            while (true) {
                val value = read()
                if (value < 0) return if (bytes.size() == 0) null else bytes.toString(Charsets.UTF_8.name())
                if (value == '\n'.code) return bytes.toByteArray().dropLastWhile { it == '\r'.code.toByte() }
                    .toByteArray().toString(Charsets.UTF_8)
                bytes.write(value)
            }
        }

        private fun BufferedInputStream.readExactly(length: Int): ByteArray {
            val bytes = ByteArray(length)
            var offset = 0
            while (offset < length) {
                val count = read(bytes, offset, length - offset)
                if (count < 0) throw EOFException("Truncated RTSP payload")
                offset += count
            }
            return bytes
        }

        private fun md5(value: String): String = MessageDigest.getInstance("MD5")
            .digest(value.toByteArray(Charsets.UTF_8)).joinToString("") { "%02x".format(it) }

        private fun escape(value: String) = value.replace("\\", "\\\\").replace("\"", "\\\"")
    }
}

private class H264FrameAssembler(
    private val frames: LatestFrameBuffer,
    private val onFrame: (EncodedFrame) -> Unit,
    private val maxFrameBytes: Int = 2 * 1024 * 1024,
) {
    private var accessUnit = ByteArrayOutputStream()
    private var timestamp = 0L

    fun accept(packet: ByteArray) {
        val rtp = parseRtp(packet) ?: return
        timestamp = rtp.timestamp
        val payload = rtp.payload
        if (payload.isEmpty()) return
        when (payload[0].toInt() and 0x1f) {
            in 1..23 -> appendNal(payload)
            24 -> appendStapA(payload)
            28 -> appendFuA(payload)
        }
        if (accessUnit.size() > maxFrameBytes) accessUnit = ByteArrayOutputStream()
        if (rtp.marker && accessUnit.size() > 0) {
            val frame = EncodedFrame(accessUnit.toByteArray(), Instant.now(), timestamp)
            frames.offer(frame)
            onFrame(frame)
            accessUnit = ByteArrayOutputStream()
        }
    }

    private fun appendNal(nal: ByteArray) {
        accessUnit.write(START_CODE)
        accessUnit.write(nal)
    }

    private fun appendStapA(payload: ByteArray) {
        var offset = 1
        while (offset + 2 <= payload.size) {
            val length = ((payload[offset].toInt() and 0xff) shl 8) or (payload[offset + 1].toInt() and 0xff)
            offset += 2
            if (length <= 0 || offset + length > payload.size) return
            accessUnit.write(START_CODE)
            accessUnit.write(payload, offset, length)
            offset += length
        }
    }

    private fun appendFuA(payload: ByteArray) {
        if (payload.size < 3) return
        val indicator = payload[0].toInt() and 0xff
        val header = payload[1].toInt() and 0xff
        val start = header and 0x80 != 0
        if (start) {
            accessUnit.write(START_CODE)
            accessUnit.write((indicator and 0xe0) or (header and 0x1f))
        }
        accessUnit.write(payload, 2, payload.size - 2)
    }

    private data class Rtp(val marker: Boolean, val timestamp: Long, val payload: ByteArray)

    private fun parseRtp(packet: ByteArray): Rtp? {
        if (packet.size < 12 || ((packet[0].toInt() and 0xff) ushr 6) != 2) return null
        val csrcCount = packet[0].toInt() and 0x0f
        val extension = packet[0].toInt() and 0x10 != 0
        val padding = packet[0].toInt() and 0x20 != 0
        var offset = 12 + csrcCount * 4
        if (offset > packet.size) return null
        if (extension) {
            if (offset + 4 > packet.size) return null
            val words = ((packet[offset + 2].toInt() and 0xff) shl 8) or (packet[offset + 3].toInt() and 0xff)
            offset += 4 + words * 4
        }
        val paddingLength = if (padding) packet.last().toInt() and 0xff else 0
        val end = packet.size - paddingLength
        if (offset >= end) return null
        val timestamp = (4..7).fold(0L) { value, index -> value shl 8 or (packet[index].toLong() and 0xff) }
        return Rtp(packet[1].toInt() and 0x80 != 0, timestamp, packet.copyOfRange(offset, end))
    }

    companion object { private val START_CODE = byteArrayOf(0, 0, 0, 1) }
}
