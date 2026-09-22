package id.mantau.agent.network

import id.mantau.agent.model.CommandResult
import id.mantau.agent.model.ControlCommand
import id.mantau.agent.model.Enrollment
import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

class ControlPlaneClient(
    private val connectTimeoutMs: Int = 10_000,
    private val readTimeoutMs: Int = 15_000,
) {
    fun enroll(serverUrl: String, agentId: String): Enrollment {
        val body = request(
            serverUrl = serverUrl,
            path = "/agents/enroll",
            method = "POST",
            body = JSONObject().put("agent_id", agentId),
            accepted = setOf(201),
        ) ?: throw IOException("Enrollment returned no response")
        return Enrollment(
            agentId = body.getString("agent_id"),
            secret = body.getString("secret"),
            claimCode = body.optString("claim_code").takeIf(String::isNotBlank),
        )
    }

    fun poll(serverUrl: String, agentId: String, secret: String, status: JSONObject): ControlCommand? {
        val response = request(
            serverUrl = serverUrl,
            path = "/agent-control/commands/poll",
            method = "POST",
            body = JSONObject().put("status", status),
            headers = authHeaders(agentId, secret),
            accepted = setOf(200, 204),
        ) ?: return null
        return ControlCommand.fromJson(response)
    }

    fun submitResult(serverUrl: String, agentId: String, secret: String, result: CommandResult) {
        request(
            serverUrl = serverUrl,
            path = "/agent-control/commands/${encodePath(result.commandId)}/results",
            method = "POST",
            body = result.toJson(),
            headers = authHeaders(agentId, secret),
            accepted = setOf(204),
        )
    }

    private fun request(
        serverUrl: String,
        path: String,
        method: String,
        body: JSONObject,
        headers: Map<String, String> = emptyMap(),
        accepted: Set<Int>,
    ): JSONObject? {
        val connection = URL(serverUrl.trimEnd('/') + path).openConnection() as HttpURLConnection
        try {
            connection.requestMethod = method
            connection.connectTimeout = connectTimeoutMs
            connection.readTimeout = readTimeoutMs
            connection.doOutput = true
            connection.setRequestProperty("Content-Type", "application/json")
            connection.setRequestProperty("Accept", "application/json")
            headers.forEach(connection::setRequestProperty)
            connection.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
            val status = connection.responseCode
            if (status !in accepted) {
                connection.errorStream?.close()
                throw ControlPlaneException(status)
            }
            if (status == 204) return null
            val text = connection.inputStream.bufferedReader().use { it.readText() }
            return text.takeIf(String::isNotBlank)?.let(::JSONObject)
        } finally {
            connection.disconnect()
        }
    }

    private fun authHeaders(agentId: String, secret: String) = mapOf(
        "X-Mantau-Agent-ID" to agentId,
        "X-Mantau-Agent-Secret" to secret,
    )

    private fun encodePath(value: String): String {
        require(value.matches(Regex("[A-Za-z0-9._-]+"))) { "Invalid command ID" }
        return value
    }
}

class ControlPlaneException(val statusCode: Int) : IOException("Control plane returned HTTP $statusCode")

