package id.mantau.agent.storage

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

interface SecretStore {
    fun put(name: String, value: String)
    fun get(name: String): String?
    fun remove(name: String)
}

class AndroidKeystoreSecretStore(context: Context) : SecretStore {
    private val preferences = context.getSharedPreferences("encrypted_secrets", Context.MODE_PRIVATE)

    override fun put(name: String, value: String) {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, key())
        val encrypted = cipher.doFinal(value.toByteArray(Charsets.UTF_8))
        val packed = ByteArray(cipher.iv.size + encrypted.size)
        cipher.iv.copyInto(packed)
        encrypted.copyInto(packed, cipher.iv.size)
        preferences.edit().putString(name, Base64.encodeToString(packed, Base64.NO_WRAP)).commit()
    }

    override fun get(name: String): String? {
        val encoded = preferences.getString(name, null) ?: return null
        val packed = Base64.decode(encoded, Base64.NO_WRAP)
        require(packed.size > IV_BYTES) { "Encrypted secret is truncated" }
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, packed.copyOfRange(0, IV_BYTES)))
        return cipher.doFinal(packed.copyOfRange(IV_BYTES, packed.size)).toString(Charsets.UTF_8)
    }

    override fun remove(name: String) {
        preferences.edit().remove(name).commit()
    }

    private fun key(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(KEY_ALIAS, null) as? SecretKey)?.let { return it }
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        generator.init(
            KeyGenParameterSpec.Builder(
                KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
            ).setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .setRandomizedEncryptionRequired(true)
                .build(),
        )
        return generator.generateKey()
    }

    companion object {
        private const val KEY_ALIAS = "mantau-agent-secrets-v1"
        private const val TRANSFORMATION = "AES/GCM/NoPadding"
        private const val IV_BYTES = 12
    }
}

