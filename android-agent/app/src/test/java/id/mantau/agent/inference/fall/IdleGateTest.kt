package id.mantau.agent.inference.fall

import id.mantau.agent.inference.fall.IdleGate.Pose
import org.junit.Assert.assertEquals
import org.junit.Test

class IdleGateTest {
    private val size = IdleGate.THUMB_WIDTH * IdleGate.THUMB_HEIGHT
    private val still = IntArray(size) { 100 }

    private fun moved(offset: Int) = IntArray(size) { i -> if (i in offset until offset + 200) 200 else 100 }

    @Test
    fun tracksWhileMovingAndForTheHoldAfter() {
        val gate = IdleGate()
        assertEquals(Pose.TRACK, gate.update(still, 0))
        assertEquals(Pose.TRACK, gate.update(moved(0), 100))
        assertEquals(Pose.TRACK, gate.update(moved(400), 200))
        assertEquals(Pose.TRACK, gate.update(moved(400), 3_100))
    }

    @Test
    fun aStillSceneIsSampledStatelesslyTwiceASecond() {
        val gate = IdleGate()
        val seen = (0..80).map { i -> gate.update(still, i * 100L) }
        assertEquals(Pose.TRACK, seen[29]) // still within the 3 s hold
        assertEquals(Pose.SNAPSHOT, seen[30])
        assertEquals(Pose.SKIP, seen[31])
        assertEquals(Pose.SNAPSHOT, seen[35])
        assertEquals(11, seen.count { it == Pose.SNAPSHOT }) // 3.0 s .. 8.0 s
    }

    @Test
    fun motionReturnsToTracking() {
        val gate = IdleGate()
        for (i in 0..40) gate.update(still, i * 100L)
        assertEquals(Pose.TRACK, gate.update(moved(1000), 4_200))
        assertEquals(Pose.TRACK, gate.update(moved(1000), 4_300))
    }

    @Test
    fun sensorNoiseIsNotMotion() {
        val gate = IdleGate()
        gate.update(still, 0)
        val noisy = IntArray(size) { i -> 100 + (i % 7) - 3 }
        for (i in 1..40) gate.update(if (i % 2 == 0) noisy else still, i * 100L)
        assertEquals(Pose.SKIP, gate.update(still, 4_100))
    }
}
