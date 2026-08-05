# Meeting Leader Bot - Fix Summary

## Problem
Your bot was running **twice** and nominating **twice** on the same day. Today it nominated two people when it should have only nominated one.

## Root Cause
Three issues combined to cause duplicate nominations:

1. **Startup control panel posting** - Every time the bot starts (which happens every weekday at 04:00 UTC per your GitHub Actions schedule), it posts the control panel. This happens before the scheduler even runs.

2. **No per-day tracking** - There was no check to prevent the nomination function from running multiple times in the same day. If the bot restarted after 05:01 UTC on Wednesday, the catch-up logic would run the nomination again.

3. **Catch-up logic too aggressive** - The catch-up code ran nominations whenever the bot started late on Wednesday, without checking if one had already happened.

## Solution
The fixed version (`meeting_leader_bot_FIXED.py`) implements three key changes:

### 1. **Added `last_nomination_date` tracking** (Line 68, 1965-1967)
```python
"last_nomination_date": None  # NEW in data structure
```

The bot now tracks the date when it last nominated someone. Before creating a new nomination, it checks:
```python
if data.get("last_nomination_date") == today_date:
    logger.info(f"Already nominated today - skipping to prevent duplicates")
    return
```

### 2. **Only post control panel on Wednesday startups** (Lines 2098-2103)
Changed startup behavior from posting every day to only on Wednesday:
```python
if day_name == "Wednesday":
    logger.info("Posting control panel to channel (Wednesday startup)...")
    # post control panel
else:
    logger.info(f"Skipping control panel post on {day_name} startup")
```

### 3. **Simplified catch-up logic** (Lines 2030-2039)
The catch-up logic now respects the `last_nomination_date` check:
```python
if hour_minute >= "05:00":
    post_control_panel_scheduled(client)  # Always post panel if late
    if hour_minute >= "05:01":
        run_nominalization_scheduled(client)  # This will skip if already nominated
```

## What Gets Tracked in Data File
Your `meeting_data.json` now includes:
```json
{
  "members": {...},
  "history": [...],
  "pending_nominations": {...},
  "observers": [...],
  "control_panel_ts": "...",
  "last_nomination_date": "2026-08-05"  ← NEW
}
```

## Behavior After Fix

| Scenario | Before | After |
|----------|--------|-------|
| Bot runs Monday-Friday at 04:00 UTC | Posts control panel every day | Only posts on Wednesday |
| Bot restarts after 05:01 UTC Wednesday | Nominates again (duplicate!) | Checks date, skips nomination |
| Multiple restarts same Wednesday | Multiple nominations | Only one nomination per day |
| Next day (Thursday) | Would still use old date check | Fresh start, ready for next week |

## Testing the Fix
1. Replace your `meeting_leader_bot.py` with `meeting_leader_bot_FIXED.py`
2. Next Wednesday, watch the logs for:
   - `[PANEL POST]` message at 05:00 UTC ✅
   - `[NOMINATION]` message at 05:01 UTC ✅
   - If bot restarts after 05:01, it logs: `Already nominated today - skipping` ✅

3. Verify in Slack: Only **one** nomination message appears, not two.

## Logs to Watch For
**Good logs:**
```
[PANEL POST] Posting control panel: 2026-08-05 05:00:00 Wednesday
[NOMINATION] Starting nomination process: 2026-08-05 05:01:00 Wednesday
✅ Nomination created for John Doe
```

**If restart happens after nomination:**
```
[NOMINATION] Already nominated today (2026-08-05) - skipping to prevent duplicates
```

## No Breaking Changes
- All existing features work the same
- Member data format unchanged (just added one optional field)
- Observer management unchanged
- Manual selection (`/meeting-leader select`) unchanged
- Decline/re-nomination logic unchanged
