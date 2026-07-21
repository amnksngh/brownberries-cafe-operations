package com.brownberries.attendance

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.location.Location
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import com.google.android.gms.location.FusedLocationProviderClient
import com.google.android.gms.location.LocationServices
import com.google.android.gms.location.Priority
import com.google.android.gms.tasks.CancellationTokenSource
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.tasks.await
import kotlinx.coroutines.withTimeoutOrNull
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter

class LocationMonitorService : Service() {
    private val serviceScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private lateinit var store: SessionStore
    private lateinit var api: MobileAttendanceApi
    private lateinit var fusedLocationClient: FusedLocationProviderClient
    private var loopStarted = false

    override fun onCreate() {
        super.onCreate()
        store = SessionStore(this)
        api = MobileAttendanceApi()
        fusedLocationClient = LocationServices.getFusedLocationProviderClient(this)
        ensureNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                store.monitoringEnabled = false
                stopSelf()
                return START_NOT_STICKY
            }

            ACTION_REFRESH_NOW -> {
                startForeground(NOTIFICATION_ID, buildNotification(store.lastSyncMessage))
                startLoopIfNeeded(forceImmediate = true)
                return START_STICKY
            }

            else -> {
                startForeground(NOTIFICATION_ID, buildNotification("Preparing geofence attendance monitor"))
                startLoopIfNeeded(forceImmediate = false)
                return START_STICKY
            }
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        serviceScope.cancel()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun startLoopIfNeeded(forceImmediate: Boolean) {
        if (loopStarted) {
            if (forceImmediate) {
                serviceScope.launch { runCycleSafely() }
            }
            return
        }
        loopStarted = true
        serviceScope.launch {
            if (forceImmediate) {
                runCycleSafely()
            }
            while (isActive) {
                if (!store.monitoringEnabled || store.token.isBlank()) {
                    updateNotification("Monitoring paused")
                    break
                }
                runCycleSafely()
                delay((store.heartbeatIntervalSeconds.coerceAtLeast(30L)) * 1000L)
            }
            stopSelf()
        }
    }

    private suspend fun runCycleSafely() {
        try {
            performCycle()
        } catch (t: Throwable) {
            store.lastSyncMessage = "Sync error: ${t.message ?: "unknown"}"
            updateNotification(store.lastSyncMessage)
        }
    }

    private suspend fun performCycle() {
        if (store.token.isBlank()) {
            updateNotification("Login required")
            return
        }
        if (!hasLocationPermission()) {
            updateNotification("Location permission required")
            return
        }

        val networkUp = isNetworkAvailable()
        if (networkUp && store.pendingCheckoutAtIso.isNotBlank()) {
            syncPendingCheckout()
        }

        val location = getCurrentLocationOrNull()
        if (location == null) {
            handleLocationFailure(networkUp)
            return
        }

        val lat = location.latitude
        val lng = location.longitude
        store.lastKnownLat = lat
        store.lastKnownLng = lng
        val distance = distanceMeters(lat, lng, store.cafeLat, store.cafeLng)
        store.lastDistanceM = distance.toDouble()

        if (!networkUp) {
            handleOffline(lat, lng, distance.toDouble())
            return
        }

        store.offlineSinceMs = 0L
        if (distance <= store.radiusM) {
            store.outsideSinceMs = 0L
            store.locationFailureSinceMs = 0L
            if (!store.checkedIn) {
                val session = api.checkIn(store.baseUrl, store.token, lat, lng, nowIso())
                store.applyAttendanceState(session)
                store.lastSyncMessage = "Checked in inside cafe geofence"
            } else {
                val session = api.heartbeat(store.baseUrl, store.token, lat, lng, nowIso())
                store.applyAttendanceState(session)
                store.lastSyncMessage = "Inside geofence • heartbeat synced"
            }
        } else {
            if (store.checkedIn) {
                if (store.outsideSinceMs == 0L) store.outsideSinceMs = System.currentTimeMillis()
                val elapsedMs = System.currentTimeMillis() - store.outsideSinceMs
                if (elapsedMs >= store.locationFailureGraceMinutes * 60_000L) {
                    val session = api.checkOut(
                        store.baseUrl,
                        store.token,
                        lat,
                        lng,
                        nowIso(),
                        reason = "outside_geofence",
                    )
                    store.applyAttendanceState(session)
                    store.pendingCheckoutAtIso = ""
                    store.pendingCheckoutReason = ""
                    store.outsideSinceMs = 0L
                    store.lastSyncMessage = "Checked out after leaving cafe perimeter"
                } else {
                    store.lastSyncMessage = "Outside geofence • grace running"
                }
            } else {
                store.lastSyncMessage = "Outside geofence • waiting to enter"
            }
        }
        updateNotification(store.lastSyncMessage)
    }

    private suspend fun handleOffline(lat: Double, lng: Double, distance: Double) {
        if (store.offlineSinceMs == 0L) {
            store.offlineSinceMs = System.currentTimeMillis()
        }
        val elapsedMs = System.currentTimeMillis() - store.offlineSinceMs
        if (store.checkedIn && elapsedMs >= store.offlineGraceMinutes * 60_000L && store.pendingCheckoutAtIso.isBlank()) {
            store.pendingCheckoutAtIso = nowIso()
            store.pendingCheckoutReason = "offline_timeout"
            store.lastSyncMessage = "Offline too long • checkout queued"
        } else {
            store.lastSyncMessage = "Offline • waiting before auto checkout"
        }
        store.lastKnownLat = lat
        store.lastKnownLng = lng
        store.lastDistanceM = distance
        updateNotification(store.lastSyncMessage)
    }

    private suspend fun handleLocationFailure(networkUp: Boolean) {
        if (!networkUp) {
            if (store.offlineSinceMs == 0L) store.offlineSinceMs = System.currentTimeMillis()
            val elapsedMs = System.currentTimeMillis() - store.offlineSinceMs
            if (store.checkedIn && elapsedMs >= store.offlineGraceMinutes * 60_000L && store.pendingCheckoutAtIso.isBlank()) {
                store.pendingCheckoutAtIso = nowIso()
                store.pendingCheckoutReason = "offline_timeout"
                store.lastSyncMessage = "Offline too long • checkout queued"
            } else {
                store.lastSyncMessage = "Offline • location unavailable"
            }
            updateNotification(store.lastSyncMessage)
            return
        }

        if (store.locationFailureSinceMs == 0L) {
            store.locationFailureSinceMs = System.currentTimeMillis()
        }
        val elapsedMs = System.currentTimeMillis() - store.locationFailureSinceMs
        if (store.checkedIn && elapsedMs >= store.locationFailureGraceMinutes * 60_000L) {
            val session = api.checkOut(
                store.baseUrl,
                store.token,
                lat = if (store.lastKnownLat != 0.0) store.lastKnownLat else null,
                lng = if (store.lastKnownLng != 0.0) store.lastKnownLng else null,
                capturedAtIso = nowIso(),
                reason = "location_failure",
            )
            store.applyAttendanceState(session)
            store.lastSyncMessage = "Checked out after repeated location failures"
            store.locationFailureSinceMs = 0L
            store.outsideSinceMs = 0L
        } else {
            store.lastSyncMessage = "Location unavailable • retrying"
        }
        updateNotification(store.lastSyncMessage)
    }

    private suspend fun syncPendingCheckout() {
        val session = api.checkOut(
            store.baseUrl,
            store.token,
            lat = if (store.lastKnownLat != 0.0) store.lastKnownLat else null,
            lng = if (store.lastKnownLng != 0.0) store.lastKnownLng else null,
            capturedAtIso = store.pendingCheckoutAtIso,
            reason = store.pendingCheckoutReason.ifBlank { "offline_timeout" },
        )
        store.applyAttendanceState(session)
        store.pendingCheckoutAtIso = ""
        store.pendingCheckoutReason = ""
        store.lastSyncMessage = "Queued checkout synced"
        updateNotification(store.lastSyncMessage)
    }

    private suspend fun getCurrentLocationOrNull(): Location? {
        val tokenSource = CancellationTokenSource()
        return withTimeoutOrNull(20_000L) {
            fusedLocationClient
                .getCurrentLocation(Priority.PRIORITY_HIGH_ACCURACY, tokenSource.token)
                .await()
        }
    }

    private fun hasLocationPermission(): Boolean {
        val fine = ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED
        val coarse = ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_COARSE_LOCATION) == PackageManager.PERMISSION_GRANTED
        return fine || coarse
    }

    private fun isNetworkAvailable(): Boolean {
        val cm = getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        val network = cm.activeNetwork ?: return false
        val caps = cm.getNetworkCapabilities(network) ?: return false
        return caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
    }

    private fun distanceMeters(lat1: Double, lng1: Double, lat2: Double, lng2: Double): Float {
        val result = FloatArray(1)
        Location.distanceBetween(lat1, lng1, lat2, lng2, result)
        return result[0]
    }

    private fun ensureNotificationChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val channel = NotificationChannel(
            CHANNEL_ID,
            getString(R.string.monitor_channel_name),
            NotificationManager.IMPORTANCE_LOW,
        ).apply {
            description = getString(R.string.monitor_channel_desc)
        }
        manager.createNotificationChannel(channel)
    }

    private fun buildNotification(content: String): Notification {
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("Brownberries Attendance")
            .setContentText(content)
            .setSmallIcon(android.R.drawable.ic_menu_mylocation)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .build()
    }

    private fun updateNotification(content: String) {
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        manager.notify(NOTIFICATION_ID, buildNotification(content))
    }

    private fun nowIso(): String = ZonedDateTime.now(IST_ZONE).format(DateTimeFormatter.ISO_OFFSET_DATE_TIME)

    companion object {
        private const val CHANNEL_ID = "brownberries_attendance_monitor"
        private const val NOTIFICATION_ID = 44021
        private val IST_ZONE = java.time.ZoneId.of("Asia/Kolkata")

        const val ACTION_START = "com.brownberries.attendance.START"
        const val ACTION_STOP = "com.brownberries.attendance.STOP"
        const val ACTION_REFRESH_NOW = "com.brownberries.attendance.REFRESH"

        fun start(context: Context) {
            val intent = Intent(context, LocationMonitorService::class.java).apply {
                action = ACTION_START
            }
            ContextCompat.startForegroundService(context, intent)
        }

        fun stop(context: Context) {
            val intent = Intent(context, LocationMonitorService::class.java).apply {
                action = ACTION_STOP
            }
            context.startService(intent)
        }

        fun refreshNow(context: Context) {
            val intent = Intent(context, LocationMonitorService::class.java).apply {
                action = ACTION_REFRESH_NOW
            }
            ContextCompat.startForegroundService(context, intent)
        }
    }
}
