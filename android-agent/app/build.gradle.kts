plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

val defaultServerUrl = providers.gradleProperty("mantau.server.url")
    .orElse("http://192.168.1.2:8100").get()
require('"' !in defaultServerUrl && '\\' !in defaultServerUrl) {
    "mantau.server.url contains an invalid character"
}

android {
    namespace = "id.mantau.agent"
    compileSdk = 36

    defaultConfig {
        applicationId = "id.mantau.agent"
        minSdk = 26
        targetSdk = 36
        versionCode = 2
        versionName = "0.2.0"
        buildConfigField("String", "DEFAULT_SERVER_URL", "\"$defaultServerUrl\"")

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    testOptions {
        unitTests.isReturnDefaultValues = true
    }

    buildFeatures {
        buildConfig = true
    }
}

dependencies {
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
}

tasks.withType<Test>().configureEach {
    systemProperty(
        "mantau.contract.fixtures",
        rootProject.file("../../mantau-core/src/mantau_core/contracts/fixtures/v1").absolutePath,
    )
    systemProperty(
        "mantau.core.activity.fixtures",
        rootProject.file("../../mantau-core/src/mantau_core/activity/fixtures/activity_sequences").absolutePath,
    )
    systemProperty(
        "mantau.protocol.examples",
        rootProject.file("../../protocol/examples").absolutePath,
    )
}
