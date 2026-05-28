# Title TBD (BullsOnTime for now) 🚌

A proactive, personalized transit notification system for PassioGo bus networks. Set your schedule once — get notified exactly when to leave.

## What It Does

Most transit apps require you to actively check them. BullsOnTime works in the background. You tell it "I need to be at ENB by 9am every Monday, Wednesday, and Friday" and it handles the rest — figuring out which bus to take, when to leave your door, and notifying you at exactly the right moment.

**Core features:**
- Recurring weekly schedules per destination
- Real-time departure calculation factoring in walk time, bus ETA, and ride duration
- Proactive notifications via email or Telegram
- Automatic delay detection — if your bus is running late, the notification adjusts
- Confidence score on every notification based on historical holding patterns
- Works with any PassioGo transit system (50+ universities and transit agencies)

**USF-specific:**
- Delay prediction model trained on proprietary Bull Runner position data collected since May 2026
- Learns holding patterns at specific stops (Library, MSC) by time of day, day of week, and academic period

## Architecture

```
PassioGo API + OSRM (road network)
        ↓
Python/Flask backend (Railway)
        ↓                    ↓
   Web app              Data logger
(accounts, schedules,   (Raspberry Pi, 24/7)
 notifications)              ↓
        ↓              SQLite → PostgreSQL
Email / Telegram             ↓
notifications /         ML delay prediction
Possibly SMS          (scikit-learn, retrained weekly)
        ↓             
      ESP32
  (personal desk device)
```

## Tech Stack

- **Backend:** Python, Flask, APScheduler
- **Database:** SQLite (dev), PostgreSQL (prod)
- **Road network:** OSRM on OpenStreetMap data (free, no API key)
- **Transit data:** PassioGo API
- **Notifications:** SendGrid (email), Telegram Bot API
- **ML:** scikit-learn Random Forest classifier (might change)
- **Hardware:** ESP32 DOIT DevKit V1, OLED display, buzzer
- **Hosting:** Railway

## Project Status

🚧 **In active development — Summer 2026**

- [x] Real-time vehicle logger. Going to start running it 24/7 on a raspberry Pi Zero 2 W.
- [x] Route graph precomputation via OSRM (road-accurate stop pair distances)
- [ ] ETA engine
- [ ] Departure calculation
- [ ] User accounts + recurring schedules
- [ ] Notification scheduler
- [ ] Web interface
- [ ] Deployment
- [ ] ESP32 firmware
- [ ] ML delay prediction model (targeting Fall 2026 data)

## Data Collection

A logging script polls the PassioGo API every 60 seconds and stores vehicle positions, routes, headings, and academic calendar context to a local database. This data will be used to train the delay prediction model after sufficient coverage across different academic periods (regular semester, finals, first week, summer).

## Why Not Just Use PassioGo?

PassioGo shows you where the bus is with an estimated time of arrival so you have to keep checking it everyday before you want to leave. Also from my experience, the time it says it's going to take is somewhat inaccurate. BullsOnTime tells you when to leave your house. The distinction matters when you factor in:

- Walking time from your specific address to your nearest stop
- Real road distances between stops (not straight-line estimates)
- Historical holding patterns at specific stops
- Multiple buses on the same route — picking the latest one that still gets you there on time

---

*Built by a USF Computer Engineering student. Currently targeting Bull Runner at USF (system #2343) with plans to expand to other PassioGo systems.*