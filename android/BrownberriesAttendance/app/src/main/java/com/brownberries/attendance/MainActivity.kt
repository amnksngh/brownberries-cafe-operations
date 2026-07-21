package com.brownberries.attendance

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.Build as AndroidBuild
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.brownberries.attendance.databinding.ActivityMainBinding
import kotlinx.coroutines.launch

class MainActivity : AppCompatActivity() {
    private lateinit var binding: ActivityMainBinding
    private lateinit var store: SessionStore
    private val api = MobileAttendanceApi()

    private val permissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions(),
    ) { _ ->
        if (hasCorePermissions()) {
            store.monitoringEnabled = true
            LocationMonitorService.start(this)
            renderState("Monitoring started")
        } else {
            toast("Location permission is required for auto attendance.")
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        store = SessionStore(this)
        binding.serverUrlInput.setText(store.baseUrl)
        binding.deviceNameInput.setText("${AndroidBuild.MANUFACTURER} ${AndroidBuild.MODEL}")

        binding.loginButton.setOnClickListener { doLogin() }
        binding.logoutButton.setOnClickListener { doLogout() }
        binding.refreshButton.setOnClickListener { refreshBootstrap() }
        binding.startMonitoringButton.setOnClickListener { ensurePermissionsAndStartMonitoring() }
        binding.stopMonitoringButton.setOnClickListener {
            store.monitoringEnabled = false
            LocationMonitorService.stop(this)
            renderState("Monitoring stopped")
        }
        binding.manualCheckoutButton.setOnClickListener { manualCheckout() }

        renderState()
        if (store.token.isNotBlank()) {
            refreshBootstrap(silent = true)
        }
    }

    private fun doLogin() {
        val baseUrl = binding.serverUrlInput.text?.toString()?.trim().orEmpty()
        val email = binding.emailInput.text?.toString()?.trim().orEmpty()
        val password = binding.passwordInput.text?.toString().orEmpty()
        val deviceName = binding.deviceNameInput.text?.toString()?.trim().orEmpty()
        if (baseUrl.isBlank() || email.isBlank() || password.isBlank()) {
            toast("Server URL, email, and password are required.")
            return
        }
        lifecycleScope.launch {
            try {
                val bootstrap = api.login(
                    baseUrl = baseUrl,
                    email = email,
                    password = password,
                    deviceId = store.deviceId(),
                    deviceName = deviceName.ifBlank { "${AndroidBuild.MANUFACTURER} ${AndroidBuild.MODEL}" },
                )
                store.baseUrl = baseUrl
                if (bootstrap.token.isNotBlank()) {
                    store.token = bootstrap.token
                }
                store.applyBootstrap(bootstrap)
                toast("Logged in as ${bootstrap.userName}")
                renderState("Login successful")
                ensurePermissionsAndStartMonitoring()
            } catch (t: Throwable) {
                toast(t.message ?: "Login failed")
                renderState("Login failed")
            }
        }
    }

    private fun refreshBootstrap(silent: Boolean = false) {
        if (store.token.isBlank()) {
            if (!silent) toast("Login first.")
            return
        }
        lifecycleScope.launch {
            try {
                val bootstrap = api.bootstrap(store.baseUrl, store.token)
                store.applyBootstrap(bootstrap)
                renderState("Attendance settings refreshed")
            } catch (t: Throwable) {
                if (!silent) toast(t.message ?: "Refresh failed")
                renderState("Refresh failed")
            }
        }
    }

    private fun manualCheckout() {
        if (store.token.isBlank() || !store.checkedIn) {
            toast("No active attendance session.")
            return
        }
        lifecycleScope.launch {
            try {
                val lat = store.lastKnownLat.takeIf { it != 0.0 }
                val lng = store.lastKnownLng.takeIf { it != 0.0 }
                val session = api.checkOut(
                    baseUrl = store.baseUrl,
                    token = store.token,
                    lat = lat,
                    lng = lng,
                    capturedAtIso = java.time.ZonedDateTime.now(java.time.ZoneId.of("Asia/Kolkata")).toString(),
                    reason = "manual",
                )
                store.applyAttendanceState(session)
                renderState("Manual check-out synced")
                toast("Checked out successfully")
            } catch (t: Throwable) {
                toast(t.message ?: "Manual check-out failed")
                renderState("Manual check-out failed")
            }
        }
    }

    private fun doLogout() {
        lifecycleScope.launch {
            try {
                if (store.token.isNotBlank()) {
                    runCatching { api.logout(store.baseUrl, store.token) }
                }
            } finally {
                LocationMonitorService.stop(this@MainActivity)
                store.clearAuth()
                renderState("Logged out")
            }
        }
    }

    private fun ensurePermissionsAndStartMonitoring() {
        if (hasCorePermissions()) {
            store.monitoringEnabled = true
            LocationMonitorService.start(this)
            renderState("Monitoring started")
            return
        }
        val permissions = mutableListOf(
            Manifest.permission.ACCESS_FINE_LOCATION,
            Manifest.permission.ACCESS_COARSE_LOCATION,
        )
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            permissions += Manifest.permission.POST_NOTIFICATIONS
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            permissions += Manifest.permission.ACCESS_BACKGROUND_LOCATION
        }
        permissionLauncher.launch(permissions.toTypedArray())
    }

    private fun hasCorePermissions(): Boolean {
        val fine = ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED
        val coarse = ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_COARSE_LOCATION) == PackageManager.PERMISSION_GRANTED
        return fine || coarse
    }

    private fun renderState(message: String? = null) {
        binding.serverUrlInput.setTextIfDifferent(store.baseUrl)
        binding.statusText.text = message ?: buildString {
            if (store.userName.isBlank()) {
                append("Not logged in")
            } else {
                append("Signed in as ${store.userName} (${store.userEmail})")
                if (store.monitoringEnabled) append("\nMonitoring is enabled")
            }
            if (store.lastSyncMessage.isNotBlank()) append("\n${store.lastSyncMessage}")
        }
        binding.geofenceText.text = "Cafe geofence: ${"%.0f".format(store.radiusM)} m • ${store.cafeLat}, ${store.cafeLng}"
        binding.shiftText.text = "Assigned shift: ${store.shiftStart} - ${store.shiftEnd}"
        binding.sessionText.text = if (store.checkedIn) {
            "Active session • ${store.activeStatusLabel.ifBlank { "Checked in" }} • ${store.activeAttendanceDate}"
        } else {
            "Attendance session inactive"
        }
    }

    private fun toast(message: String) {
        Toast.makeText(this, message, Toast.LENGTH_SHORT).show()
    }
}

private fun com.google.android.material.textfield.TextInputEditText.setTextIfDifferent(next: String) {
    val current = text?.toString().orEmpty()
    if (current != next) {
        setText(next)
        setSelection(next.length)
    }
}
