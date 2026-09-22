package id.mantau.agent.service

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class MonitoringLifecycleTest {
    @Test
    fun `start and stop are idempotent at service boundary`() {
        val lifecycle = MonitoringLifecycle()
        assertTrue(lifecycle.beginStart())
        assertFalse(lifecycle.beginStart())
        lifecycle.markRunning()
        assertEquals(ServiceLifecycleState.RUNNING, lifecycle.state)
        assertTrue(lifecycle.beginStop())
        assertFalse(lifecycle.beginStop())
        lifecycle.markStopped()
        assertEquals(ServiceLifecycleState.STOPPED, lifecycle.state)
    }

    @Test
    fun `failed startup can be retried`() {
        val lifecycle = MonitoringLifecycle()
        lifecycle.beginStart()
        lifecycle.markFailed()
        assertTrue(lifecycle.beginStart())
        lifecycle.beginStop()
        lifecycle.markStopped()
    }
}

