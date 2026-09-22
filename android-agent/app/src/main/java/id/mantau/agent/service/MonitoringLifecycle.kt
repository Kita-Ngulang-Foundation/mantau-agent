package id.mantau.agent.service

enum class ServiceLifecycleState { STOPPED, STARTING, RUNNING, STOPPING, FAILED }

class MonitoringLifecycle {
    var state: ServiceLifecycleState = ServiceLifecycleState.STOPPED
        private set

    fun beginStart(): Boolean {
        if (state == ServiceLifecycleState.STARTING || state == ServiceLifecycleState.RUNNING) return false
        check(state == ServiceLifecycleState.STOPPED || state == ServiceLifecycleState.FAILED)
        state = ServiceLifecycleState.STARTING
        return true
    }

    fun markRunning() {
        check(state == ServiceLifecycleState.STARTING)
        state = ServiceLifecycleState.RUNNING
    }

    fun beginStop(): Boolean {
        if (state == ServiceLifecycleState.STOPPED || state == ServiceLifecycleState.STOPPING) return false
        state = ServiceLifecycleState.STOPPING
        return true
    }

    fun markStopped() {
        check(state == ServiceLifecycleState.STOPPING || state == ServiceLifecycleState.STARTING)
        state = ServiceLifecycleState.STOPPED
    }

    fun markFailed() {
        state = ServiceLifecycleState.FAILED
    }
}

