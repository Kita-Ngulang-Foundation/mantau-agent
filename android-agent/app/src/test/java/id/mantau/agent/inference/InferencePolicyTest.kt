package id.mantau.agent.inference

import org.junit.Assert.assertEquals
import org.junit.Test

class InferencePolicyTest {
    private fun policy(cloud: Boolean) = InferenceModePolicy(cloudAvailable = { cloud })

    @Test
    fun `requested modes fall back to server inference`() {
        assertEquals("CLOUD", policy(cloud = true).select("AUTO").effective)
        assertEquals("CLOUD", policy(cloud = true).select("EDGE").effective)
        assertEquals("CLOUD", policy(cloud = true).select("HYBRID").effective)
        assertEquals("CLOUD", policy(cloud = true).select("CLOUD").effective)
    }
}
