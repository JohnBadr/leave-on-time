# LeaveOnTime 🚌

A proactive, personalized transit notification system for PassioGo bus networks. Set your schedule once — get notified exactly when to leave.

## What It Does

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
- Delay prediction model trained on proprietary Bull Runner position data from Fall 2026+.
- Separate ML models per academic period (regular, finals, first week, summer, break, holiday)

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

A standalone shadow-testing process continuously validates ETA prediction accuracy against real bus arrivals across multiple PassioGo systems, fully isolated from the production Bull Runner tracker and its training data.

## Tech Stack

- **Backend:** Python, Flask, APScheduler
- **Database:** SQLite (dev), PostgreSQL (prod)
- **Road network:** OSRM on OpenStreetMap data (free, no API key, hosted by myself on a google server for precomputing logic)
- **Transit data:** PassioGo API (`passiogo-fix` fork)
- **Notifications:** Web app (default), SendGrid (email)
- **ML:** scikit-learn Random Forest, one classifier per academic period type
- **Hardware:** ESP32 DOIT DevKit V1, OLED display, buzzer
- **Hosting:** Railway

## Project Status

🚧 **In active development — Summer 2026**

- [x] Route graph precomputation via OSRM (road-accurate stop-pair distances + shape points)
- [x] Real-time vehicle tracking engine (shape projection, cold start resolution, index advancement)
- [x] Unscheduled break detector (filters stop time from speed buffer)
- [x] High-frequency data logger — 5s polling, DETERMINED-only, running 24/7 on Raspberry Pi
- [x] Shadow testing framework — isolated multi-system validation of ETA predictions against real arrivals
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

A logging script polls the PassioGo API every 5 seconds and stores vehicle positions, current stop pair index, elapsed time at position, and academic calendar context to a local SQLite database. Only vehicles in DETERMINED tracking state are logged — positions during cold start resolution are excluded to keep training data clean. This data will be used to train the break prediction model after sufficient coverage across different academic periods.

## Why Not Just Use PassioGo?

PassioGo shows you where the bus is right now — you still have to check it yourself every morning and do the math. LeaveOnTime tells you when to leave your door. The distinction matters when you factor in:

- Walking time from your specific location to your nearest stop
- Real road distances between stops (not straight-line estimates)
- Multiple buses on the same route — picking the latest one that still gets you there on time
- Historical unscheduled break patterns so ETAs don't get thrown off by driver breaks
- Automatic override if your bus is delayed after you've already been notified

---

*Built by a USF Computer Engineering student. Supports any PassioGo transit system — ML delay prediction currently targets Bull Runner at USF (system #2343).*