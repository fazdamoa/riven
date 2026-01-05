# Rate Limit & Infinite Loop Fix

## 🐛 Problem

Two issues were discovered in production:

### Issue 1: AttributeError on BucketFullException
```
ERROR: 'BucketFullException' object has no attribute 'response'
```

The code assumed all exceptions had a `.response.status_code` attribute, but rate limiting exceptions (like `BucketFullException`) don't have this.

### Issue 2: Infinite Retry Loop
```
25-11-05 15:49:44 | Starting download process for Bone Lake
25-11-05 15:49:44 | ERROR: Failed to set up uncached download
25-11-05 15:49:44 | Starting download process for Bone Lake
25-11-05 15:49:44 | ERROR: Failed to set up uncached download
25-11-05 15:49:44 | Starting download process for Bone Lake
... (repeats 3x per second, fills logs)
```

When downloads failed (especially due to rate limits), the item was immediately reprocessed with no cooldown, creating a spam loop that would fill up logs and hammer the API.

---

## ✅ Solution

### Fix 1: Robust Exception Handling

**File:** `src/program/services/downloaders/realdebrid.py`

**Before:**
```python
except Exception as e:
    if e.response.status_code == 503:  # CRASH if no .response!
        ...
```

**After:**
```python
except Exception as e:
    # Check if exception has response attribute (HTTP exception)
    if hasattr(e, 'response') and hasattr(e.response, 'status_code'):
        status_code = e.response.status_code
        # Handle HTTP errors...
    else:
        # Non-HTTP exception (rate limit, bucket full, etc.)
        error_type = type(e).__name__
        error_msg = str(e)
        logger.debug(f"Failed to add torrent: [{error_type}] {error_msg}")
        raise RealDebridError(f"{error_type}: {error_msg}")
```

**Benefits:**
- ✅ Handles both HTTP and non-HTTP exceptions
- ✅ No more crashes on BucketFullException
- ✅ Proper error logging for all exception types

---

### Fix 2: Exponential Backoff Cooldown

**File:** `src/program/services/downloaders/__init__.py`

Added intelligent cooldown system to prevent spam retries:

```python
class Downloader:
    def __init__(self):
        # Track failed attempts to prevent spam
        self._failed_items = {}  # {item_id: (timestamp, consecutive_failures)}
```

**Cooldown Logic:**
```python
# At start of run() method
if item.id in self._failed_items:
    last_attempt_time, failure_count = self._failed_items[item.id]
    cooldown_seconds = min(60 * (2 ** failure_count), 3600)
    # Exponential backoff: 60s, 120s, 240s, 480s, 960s, ... max 1 hour
    
    if time_since_last_attempt < cooldown_seconds:
        logger.debug(f"Skipping {item} - in cooldown for {remaining}s")
        return  # Skip this item for now
```

**When Failures Occur:**
```python
# When rate limit or API error detected
if "BucketFullException" in error_msg or "Rate Limit" in error_msg:
    logger.warning(f"Rate limit hit, will back off and retry later")
    # Track failure with timestamp
    if item.id in self._failed_items:
        _, failure_count = self._failed_items[item.id]
        self._failed_items[item.id] = (time(), failure_count + 1)
    else:
        self._failed_items[item.id] = (time(), 0)
```

**When Success Occurs:**
```python
if download_success:
    # Clear any previous failures
    if item.id in self._failed_items:
        del self._failed_items[item.id]
```

---

## 📊 Cooldown Schedule

| Attempt | Cooldown Time | Total Wait |
|---------|---------------|------------|
| 1st failure | 60 seconds (1 min) | 1 min |
| 2nd failure | 120 seconds (2 min) | 3 min |
| 3rd failure | 240 seconds (4 min) | 7 min |
| 4th failure | 480 seconds (8 min) | 15 min |
| 5th failure | 960 seconds (16 min) | 31 min |
| 6th+ failure | 3600 seconds (1 hour) | Max |

**Benefits:**
- ✅ Quick retry for transient issues (1 minute)
- ✅ Increasing delays for persistent problems
- ✅ Caps at 1 hour to prevent indefinite delays
- ✅ Clears on success (fresh start)

---

## 🔍 Example Scenarios

### Scenario 1: Rate Limit Hit

```
15:49:44 | Starting download for Bone Lake
15:49:44 | ERROR: BucketFullException
15:49:44 | Rate limit hit, will back off (attempt #1)
15:49:44 | Tracked failure for Bone Lake

[1 minute later]

15:50:44 | Cooldown expired for Bone Lake, attempting again
15:50:44 | Starting download for Bone Lake
15:50:44 | SUCCESS: Download started
15:50:44 | Cleared failures for Bone Lake
```

### Scenario 2: Persistent API Issues

```
15:49:44 | Starting download for Movie X
15:49:44 | ERROR: BucketFullException (attempt #1)
15:49:44 | In cooldown for 60s

15:50:44 | Cooldown expired, attempting again
15:50:44 | ERROR: BucketFullException (attempt #2)
15:50:44 | In cooldown for 120s

15:52:44 | Cooldown expired, attempting again
15:52:44 | ERROR: BucketFullException (attempt #3)
15:52:44 | In cooldown for 240s

... continues with increasing delays until resolved or max 1 hour
```

### Scenario 3: No More Spam

**Before (BAD):**
```
15:49:44.000 | Starting download for Bone Lake
15:49:44.100 | ERROR
15:49:44.200 | Starting download for Bone Lake
15:49:44.300 | ERROR
15:49:44.400 | Starting download for Bone Lake
... 3 attempts per second × 1000 seconds = 3000 log lines!
```

**After (GOOD):**
```
15:49:44 | Starting download for Bone Lake
15:49:44 | ERROR: Rate limit (attempt #1)
15:49:44 | In cooldown for 60s

15:50:44 | Cooldown expired, attempting again
15:50:44 | Starting download for Bone Lake
... only 2 log lines, then waits patiently
```

---

## 🎯 What Changed

### Files Modified
1. **`realdebrid.py`** - Robust exception handling in `add_torrent()`
2. **`__init__.py`** - Added cooldown tracking system

### Code Changes
- ✅ Added `_failed_items` dict to track failures
- ✅ Added cooldown check at start of `run()`
- ✅ Added failure tracking when rate limits hit
- ✅ Added success cleanup to reset failures
- ✅ Fixed exception handling to handle all exception types

### No Configuration Needed
- No settings to change
- No database migration
- Fully automatic

---

## 📈 Expected Behavior

### Before Fix
- ❌ Crashes on BucketFullException
- ❌ Retries 3x per second
- ❌ Fills logs with spam
- ❌ Hammers Real-Debrid API
- ❌ No backoff strategy

### After Fix
- ✅ Handles all exception types gracefully
- ✅ Waits 1 min before first retry
- ✅ Exponentially increases wait time
- ✅ Clean, readable logs
- ✅ Respects API limits
- ✅ Smart backoff strategy

---

## 🚀 Deployment

**No special steps needed!** The fixes are already in the code.

Just restart Riven:
```bash
docker-compose restart riven
# or
systemctl restart riven
```

---

## ✅ Verification

After deploying, you should see:

**For rate limit errors:**
```log
Rate limit or API limit hit for {item}, will back off and retry later
Skipping {item} - in cooldown for {X}s more (attempt #{N})
Cooldown expired for {item}, attempting download again
```

**No more:**
```log
❌ ERROR: 'BucketFullException' object has no attribute 'response'
❌ Same item processing 3x per second
❌ Logs filling up with repeated errors
```

---

## 🐛 Troubleshooting

### "Item stuck in cooldown forever"

**Check:** Maximum cooldown is 1 hour
**Expected:** After 1 hour, will retry automatically
**Action:** If truly stuck, restart Riven (clears in-memory cooldown cache)

### "Still seeing spam retries"

**Check:** Verify code changes were applied
**Check:** Verify Riven was restarted
**Action:** Re-apply changes and restart

### "Cooldown too aggressive/not aggressive enough"

**Adjust:** Edit the cooldown formula in `__init__.py`:
```python
# Current (1min, 2min, 4min, 8min, 16min, max 1h):
cooldown_seconds = min(60 * (2 ** failure_count), 3600)

# More aggressive (30s, 1min, 2min, 4min, 8min, max 30min):
cooldown_seconds = min(30 * (2 ** failure_count), 1800)

# Less aggressive (2min, 4min, 8min, 16min, 32min, max 2h):
cooldown_seconds = min(120 * (2 ** failure_count), 7200)
```

---

## 📊 Impact

| Metric | Before | After |
|--------|--------|-------|
| **Crash on BucketFull** | Yes ❌ | No ✅ |
| **Retry attempts/second** | 3x/sec ❌ | 1x/min ✅ |
| **Log spam** | Severe ❌ | Minimal ✅ |
| **API hammering** | Yes ❌ | No ✅ |
| **Backoff strategy** | None ❌ | Exponential ✅ |

---

## 🎓 Summary

**Problem:** Items with rate limit errors crashed and retried endlessly

**Solution:** 
1. Fixed exception handling to handle all exception types
2. Added exponential backoff cooldown system

**Result:**
- ✅ No more crashes
- ✅ No more spam loops
- ✅ Intelligent retry strategy
- ✅ Clean logs
- ✅ Respects API limits

**Your Riven now handles rate limits gracefully! 🎉**
