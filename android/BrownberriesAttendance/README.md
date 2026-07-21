# Brownberries Attendance

Android-first attendance app scaffold for Brownberries Cafe.

## What this app does

- Staff logs in with their existing Brownberries staff account
- App downloads the cafe geofence and the assigned staff shift window
- A foreground location monitor checks position every minute
- When the staff member is inside the geofence, the app can auto check-in
- While checked in, the app sends heartbeats to the server
- If location repeatedly fails while internet is available, the app auto checks out after the grace period
- If internet is unavailable, the app waits longer, queues the checkout locally, and syncs it when the network returns

## Current backend endpoints used

- `POST /api/mobile/attendance/login`
- `GET /api/mobile/attendance/bootstrap`
- `POST /api/mobile/attendance/check-in`
- `POST /api/mobile/attendance/heartbeat`
- `POST /api/mobile/attendance/check-out`
- `POST /api/mobile/attendance/logout`

## Build in Android Studio

1. Open Android Studio
2. Choose `Open`
3. Select this folder:
   - `.../Brownberries Cafe Operations/android/BrownberriesAttendance`
4. Let Gradle sync
5. Run on an Android phone with Google Play Services enabled

## Server URL

Use either:

- Production domain: `https://brownberriescafe.com`
- Local network server during testing: for example `http://192.168.x.x:5050`

## Permissions to allow on the phone

- Fine location
- Background location
- Notifications

## Important notes

- This version is Android-first on purpose
- It focuses on reliable geofence attendance, not screen-time tracking
- iPhone support should be built later with a separate platform-specific behavior model
- If Android background-location policy changes, the app may need a Play-policy review before public distribution
