# Brownberries Staff

Android-first staff workspace for Brownberries Cafe.

## What this app does

- Staff logs in with their existing Brownberries staff account
- Staff can manage their profile, attendance history, leave requests, and documents
- Staff can place table orders and update menu-item availability from the native Work and Orders tabs
- Before sign-in, the home screen shows only a clean email/password form
- After sign-in, the app uses four Android-native destinations: Home, Orders, Work, and Me
- Attendance remains available from the Home shift card and Me > Staff Management; it is not a separate bottom tab
- Logout is kept under Me > Security & Login instead of competing with daily work destinations
- App downloads the cafe geofence and the assigned staff shift window
- A foreground location monitor checks position every minute and registers a Google Play Services geofence for enter/exit wake-ups
- When the staff member is inside the geofence, the app can auto check-in
- While checked in, the app sends heartbeats to the server
- If location repeatedly fails while internet is available, the app auto checks out after the grace period
- If internet is unavailable, the app waits longer, queues the checkout locally, and syncs it when the network returns
- Monitoring starts automatically after login, at every app launch, after device restart, and after an app update
- The only manual attendance action is **Check-Out**; automatic checkout remains controlled by the server policy and monitor
- The staff workspace is a native Android experience backed by the existing production APIs. It does not embed the web UI or duplicate the public/customer navigation.
- Home is the daily command centre, Orders is the fast table-ordering flow, Work groups item availability by workstation, and Me contains personal information, attendance, leave, documents, rule book and security.
- Navigation is capability-aware: when the server denies an operation, its destination is hidden instead of showing an unusable locked screen.

## Current backend endpoints used

- `POST /api/mobile/attendance/login`
- `GET /api/mobile/attendance/bootstrap`
- `POST /api/mobile/attendance/check-in`
- `POST /api/mobile/attendance/heartbeat`
- `POST /api/mobile/attendance/check-out`
- `POST /api/mobile/attendance/logout`

### Staff workspace endpoints

- `GET /api/mobile/staff/workspace`
- `POST /api/mobile/staff/profile`
- `POST /api/mobile/staff/leaves`
- `POST /api/mobile/staff/leaves/<id>/cancel`
- `POST /api/mobile/staff/documents`
- `GET /api/mobile/staff/documents/<id>/download`
- `POST /api/mobile/staff/items/<id>/availability`
- `POST /api/mobile/staff/orders`

The native app uses the bearer token returned by the attendance login for every
workspace request. The Windows server must be running the matching source
revision containing `app/mobile_staff.py`; after deployment,
`GET /api/mobile/staff/workspace` should return `401` when called without a
token (not `404`).

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

On Samsung devices, set the app battery mode to **Unrestricted** and allow
background activity. Also allow **Location > Allow all the time** and enable
notifications. Android may stop a location service after a user uses **Force
stop**; no app can restart itself after that system action. Closing the app
normally leaves the foreground monitor running. The monitor is a foreground
service, restarts after normal process termination, and is started again after
device boot or app replacement when the staff session is still active.

## Admin attendance setup

In the web app, open **Cafe > Staff Attendance QR & Geofence**. Set:

- **Cafe Latitude / Longitude**: the cafe center coordinates
- **Allowed Radius**: the circular territory in meters
- **Presence Leniency**: minutes allowed around the assigned shift start/end when calculating the automatic presence status
- **Outside-Geofence Checkout Grace**: how long the phone must remain outside before automatic checkout
- **Offline Checkout Grace**: how long an already checked-in staff member can remain without network before checkout is queued
- **Location-Failure Grace**: how long repeated location failures are tolerated while internet is available

The same page shows a map with the cafe marker and the live circular geofence. The
server calculates distance using direct haversine distance between the cafe and
the phone coordinates. Admins and managers can use **Staff > Attendance Entry**
to correct a record, check out an active session, or override the automatic
status. An override remains authoritative until an administrator changes it.

## What staff should expect

After the first successful sign-in and permission approval, the app starts
monitoring without a Start button. Entering the circle creates the day's
attendance record. Leaving the circle closes it after the configured grace
period. The Profile attendance view and the admin calendar show check-in time,
check-out time, actual hours and minutes, GPS distance, source, and any automatic
checkout reason. If the phone loses connectivity, the app keeps the session
locally and synchronizes the checkout when the network returns, subject to the
offline grace policy.

## Important notes

- This version is Android-first on purpose
- It does not collect screen-time data
- iPhone support should be built later with a separate platform-specific behavior model
- If Android background-location policy changes, the app may need a Play-policy review before public distribution
