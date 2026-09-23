package id.mantau.agent.inference.fall

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.abs

/**
 * Per-person observations (the activity rules' input) must match what mantau-AI's
 * ObservationTracker recorded in every pose-sequence fixture: same track ids and
 * postures exactly, same box, motion and confidence to floating-point noise.
 */
class ObservationParityTest {
    private fun close(a: Double, b: Double) = abs(a - b) <= 1e-9 * maxOf(1.0, abs(b))

    @Test
    fun observationsMatchPython() {
        var compared = 0
        for (file in PoseSequences.files) {
            val sequence = PoseSequences.load(file.nameWithoutExtension)
            val shape = sequence.getJSONArray("image_shape")
            val expected = sequence.getJSONObject("expected").getJSONObject("rules_only")
                .getJSONArray("observations")
            val stage = FallRulesStage(PoseSequences.config(sequence))
            PoseSequences.frames(sequence).forEachIndexed { index, (t, people) ->
                val actual = requireNotNull(stage.perceive(people, t, shape.getInt(0), shape.getInt(1))).people!!
                val want = expected.getJSONArray(index)
                assertEquals("${file.name} frame $index: people", want.length(), actual.size)
                actual.forEachIndexed { i, o ->
                    val row = want.getJSONArray(i)
                    val where = "${file.name} frame $index person $i"
                    assertEquals("$where: track", row.getInt(0), o.trackId)
                    assertEquals("$where: posture", row.getString(5), o.posture.wire)
                    val numbers = listOf(1 to o.left, 2 to o.top, 3 to o.width, 4 to o.height,
                        6 to o.motion, 7 to o.confidence)
                    for ((column, value) in numbers) {
                        assertTrue("$where: column $column $value != ${row.getDouble(column)}",
                            close(value, row.getDouble(column)))
                    }
                    compared++
                }
            }
        }
        assertTrue(compared > 1000)
    }

    @Test
    fun postureCoversEveryKindInTheFixtures() {
        val seen = mutableSetOf<String>()
        for (file in PoseSequences.files) {
            val observations = PoseSequences.load(file.nameWithoutExtension).getJSONObject("expected")
                .getJSONObject("rules_only").getJSONArray("observations")
            for (i in 0 until observations.length()) {
                val frame = observations.getJSONArray(i)
                for (j in 0 until frame.length()) seen += frame.getJSONArray(j).getString(5)
            }
        }
        assertTrue(seen.containsAll(listOf("standing", "lying")))
    }
}
