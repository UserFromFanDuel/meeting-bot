# Deployment Instructions

## What to Do

Replace your current `meeting_leader_bot.py` with `meeting_leader_bot_FIXED.py`

### Step 1: Backup (optional but recommended)
```bash
cp meeting_leader_bot.py meeting_leader_bot.py.backup
```

### Step 2: Deploy the Fixed Version
```bash
cp meeting_leader_bot_FIXED.py meeting_leader_bot.py
```

### Step 3: Push to GitHub
```bash
git add meeting_leader_bot.py
git commit -m "Fix: Prevent duplicate nominations by tracking per-day"
git push
```

## No Other Changes Needed
- ✅ `run-bot.yml` stays the same
- ✅ `requirements.txt` stays the same
- ✅ Environment variables stay the same
- ✅ All existing functionality preserved

## What's Fixed

| Issue | Before | After |
|-------|--------|-------|
| Bot runs twice, nominates twice | ❌ Happens every time | ✅ Only nominates once per day |
| Catch-up logic runs multiple times | ❌ No protection | ✅ Checks `last_nomination_date` |
| Control panel posts on every startup | ❌ Posts Mon-Fri | ✅ Only on Wednesday |

## Testing on Next Wednesday

Watch your logs for:
```
✅ [PANEL POST] Posting control panel: YYYY-MM-DD 05:00:00 Wednesday
✅ [NOMINATION] Starting nomination process: YYYY-MM-DD 05:01:00 Wednesday
✅ Nomination created for [Person Name]
```

If bot restarts after 05:01, you should see:
```
✅ [NOMINATION] Already nominated today (YYYY-MM-DD) - skipping to prevent duplicates
```

## Verify in Slack
- ✅ Only **ONE** nomination message in channel (not two)
- ✅ Control panel posts once
- ✅ All functionality works normally
