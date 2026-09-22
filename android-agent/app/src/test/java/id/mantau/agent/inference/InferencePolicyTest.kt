package id.mantau.agent.inference

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.Instant

class InferencePolicyTest {
    @Test
    fun `AUTO and detector failure safely fall back to cloud`() {
        var failure: String? = "verified model artifact is missing"
        val policy = InferenceModePolicy(
            detectorAvailable = { failure == null }, detectorFailure = { failure },
            thermal = ThermalStateProvider { ThermalState.NONE },
        )
        val selected = policy.select("AUTO")
        assertEquals("CLOUD", selected.effective)
        assertTrue(selected.reason.contains("model"))
        assertThrows(UnsupportedOperationException::class.java) { policy.select("EDGE") }
    }

    @Test
    fun `thermal pressure transitions supported edge to cloud`() {
        var thermal = ThermalState.NONE
        val policy = InferenceModePolicy(
            detectorAvailable = { true }, detectorFailure = { null },
            thermal = ThermalStateProvider { thermal },
        )
        assertEquals("EDGE", policy.select("AUTO").effective)
        thermal = ThermalState.SEVERE
        val selected = policy.select("EDGE")
        assertEquals("CLOUD", selected.effective)
        assertTrue(selected.reason.contains("Thermal severe"))
    }

    @Test
    fun `hybrid requires bounded event-correlated confirmation transport`() {
        val policy = InferenceModePolicy(
            detectorAvailable = { true }, detectorFailure = { null },
            confirmationAvailable = { false }, thermal = ThermalStateProvider { ThermalState.NONE },
        )
        assertThrows(UnsupportedOperationException::class.java) { policy.select("HYBRID") }
        val confirmations = HybridConfirmationPolicy(minIntervalMs = 5_000)
        assertTrue(confirmations.shouldUpload("event-1", 0))
        assertFalse(confirmations.shouldUpload("event-1", 10_000))
        assertFalse(confirmations.shouldUpload("event-2", 4_999))
        assertTrue(confirmations.shouldUpload("event-2", 5_000))
    }

    @Test
    fun `fall gate applies cooldown and track dedupe without synthetic events`() {
        val gate = FallEventGate("cam", cooldownMs = 1_000, dedupeMs = 5_000)
        val candidate = DetectionCandidate(.91, trackId = 7, signals = mapOf("velocity" to .5))
        assertNotNull(gate.accept(candidate, Instant.ofEpochMilli(0)))
        assertNull(gate.accept(candidate, Instant.ofEpochMilli(999)))
        assertNull(gate.accept(candidate, Instant.ofEpochMilli(1_001)))
        assertNotNull(gate.accept(candidate, Instant.ofEpochMilli(5_001)))
    }
}
