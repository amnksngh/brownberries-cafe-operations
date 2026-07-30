package com.brownberries.attendance

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import com.google.android.gms.location.GeofencingEvent

class GeofenceReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent?) {
        val event = intent?.let { GeofencingEvent.fromIntent(it) } ?: return
        if (event.hasError()) return
        val store = SessionStore(context)
        if (store.token.isBlank()) return
        store.monitoringEnabled = true
        LocationMonitorService.refreshNow(context)
    }
}
