# Code Review: Automated Action Timing Issue

**Date:** 2026-07-23  
**Issue:** Control panel posts at 09:23 AM but nominalization happens at 13:00 PM (4-hour gap)  
**Goal:** Run nominalization ~1 minute after control panel is posted

---

## 🔴 Current Issue

### Problem Statement
- **09:23 AM RO Time:** Control panel posts to Slack (currently working ✓)
- **13:00 PM RO Time:** Nominalization should happen, but timing is wrong
- **Expected:** Nominalization should run ~1 minute after panel post (09:24 AM)
- **Gap:** 3.5+ hours between events

### Root Cause Analysis

**Current Code Flow (lines 1867-1870):**
```python
def run_scheduler(client):
    schedule.every().tuesday.at("07:00").do(lambda: automated_nomination(client))
    schedule.every().thursday.at("07:00").do(lambda: automated_nomination(client))
    logger.info("Scheduler started: Tue/Thu at 07:00 UTC (10:00 AM Romanian time)")
```

**What happens in `automated_nomination()` (lines 673-741):**
1. Checks if holiday ✓
2. Syncs members ✓
3. Selects random leader ✓
4. Creates nomination + posts to Slack (lines 741) ✓

**Issue:** Both posting and nominalization happen at the SAME time in one function call. There's no delay mechanism.

### Secondary Issue
**Syntax Error on line 1870:**
```python
   logger.info(...)  # Wrong indentation - should be 4 spaces, not 3
```

---

## 🎯 Proposed Solutions

### **Option 1: Simple 1-Minute Delay (Easiest)**
Add `time.sleep(60)` in `automated_nomination()` after posting panel, before nominalization.

**Pros:** Minimal code change  
**Cons:** Blocks scheduler thread for 1 minute; synchronous approach  
**Risk:** High - thread sleeping could cause missed other events

```python
def automated_nomination(client):
    # ... sync & selection logic ...
    
    # Post control panel
    refresh_control_panel(client, MEETING_CHANNEL_ID, data)
    
    # Wait 1 minute before nominalization
    time.sleep(60)
    
    # Then create nomination
    create_nomination(client, MEETING_CHANNEL_ID, selected_id, data, is_auto=True)
```

**❌ NOT RECOMMENDED** - Blocks the entire scheduler thread

---

### **Option 2: Two Separate Scheduled Jobs (Recommended)**
Create two distinct scheduler jobs:
- Job 1 (09:23 AM): Posts control panel
- Job 2 (09:24 AM): Runs nominalization

**Pros:**
- Non-blocking ✓
- Decouples concerns ✓
- Flexible timing ✓
- Easy to adjust delays ✓

**Cons:** Slightly more code

```python
def post_control_panel_scheduled(client):
    """Post control panel without nomination."""
    if not MEETING_CHANNEL_ID:
        logger.error("MEETING_CHANNEL_ID not set")
        return
    
    data = load_data()
    today = datetime.now()
    day_name = today.strftime("%A")
    
    if day_name not in ["Tuesday", "Thursday"]:
        return
    
    # Check holiday
    if is_holiday(today):
        logger.info("Public holiday detected - skipping")
        return
    
    logger.info(f"Posting control panel: {today.strftime('%Y-%m-%d')}")
    refresh_control_panel(client, MEETING_CHANNEL_ID, data)


def run_nominalization_scheduled(client):
    """Run nominalization independently."""
    if not MEETING_CHANNEL_ID:
        return
    
    data = load_data()
    today = datetime.now()
    day_name = today.strftime("%A")
    
    if day_name not in ["Tuesday", "Thursday"]:
        return
    
    logger.info(f"Running nominalization: {today.strftime('%Y-%m-%d')}")
    
    sync_channel_members(client, MEETING_CHANNEL_ID, data, verbose=False)
    selected_id = select_random_leader(data, today, client, check_status=True)
    
    if not selected_id:
        logger.warning("No eligible leaders available")
        try:
            client.chat_postMessage(
                channel=MEETING_CHANNEL_ID,
                text="🚫 *No available leaders today* - All members already led this week."
            )
        except Exception as e:
            logger.error(f"Error posting cancellation: {e}")
        return
    
    # Proceed with nomination
    member = data["members"][selected_id]
    is_available, status_message = check_user_status(client, selected_id)
    
    if not is_available:
        logger.warning(f"Selected person unavailable: {status_message}")
        try:
            client.chat_postMessage(
                channel=MEETING_CHANNEL_ID,
                text=f"⚠️ Nominated person is unavailable: {status_message}"
            )
        except Exception as e:
            logger.error(f"Error: {e}")
        return
    
    create_nomination(client, MEETING_CHANNEL_ID, selected_id, data, is_auto=True)


def run_scheduler(client):
    # Post panel at 09:23 (07:23 UTC)
    schedule.every().tuesday.at("07:23").do(lambda: post_control_panel_scheduled(client))
    schedule.every().thursday.at("07:23").do(lambda: post_control_panel_scheduled(client))
    
    # Run nominalization at 09:24 (07:24 UTC) - 1 minute after panel
    schedule.every().tuesday.at("07:24").do(lambda: run_nominalization_scheduled(client))
    schedule.every().thursday.at("07:24").do(lambda: run_nominalization_scheduled(client))
    
    logger.info("Scheduler started: Tue/Thu at 07:23 UTC (panel) and 07:24 UTC (nomination)")
    
    while True:
        try:
            schedule.run_pending()
            time.sleep(60)
        except Exception as e:
            logger.error(f"Scheduler error: {e}")
            time.sleep(60)
```

**✅ RECOMMENDED** - Clean, non-blocking, easy to maintain

---

### **Option 3: Event-Driven with Message Callback (Most Flexible)**
Use message posted event + delay to trigger nominalization.

```python
# Store pending nominations waiting for panel post
panel_posted_queue = {}

def post_control_panel_scheduled(client):
    """Post panel and queue for delayed nominalization."""
    if not MEETING_CHANNEL_ID:
        return
    
    data = load_data()
    today = datetime.now()
    
    if is_holiday(today):
        return
    
    # Post panel
    ts = post_control_panel(client, MEETING_CHANNEL_ID)
    
    if ts:
        # Queue nominalization for 1 minute later
        panel_posted_queue[today.strftime("%Y-%m-%d")] = {
            "posted_at": time.time(),
            "channel_id": MEETING_CHANNEL_ID
        }
        logger.info(f"Panel posted, queued nominalization")


def check_pending_nominations(client):
    """Check queue and run nominalization if 1+ minute has passed."""
    now = time.time()
    to_remove = []
    
    for date_key, info in panel_posted_queue.items():
        elapsed = now - info["posted_at"]
        
        # 60+ seconds passed
        if elapsed >= 60:
            logger.info(f"Running queued nominalization for {date_key}")
            run_nominalization_scheduled(client)
            to_remove.append(date_key)
    
    for key in to_remove:
        del panel_posted_queue[key]


def run_scheduler(client):
    schedule.every().tuesday.at("07:23").do(lambda: post_control_panel_scheduled(client))
    schedule.every().thursday.at("07:23").do(lambda: post_control_panel_scheduled(client))
    
    # Check queue every 30 seconds
    schedule.every(30).seconds.do(lambda: check_pending_nominations(client))
    
    logger.info("Scheduler started with event-driven nomination queue")
    
    while True:
        try:
            schedule.run_pending()
            time.sleep(60)
        except Exception as e:
            logger.error(f"Scheduler error: {e}")
            time.sleep(60)
```

**Pros:** Most flexible, event-driven approach  
**Cons:** More complex state management  
**Use Case:** If you need to monitor the panel post for success before nominalization

---

## 📋 Comparison Table

| Solution | Delay Type | Blocking? | Complexity | Reliability | Recommended |
|----------|-----------|-----------|-----------|-------------|-------------|
| **Option 1** | Synchronous sleep | ❌ Yes | Low | ⚠️ Medium | ❌ No |
| **Option 2** | Two jobs | ✅ No | Medium | ✅ High | ✅ **Yes** |
| **Option 3** | Queue-based | ✅ No | High | ✅ High | ℹ️ For complex flows |

---

## 🐛 Additional Bugs Found

1. **Line 1870 - Indentation Error**
   ```python
      logger.info(...)  # Wrong indentation
   ```
   Should be:
   ```python
    logger.info(...)  # 4 spaces
   ```

2. **No error handling in scheduler**
   If nominalization fails silently, there's no retry mechanism.

---

## 🎬 Implementation Steps (Option 2 Recommended)

1. **Refactor `automated_nomination()`** into two functions:
   - `post_control_panel_scheduled()` - posts panel only
   - `run_nominalization_scheduled()` - runs selection + nomination

2. **Update `run_scheduler()`** to schedule both at correct times:
   - Panel: 07:23 UTC (09:23 AM RO)
   - Nomination: 07:24 UTC (09:24 AM RO)

3. **Fix indentation error** on line 1870

4. **Test** on a Tuesday/Thursday to verify timing

---

## ⏰ Time Zone Verification

Your setup: **RO Time = UTC +3**
- 09:23 AM RO = 06:23 UTC (not 07:23!)
- 09:24 AM RO = 06:24 UTC (not 07:24!)

**Current scheduler shows 07:00 UTC = 10:00 AM RO**

**You said it runs at 09:23 AM** - This suggests your actual cron job elsewhere is different from the Python scheduler. Verify if you're using external scheduler or bot's internal scheduler.

---

## ✅ Recommended Fix

Implement **Option 2** with corrected times:

```python
def run_scheduler(client):
    # Adjust times based on YOUR actual cron job time
    # If panel posts at 09:23 AM RO:
    schedule.every().tuesday.at("06:23").do(lambda: post_control_panel_scheduled(client))
    schedule.every().thursday.at("06:23").do(lambda: post_control_panel_scheduled(client))
    
    # Nominalization 1 minute later
    schedule.every().tuesday.at("06:24").do(lambda: run_nominalization_scheduled(client))
    schedule.every().thursday.at("06:24").do(lambda: run_nominalization_scheduled(client))
    
    logger.info("Scheduler: Panel @ 09:23 AM RO, Nominalization @ 09:24 AM RO")
    
    while True:
        try:
            schedule.run_pending()
            time.sleep(60)
        except Exception as e:
            logger.error(f"Scheduler error: {e}")
            time.sleep(60)
```

