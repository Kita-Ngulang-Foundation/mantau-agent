package id.mantau.agent.rtsp

import kotlin.math.min
import kotlin.random.Random

class ReconnectPolicy(
    private val baseMs: Long = 500,
    private val capMs: Long = 30_000,
    private val random: (Long) -> Long = { upper -> Random.nextLong(upper + 1) },
) {
    private var attempt = 0

    init {
        require(baseMs > 0 && capMs >= baseMs)
    }

    fun nextDelayMs(): Long {
        val exponent = min(attempt, 30)
        val bound = min(capMs, baseMs * (1L shl exponent).coerceAtMost(capMs / baseMs + 1))
        attempt++
        return random(bound).coerceIn(0, bound)
    }

    fun reset() {
        attempt = 0
    }

    val attempts: Int get() = attempt
}

enum class RtspState { STOPPED, CONNECTING, AUTHENTICATING, CONNECTED, STREAMING, BACKING_OFF }

class RtspStateMachine {
    var state: RtspState = RtspState.STOPPED
        private set

    fun transition(next: RtspState) {
        val allowed = when (state) {
            RtspState.STOPPED -> setOf(RtspState.CONNECTING)
            RtspState.CONNECTING -> setOf(RtspState.AUTHENTICATING, RtspState.CONNECTED, RtspState.BACKING_OFF, RtspState.STOPPED)
            RtspState.AUTHENTICATING -> setOf(RtspState.CONNECTED, RtspState.BACKING_OFF, RtspState.STOPPED)
            RtspState.CONNECTED -> setOf(RtspState.STREAMING, RtspState.BACKING_OFF, RtspState.STOPPED)
            RtspState.STREAMING -> setOf(RtspState.BACKING_OFF, RtspState.STOPPED)
            RtspState.BACKING_OFF -> setOf(RtspState.CONNECTING, RtspState.STOPPED)
        }
        require(next in allowed) { "Invalid RTSP transition: $state -> $next" }
        state = next
    }
}

