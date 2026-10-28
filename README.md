# LeaveOnTime 🚌

A proactive, personalized transit notification system for PassioGo bus networks. Set your schedule once — get notified exactly when to leave.

## What It Does

The tracking and ETA implementation now includes per-route versions, timestamp-aware
tracking, a durable recorder, historical/live travel-time estimates, and offline
replay. Start with [ACCURACY.md](ACCURACY.md) for setup, verification, and the
Raspberry Pi service example. Let any already-running old route updater finish
before initializing the version columns. No USF route-source replacement is required.

Most transit apps require you to actively check them. LeaveOnTime works in the background. You tell it "I need to be at ENB by 9am every Monday, Wednesday, and Friday" and it handles the rest — figuring out which bus to take, when to leave your door, and notifying you at exactly the right moment.

**Core features:**
- Recurring weekly schedules per destination
- Real-time departure calculation factoring in walk time, live bus position, and ride duration
- Proactive notifications via the web app or email
- Picks the latest bus that still gets you there on time — not just the next one
- Automatic override notifications if your bus is running late
- Works with any PassioGo transit system 
- Real-time bus tracking engine with shape-based segment projection

**USF-specific:**
- Historical segment and stop-time estimates combined with recent fleet observations.
- Academic-period features (regular, finals, first week, summer, break, holiday), evaluated at USF local event time.

## Architecture

```
PassioGo API + OSRM (road network)
        ↓
Python/Flask backend (Railway)
        ↓                         ↓
   Web app                   Data logger
(accounts, schedules,        (Raspberry Pi Zero 2W, 24/7)
 notifications)                    ↓
        ↓                    bullrunner.db
Web / Email                        ↓
notifications               ML break prediction
        ↓                   (scikit-learn, retrained periodically)
      ESP32
  (personal desk device)
```

A standalone shadow recorder uses an isolated database and can observe multiple
PassioGo systems through repeated `--system-id` arguments. Its automatic arrival
labels are provisional; manually checked arrivals and offline replay provide the
audited accuracy scorecard. Legacy shadow-test tables are preserved.

## Tech Stack

- **Backend:** Python recorder and shared ETA engine; web integration remains in progress
- **Database:** SQLite (dev), PostgreSQL (prod)
- **Road network:** OSRM on OpenStreetMap data (free, no API key, hosted by myself on a google server for precomputing logic)
- **Transit data:** PassioGo API (`passiogo-fix` fork)
- **Notifications:** Web app (default), SendGrid (email)
- **Prediction:** Historical segment/stop baselines with live corrections; trained ML remains a future comparison
- **Hardware:** ESP32 DOIT DevKit V1, OLED display, buzzer
- **Hosting:** Railway

## Project Status

🚧 **In active development — Summer 2026**

- [x] Route graph precomputation via OSRM (road-accurate stop-pair distances + shape points)
- [x] Real-time vehicle tracking engine (shape projection, cold start resolution, index advancement)
- [x] Unscheduled break detector (filters stop time from speed buffer)
- [x] High-frequency recorder — 5s polling, raw reports in every tracking state, immediate durable samples
- [ ] Install the recorder as a 24/7 service on the Raspberry Pi/server
- [x] Shadow recorder and deterministic replay — isolated multi-system cohorts, explicit failures, independent provisional labels, and manual auditing
- [x] Per-vehicle ETA calculation (segment-ratio-corrected against OSRM baselines)
- [ ] Multi-bus selection algorithm (project-forward, pick latest bus still on time)
- [ ] Optimal stop selection
- [ ] Departure calculation
- [~] User accounts + recurring schedules — in progress
- [ ] Notification scheduler
- [ ] Web interface (PWA)
- [ ] Deployment
- [ ] ESP32 firmware
- [ ] ML break prediction model (targeting Fall 2026 data)

## Data Collection

A logging script polls PassioGo every 5 seconds and saves original reports before
tracking, including uncertain, repeated, and out-of-service reports. Completed
movement and stop measurements use explicit event boundaries and route versions.
The estimator learns from up to eight weeks of version-matched history and recent
fleet observations. Old measurement tables remain intact and are excluded from
new training because their route layout and label quality cannot be established.
See [ACCURACY.md](ACCURACY.md) for recording, replay, and validation commands.

## Why Not Just Use PassioGo?

PassioGo shows you where the bus is right now — you still have to check it yourself every morning and do the math. LeaveOnTime tells you when to leave your door. The distinction matters when you factor in:

- Walking time from your specific location to your nearest stop
- Real road distances between stops (not straight-line estimates)
- Multiple buses on the same route — picking the latest one that still gets you there on time
- Historical unscheduled break patterns so ETAs don't get thrown off by driver breaks
- Automatic override if your bus is delayed after you've already been notified

---

*Built by a USF Computer Engineering student. Supports any PassioGo transit system — ML delay prediction currently targets Bull Runner at USF (system #2343).*
