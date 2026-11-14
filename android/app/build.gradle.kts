plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

val releaseSigning = listOf(
    System.getenv("ANDROID_KEYSTORE_PATH"),
    System.getenv("ANDROID_KEYSTORE_PASSWORD"),
    System.getenv("ANDROID_KEY_ALIAS"),
    System.getenv("ANDROID_KEY_PASSWORD"),
)
val releaseSigningConfigured = releaseSigning.all { !it.isNullOrBlank() }
if (releaseSigning.any { !it.isNullOrBlank() } && !releaseSigningConfigured) {
    throw GradleException("Set all Android signing environment variables to sign release builds")
}

android {
    namespace = "it.zorn.ledgepill"
    compileSdk = 34

    defaultConfig {
        applicationId = "it.zorn.ledgepill"
        minSdk = 26
        targetSdk = 34
        versionCode = 7
        versionName = "1.0.0"
    }

    signingConfigs {
        if (releaseSigningConfigured) {
            create("release") {
                storeFile = file(releaseSigning[0]!!)
                storePassword = releaseSigning[1]
                keyAlias = releaseSigning[2]
                keyPassword = releaseSigning[3]
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            if (releaseSigningConfigured) {
                signingConfig = signingConfigs.getByName("release")
            }
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
}
