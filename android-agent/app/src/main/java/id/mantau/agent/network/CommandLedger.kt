package id.mantau.agent.network

import android.content.Context
import id.mantau.agent.model.CommandResult
import id.mantau.agent.model.ControlCommand
import id.mantau.agent.storage.SecretStore
import org.json.JSONArray
import org.json.JSONObject

class CommandLedger(
    context: Context,
    private val secrets: SecretStore,
    private val keep: Int = 128,
) {
    private val preferences = context.getSharedPreferences("command_ledger", Context.MODE_PRIVATE)

    @Synchronized
    fun completed(commandId: String): CommandResult? =
        preferences.getString("result.$commandId", null)?.let { CommandResult.fromJson(JSONObject(it)) }

    @Synchronized
    fun inflight(commandId: String): ControlCommand? =
        secrets.get("command.$commandId")?.let { ControlCommand.fromJson(JSONObject(it)) }

    @Synchronized
    fun putInflight(command: ControlCommand) {
        secrets.put("command.${command.commandId}", command.toJson().toString())
    }

    @Synchronized
    fun putCompleted(result: CommandResult) {
        secrets.remove("command.${result.commandId}")
        val ids = resultIds().filterNot { it == result.commandId }.toMutableList()
        ids += result.commandId
        while (ids.size > keep) {
            preferences.edit().remove("result.${ids.removeAt(0)}").commit()
        }
        val array = JSONArray().also { target -> ids.forEach(target::put) }
        check(preferences.edit()
            .putString("result.${result.commandId}", result.toJson().toString())
            .putString("result_ids", array.toString())
            .commit()) { "Could not persist command result" }
    }

    private fun resultIds(): List<String> {
        val array = JSONArray(preferences.getString("result_ids", "[]"))
        return (0 until array.length()).map(array::getString)
    }
}
