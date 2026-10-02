plugins { id("com.android.application"); id("org.jetbrains.kotlin.android") }
android {
    namespace = "com.pencilfinely.expmonitor"
    compileSdk = 35
    defaultConfig {
        applicationId = "com.pencilfinely.expmonitor"
        minSdk = 28
        targetSdk = 35
        versionCode = 2
        versionName = "0.5.0"
    }
    compileOptions { sourceCompatibility = JavaVersion.VERSION_17; targetCompatibility = JavaVersion.VERSION_17 }
    kotlinOptions { jvmTarget = "17" }
}
