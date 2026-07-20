"""
Slack Meeting Leader Bot
========================
Automates the selection of meeting leaders for recurring team sync meetings.

Features:
- Automated random leader selection (Tuesday/Thursday at 10:00 AM)
- Tracks members by name, email, and user_id for robust identity management
- Interactive accept/decline buttons for nominations
- Observer mode (exclude specific members from selection)
- Holiday detection and vacation status checking
- Comprehensive statistics and history tracking
- Batch add/remove observers by email or name
"""

import os
import json
import random
import schedule
import time
import threading
from datetime import datetime
import requests
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import logging

# ============================================================================
# LOGGING CONFIGURATION
# ============================================================================

# Configure logging to reduce noise
logging.basicConfig(
    level=logging.WARNING,  # Only show warnings and errors
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Create a custom logger for important events only
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ============================================================================
# INITIALIZATION
# ============================================================================

# Initialize Slack app with bot token from environment
app = App(token=os.environ.get("SLACK_BOT_TOKEN"))

# Data file path for persistent storage
DATA_FILE = "meeting_data.json"

# Channel ID where meetings are held (set via environment variable)
MEETING_CHANNEL_ID = os.environ.get("MEETING_CHANNEL_ID")

# ============================================================================
# DATA MANAGEMENT
# ============================================================================

def load_data():
    """Load meeting data from JSON file."""
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r') as f:
            return json.load(f)
    
    return {
        "members": {},
        "history": [],
        "pending_nominations": {},
        "observers": []
    }

def save_data(data):
    """Persist data structure to JSON file."""
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, indent=2)

# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def get_week_number(date):
    """Get ISO week number for a given date."""
    return date.strftime("%Y-W%U")

def is_holiday(date, country_code="US"):
    """Check if a date is a public holiday using Nager.Date API."""
    try:
        year = date.year
        api_url = f"https://date.nager.at/api/v3/PublicHolidays/{year}/{country_code}"
        holidays = requests.get(api_url, timeout=5).json()
        return any(h['date'] == date.strftime("%Y-%m-%d") for h in holidays)
    except Exception:
        return False

def check_user_status(client, user_id):
    """Check if user is available based on Slack status and presence."""
    try:
        user_info = client.users_info(user=user_id)
        user = user_info["user"]
        
        if user.get("deleted", False):
            return False, "User account is deactivated"
        
        profile = user.get("profile", {})
        status_text = profile.get("status_text", "").lower()
        status_emoji = profile.get("status_emoji", "").lower()
        
        vacation_keywords = ["vacation", "ooo", "out of office", "away", "off", "pto", "holiday"]
        
        if any(keyword in status_text for keyword in vacation_keywords):
            return False, f"Status: {profile.get('status_text', 'Away')}"
        
        vacation_emojis = [":palm_tree:", ":airplane:", ":beach:", ":sunny:", ":camping:", ":mountain:"]
        if any(emoji in status_emoji for emoji in vacation_emojis):
            return False, f"Status: {profile.get('status_text', 'On vacation')}"
        
        return True, "Available"
    
    except Exception:
        return True, "Unknown"
# ============================================================================
# MEMBER SYNCHRONIZATION
# ============================================================================

def sync_channel_members(client, channel_id, data, verbose=False):
    """
    Synchronize member list from Slack channel.
    
    Args:
        client: Slack API client
        channel_id: Slack channel ID to sync from
        data: Current data structure (modified in-place)
        verbose: If True, print detailed logs. If False, only log critical info.
        
    Returns:
        tuple: (new_member_count, removed_member_count)
    """
    if verbose:
        logger.info(f"SYNC STARTED - Channel: {channel_id}")
    
    try:
        # Fetch Channel Members
        result = client.conversations_members(channel=channel_id)
        channel_member_ids = set(result["members"])
        
        # Exclude Bot
        bot_info = client.auth_test()
        bot_user_id = bot_info["user_id"]
        channel_member_ids.discard(bot_user_id)
        
        # Build Current Channel Data
        channel_user_data = {}
        
        for user_id in channel_member_ids:
            try:
                user_info = client.users_info(user=user_id)
                
                if not user_info.get("ok"):
                    continue
                    
                user_data = user_info.get("user")
                if not user_data or user_data.get("is_bot", False):
                    continue
        
                profile = user_data.get("profile", {})
                
                # Primary email field
                email = profile.get("email", "").lower().strip()
                if not email:
                    email = user_data.get("email", "").lower().strip()
        
                full_name = user_data.get("real_name") or user_data.get("name") or "Unknown"
                name_parts = full_name.split(maxsplit=1)
                first_name = name_parts[0] if name_parts else ""
                last_name = name_parts[1] if len(name_parts) > 1 else ""
        
                if email:
                    channel_user_data[user_id] = {
                        "email": email,
                        "name": full_name,
                        "first_name": first_name,
                        "last_name": last_name
                    }
                    
            except Exception as e:
                if verbose:
                    logger.warning(f"Error getting user info for {user_id}: {e}")
                continue
        
        # Build Existing Data Mappings
        data_user_ids = set(data["members"].keys())
        data_email_to_id = {}
        
        for user_id, member in data["members"].items():
            email = member.get("email", "").lower().strip()
            if email:
                data_email_to_id[email] = user_id
        
        # Process Channel Members
        new_member_count = 0
        removed_member_count = 0
        unchanged_count = 0
        
        for user_id, user_info in channel_user_data.items():
            current_email = user_info["email"]
            current_name = user_info["name"]
            current_first_name = user_info["first_name"]
            current_last_name = user_info["last_name"]
            
            # User ID exists in data
            if user_id in data["members"]:
                stored_email = data["members"][user_id].get("email", "").lower().strip()
                
                data["members"][user_id]["email"] = current_email
                data["members"][user_id]["name"] = current_name
                data["members"][user_id]["first_name"] = current_first_name
                data["members"][user_id]["last_name"] = current_last_name
                
                if verbose and stored_email != current_email:
                    logger.info(f"Email updated: {current_name} - {stored_email} → {current_email}")
                
                unchanged_count += 1
            
            # User ID not in data but email exists (user ID changed)
            elif current_email in data_email_to_id:
                old_user_id = data_email_to_id[current_email]
                
                if verbose:
                    logger.info(f"USER_ID CHANGED for {current_email}: {old_user_id} → {user_id}")
                
                # Migrate data
                data["members"][user_id] = data["members"][old_user_id].copy()
                data["members"][user_id].update({
                    "email": current_email,
                    "name": current_name,
                    "first_name": current_first_name,
                    "last_name": current_last_name
                })
                
                del data["members"][old_user_id]
                
                # Update observers list
                if old_user_id in data.get("observers", []):
                    data["observers"].remove(old_user_id)
                    data["observers"].append(user_id)
                    data["members"][user_id]["is_observer"] = True
                
                # Update history
                for entry in data["history"]:
                    if entry.get("leader_id") == old_user_id:
                        entry["leader_id"] = user_id
                
                # Update pending nominations
                for nomination in data.get("pending_nominations", {}).values():
                    if nomination.get("user_id") == old_user_id:
                        nomination["user_id"] = user_id
                
                unchanged_count += 1
            
            # Completely new member
            else:
                if verbose:
                    logger.info(f"NEW MEMBER: {current_name} - {current_email}")
                
                data["members"][user_id] = {
                    "name": current_name,
                    "first_name": current_first_name,
                    "last_name": current_last_name,
                    "email": current_email,
                    "first_seen": datetime.now().strftime("%Y-%m-%d"),
                    "last_led": None,
                    "total_led": 0,
                    "total_nominated": 0,
                    "total_accepted": 0,
                    "total_declined": 0,
                    "is_observer": user_id in data.get("observers", [])
                }
                new_member_count += 1
        
        # Find Members Who Left
        current_channel_emails = {info["email"] for info in channel_user_data.values()}
        
        members_to_remove = []
        for user_id, member in list(data["members"].items()):
            email = member.get("email", "").lower().strip()
            name = member.get("name", "Unknown")
            
            if user_id not in channel_user_data and email not in current_channel_emails:
                members_to_remove.append((user_id, name, email))
        
        for user_id, name, email in members_to_remove:
            if verbose:
                logger.info(f"REMOVING: {name} ({email})")
            del data["members"][user_id]
            removed_member_count += 1
            
            if user_id in data.get("observers", []):
                data["observers"].remove(user_id)
        
        # Save Data
        save_data(data)
        
        # Log summary
        if new_member_count > 0 or removed_member_count > 0 or verbose:
            logger.info(f"Sync complete: +{new_member_count} new, -{removed_member_count} removed, {len(data['members'])} total")
        
        return new_member_count, removed_member_count
    
    except Exception as e:
        logger.error(f"SYNC ERROR: {str(e)}")
        return 0, 0
# ============================================================================
# LEADER SELECTION LOGIC
# ============================================================================

def get_eligible_members(data, current_date, client=None, check_status=False):
    """Get list of members eligible to lead a meeting on the given date."""
    current_week = get_week_number(current_date)
    eligible = []
    
    for user_id, member in data["members"].items():
        # Skip permanent observers
        if member.get("is_observer", False):
            continue
        
        # Check if already led this week
        led_this_week = False
        for entry in data["history"]:
            if (entry["week"] == current_week and 
                entry["leader_id"] == user_id and 
                entry.get("status") == "accepted"):
                led_this_week = True
                break
        
        if led_this_week:
            continue
        
        # Check Slack availability status (optional)
        if check_status and client:
            is_available, _ = check_user_status(client, user_id)
            if not is_available:
                continue
        
        eligible.append(user_id)
    
    return eligible

def calculate_weights(data, eligible_members, current_date):
    """Calculate selection weights for weighted random selection."""
    weights = []
    
    for user_id in eligible_members:
        member = data["members"][user_id]
        last_led = member.get("last_led")
        
        if last_led:
            last_date = datetime.strptime(last_led, "%Y-%m-%d")
            days_since = (current_date - last_date).days
            weight = max(1, days_since)
        else:
            weight = 1000  # Never led before
        
        weights.append(weight)
    
    return weights

def select_random_leader(data, current_date, client=None, check_status=False):
    """Select a random leader using weighted probability."""
    eligible = get_eligible_members(data, current_date, client, check_status)
    
    if not eligible:
        return None
    
    weights = calculate_weights(data, eligible, current_date)
    selected = random.choices(eligible, weights=weights)[0]
    
    return selected
# ============================================================================
# INTERACTIVE BUTTON HANDLERS
# ============================================================================

@app.action("accept_nomination")
def handle_accept(ack, body, client):
    """Handle when nominated person clicks Accept button."""
    ack()
    
    nomination_id = body["actions"][0]["value"]
    user_id = body["user"]["id"]
    
    logger.info(f"Accept: {nomination_id} by {user_id}")
    
    data = load_data()
    
    if nomination_id not in data["pending_nominations"]:
        client.chat_postMessage(
            channel=body["channel"]["id"],
            text="⚠️ This nomination has already been processed or expired.",
            thread_ts=body["message"]["ts"]
        )
        return
    
    nomination = data["pending_nominations"][nomination_id]
    
    if nomination["user_id"] != user_id:
        client.chat_postMessage(
            channel=body["channel"]["id"],
            text=f"⚠️ Only <@{nomination['user_id']}> can respond to this nomination.",
            thread_ts=body["message"]["ts"]
        )
        return
    
    # Update member statistics
    member = data["members"][user_id]
    member["last_led"] = nomination["date"]
    member["total_led"] = member.get("total_led", 0) + 1
    member["total_accepted"] = member.get("total_accepted", 0) + 1
    
    member_name = member["name"]
    member_email = member.get("email", "N/A")
    total_led = member["total_led"]
    
    # Update history status
    for entry in data["history"]:
        if entry.get("nomination_id") == nomination_id:
            entry["status"] = "accepted"
            break
    
    # Remove from pending
    del data["pending_nominations"][nomination_id]
    
    save_data(data)
    
    # Update message
    try:
        client.chat_update(
            channel=body["channel"]["id"],
            ts=body["message"]["ts"],
            text=f"✅ <@{user_id}> accepted and will lead today's meeting!",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"✅ *<@{user_id}>* accepted and will lead today's meeting!\n\n"
                                f"**Name:** {member_name}\n"
                                f"**Email:** {member_email}\n"
                                f"**Total meetings led:** {total_led}\n\n"
                                f"_Thank you for leading! 🎉_"
                    }
                }
            ]
        )
    except Exception as e:
        logger.error(f"Error updating message: {e}")

@app.action("decline_nomination")
def handle_decline(ack, body, client):
    """Handle when nominated person clicks Decline button."""
    ack()
    
    nomination_id = body["actions"][0]["value"]
    user_id = body["user"]["id"]
    
    logger.info(f"Decline: {nomination_id} by {user_id}")
    
    data = load_data()
    
    if nomination_id not in data["pending_nominations"]:
        client.chat_postMessage(
            channel=body["channel"]["id"],
            text="⚠️ This nomination has already been processed or expired.",
            thread_ts=body["message"]["ts"]
        )
        return
    
    nomination = data["pending_nominations"][nomination_id]
    
    if nomination["user_id"] != user_id:
        client.chat_postMessage(
            channel=body["channel"]["id"],
            text=f"⚠️ Only <@{nomination['user_id']}> can respond to this nomination.",
            thread_ts=body["message"]["ts"]
        )
        return
    
    # Update member statistics
    member = data["members"][user_id]
    member["total_declined"] = member.get("total_declined", 0) + 1
    
    member_name = member["name"]
    member_email = member.get("email", "N/A")
    
    # Update history status
    for entry in data["history"]:
        if entry.get("nomination_id") == nomination_id:
            entry["status"] = "declined"
            break
    
    # Remove from pending
    del data["pending_nominations"][nomination_id]
    
    save_data(data)
    
    # Update message
    try:
        client.chat_update(
            channel=body["channel"]["id"],
            ts=body["message"]["ts"],
            text=f"❌ <@{user_id}> declined the nomination.",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"❌ *<@{user_id}>* declined the nomination.\n\n"
                                f"**Name:** {member_name}\n"
                                f"**Email:** {member_email}\n\n"
                                f"Please manually nominate another member using:\n"
                                f"`/meeting-leader select`"
                    }
                }
            ]
        )
    except Exception as e:
        logger.error(f"Error updating message: {e}")
# ============================================================================
# USER LOOKUP FUNCTIONS
# ============================================================================

def find_user_by_email(data, email):
    """
    Find a user by email (primary method).
    
    Returns:
        tuple: (user_id, member_data) or (None, None) if not found
    """
    email = email.lower().strip()
    
    for user_id, member in data["members"].items():
        if member.get("email", "").lower().strip() == email:
            return user_id, member
    
    return None, None

def find_user_by_name(data, name):
    """
    Find a user by name.
    
    Returns:
        tuple: (user_id, member_data, match_type) or (None, None, None) if not found
                or ("MULTIPLE", list_of_matches, "multiple") if multiple matches
    """
    name_input = name.lower().strip()
    name_parts = name_input.split()
    
    if len(name_parts) >= 2:
        first_name = name_parts[0]
        last_name = " ".join(name_parts[1:])
        
        # Exact match
        for user_id, member in data["members"].items():
            member_first = member.get("first_name", "").lower()
            member_last = member.get("last_name", "").lower()
            
            if member_first == first_name and member_last == last_name:
                return user_id, member, "exact"
        
        # Fuzzy match (starts with)
        for user_id, member in data["members"].items():
            member_first = member.get("first_name", "").lower()
            member_last = member.get("last_name", "").lower()
            
            if member_first.startswith(first_name) and member_last.startswith(last_name):
                return user_id, member, "fuzzy"
    
    # Single name
    if len(name_parts) == 1:
        single_name = name_parts[0]
        matches = []
        
        for user_id, member in data["members"].items():
            member_first = member.get("first_name", "").lower()
            member_last = member.get("last_name", "").lower()
            full_name = member.get("name", "").lower()
            
            if (single_name in member_first or 
                single_name in member_last or 
                single_name in full_name):
                matches.append((user_id, member))
        
        if len(matches) == 1:
            return matches[0][0], matches[0][1], "single"
        elif len(matches) > 1:
            return "MULTIPLE", matches, "multiple"
    
    return None, None, None

def find_users_batch(data, identifiers):
    """
    Find multiple users by emails or names.
    
    Args:
        data: Current data structure
        identifiers: List of email addresses or names
        
    Returns:
        dict: {
            "found": [(user_id, member, identifier, match_type), ...],
            "not_found": [identifier, ...],
            "ambiguous": [(identifier, matches), ...]
        }
    """
    result = {
        "found": [],
        "not_found": [],
        "ambiguous": []
    }
    
    for identifier in identifiers:
        identifier = identifier.strip()
        
        if not identifier:
            continue
        
        # Try email first (primary method)
        if "@" in identifier and "." in identifier:
            user_id, member = find_user_by_email(data, identifier)
            if user_id:
                result["found"].append((user_id, member, identifier, "email"))
                continue
        
        # Try name
        user_id, member, match_type = find_user_by_name(data, identifier)
        
        if user_id == "MULTIPLE":
            result["ambiguous"].append((identifier, member))  # member contains list of matches
        elif user_id:
            result["found"].append((user_id, member, identifier, match_type))
        else:
            result["not_found"].append(identifier)
    
    return result
# ============================================================================
# OBSERVER MANAGEMENT (BATCH OPERATIONS)
# ============================================================================

def handle_add_observer(data, parts, channel_id, say, client):
    """Add one or more members to observers list by email or name."""
    if len(parts) < 2:
        say("⚠️ **Invalid format**\n\n"
            "**Usage:**\n"
            "• `/meeting-leader add-observer email1@domain.com email2@domain.com`\n"
            "• `/meeting-leader add-observer John Doe, Jane Smith`\n"
            "• `/meeting-leader add-observer john@domain.com, Jane Smith`\n\n"
            "**Examples:**\n"
            "• `/meeting-leader add-observer john.smith@fanduel.com`\n"
            "• `/meeting-leader add-observer john@fanduel.com jane@fanduel.com`\n"
            "• `/meeting-leader add-observer John Smith, Jane Doe`")
        return
    
    # Sync members first (silent)
    sync_channel_members(client, channel_id, data, verbose=False)
    
    # Extract identifiers
    identifiers_str = " ".join(parts[1:])
    
    # Split by comma or space
    if "," in identifiers_str:
        identifiers = [i.strip() for i in identifiers_str.split(",")]
    else:
        identifiers = identifiers_str.split()
    
    logger.info(f"Adding observers: {identifiers}")
    
    # Find users
    lookup_result = find_users_batch(data, identifiers)
    
    # Initialize observers list
    if "observers" not in data:
        data["observers"] = []
    
    # Process found users
    added = []
    already_observers = []
    
    for user_id, member, identifier, match_type in lookup_result["found"]:
        if user_id in data["observers"]:
            already_observers.append((member["name"], member.get("email", "N/A")))
        else:
            data["observers"].append(user_id)
            data["members"][user_id]["is_observer"] = True
            added.append((member["name"], member.get("email", "N/A"), match_type))
    
    save_data(data)
    
    # Build response message
    lines = []
    
    if added:
        lines.append(f"✅ **{len(added)} observer(s) added successfully:**\n")
        for name, email, match_type in added:
            lines.append(f"• *{name}* - `{email}` _(matched by {match_type})_")
        lines.append("")
    
    if already_observers:
        lines.append(f"⚠️ **{len(already_observers)} already observer(s):**\n")
        for name, email in already_observers:
            lines.append(f"• *{name}* - `{email}`")
        lines.append("")
    
    if lookup_result["not_found"]:
        lines.append(f"❌ **{len(lookup_result['not_found'])} not found:**\n")
        for identifier in lookup_result["not_found"]:
            lines.append(f"• `{identifier}`")
        lines.append("")
    
    if lookup_result["ambiguous"]:
        lines.append(f"⚠️ **{len(lookup_result['ambiguous'])} ambiguous (multiple matches):**\n")
        for identifier, matches in lookup_result["ambiguous"]:
            lines.append(f"• `{identifier}` matches:")
            for uid, m in matches[:3]:  # Show max 3 matches
                lines.append(f"  - {m.get('name', 'Unknown')} ({m.get('email', 'N/A')})")
            lines.append("")
    
    if not lines:
        lines.append("⚠️ **No valid identifiers provided**")
    
    say("\n".join(lines))

def handle_remove_observer(data, parts, channel_id, say, client):
    """Remove one or more members from observers list by email or name."""
    if len(parts) < 2:
        say("⚠️ **Invalid format**\n\n"
            "**Usage:**\n"
            "• `/meeting-leader remove-observer email1@domain.com email2@domain.com`\n"
            "• `/meeting-leader remove-observer John Doe, Jane Smith`\n"
            "• `/meeting-leader remove-observer john@domain.com, Jane Smith`\n\n"
            "**Examples:**\n"
            "• `/meeting-leader remove-observer john.smith@fanduel.com`\n"
            "• `/meeting-leader remove-observer john@fanduel.com jane@fanduel.com`\n"
            "• `/meeting-leader remove-observer John Smith, Jane Doe`")
        return
    
    # Extract identifiers
    identifiers_str = " ".join(parts[1:])
    
    # Split by comma or space
    if "," in identifiers_str:
        identifiers = [i.strip() for i in identifiers_str.split(",")]
    else:
        identifiers = identifiers_str.split()
    
    logger.info(f"Removing observers: {identifiers}")
    
    # Find users
    lookup_result = find_users_batch(data, identifiers)
    
    # Initialize observers list
    if "observers" not in data:
        data["observers"] = []
    
    # Process found users
    removed = []
    not_observers = []
    
    for user_id, member, identifier, match_type in lookup_result["found"]:
        if user_id in data["observers"]:
            data["observers"].remove(user_id)
            data["members"][user_id]["is_observer"] = False
            removed.append((member["name"], member.get("email", "N/A"), match_type))
        else:
            not_observers.append((member["name"], member.get("email", "N/A")))
    
    save_data(data)
    
    # Build response message
    lines = []
    
    if removed:
        lines.append(f"✅ **{len(removed)} observer(s) removed successfully:**\n")
        for name, email, match_type in removed:
            lines.append(f"• *{name}* - `{email}` _(matched by {match_type})_")
        lines.append("")
    
    if not_observers:
        lines.append(f"⚠️ **{len(not_observers)} not observer(s):**\n")
        for name, email in not_observers:
            lines.append(f"• *{name}* - `{email}`")
        lines.append("")
    
    if lookup_result["not_found"]:
        lines.append(f"❌ **{len(lookup_result['not_found'])} not found:**\n")
        for identifier in lookup_result["not_found"]:
            lines.append(f"• `{identifier}`")
        lines.append("")
    
    if lookup_result["ambiguous"]:
        lines.append(f"⚠️ **{len(lookup_result['ambiguous'])} ambiguous (multiple matches):**\n")
        for identifier, matches in lookup_result["ambiguous"]:
            lines.append(f"• `{identifier}` matches:")
            for uid, m in matches[:3]:
                lines.append(f"  - {m.get('name', 'Unknown')} ({m.get('email', 'N/A')})")
            lines.append("")
    
    if not lines:
        lines.append("⚠️ **No valid identifiers provided**")
    
    say("\n".join(lines))

def handle_list_observers(data, say):
    """List all observers."""
    if "observers" not in data or not data["observers"]:
        say("📋 **No observers currently set**\n\n"
            "All members are eligible for selection.\n\n"
            "**To add observers:**\n"
            "• `/meeting-leader add-observer email@domain.com`\n"
            "• `/meeting-leader add-observer John Doe, Jane Smith`")
        return
    
    lines = ["*👀 Current Observers*\n"]
    lines.append("_These members are excluded from random selection:_\n")
    
    for user_id in data["observers"]:
        if user_id in data["members"]:
            member = data["members"][user_id]
            name = member.get("name", "Unknown")
            email = member.get("email", "N/A")
            led = member.get("total_led", 0)
            
            lines.append(f"*{name}*")
            lines.append(f"  Email: `{email}`")
            lines.append(f"  Led: {led} meeting(s)\n")
    
    lines.append(f"\n**Total observers:** {len(data['observers'])}")
    
    say("\n".join(lines))
# ============================================================================
# OTHER COMMAND HANDLERS
# ============================================================================

def handle_select(data, channel_id, say, client):
    """Manually select a random meeting leader."""
    logger.info("Manual select command")
    
    # Auto-sync members (silent)
    sync_channel_members(client, channel_id, data, verbose=False)
    
    today = datetime.now()
    day_name = today.strftime("%A")
    
    # Select leader
    selected_id = select_random_leader(data, today, client, check_status=False)
    
    if not selected_id:
        say("🚫 **No available leaders today**\n\n"
            "Possible reasons:\n"
            "• All members already led this week\n"
            "• All members are marked as observers")
        save_data(data)
        return
    
    member = data["members"][selected_id]
    member_name = member["name"]
    member_email = member.get("email", "N/A")
    
    # Check if meeting day
    if day_name not in ["Tuesday", "Thursday"]:
        say(f"🎲 **Random selection result:** *<@{selected_id}>*\n\n"
            f"**Name:** {member_name}\n"
            f"**Email:** {member_email}\n\n"
            f"⚠️ Today is {day_name} - no sync meeting scheduled.")
        return
    
    # Meeting day - proceed with nomination
    member["total_nominated"] = member.get("total_nominated", 0) + 1
    
    nomination_id = f"{selected_id}_{today.strftime('%Y%m%d_%H%M%S')}"
    
    data["pending_nominations"][nomination_id] = {
        "user_id": selected_id,
        "date": today.strftime("%Y-%m-%d"),
        "day": day_name,
        "channel_id": channel_id,
        "week": get_week_number(today)
    }
    
    data["history"].append({
        "date": today.strftime("%Y-%m-%d"),
        "day": day_name,
        "leader_id": selected_id,
        "leader_name": member_name,
        "leader_email": member_email,
        "week": get_week_number(today),
        "status": "nominated",
        "nomination_id": nomination_id
    })
    
    save_data(data)
    
    try:
        say(
            text=f"🎲 <@{selected_id}> has been manually selected to lead today's meeting!",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"🎲 *<@{selected_id}>* has been manually selected to lead today's meeting!\n\n"
                                f"**Name:** {member_name}\n"
                                f"**Email:** {member_email}\n\n"
                                f"Please confirm your availability:"
                    }
                },
                {
                    "type": "actions",
                    "block_id": "nomination_response",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "✅ Accept"},
                            "style": "primary",
                            "value": nomination_id,
                            "action_id": "accept_nomination"
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "❌ Decline"},
                            "style": "danger",
                            "value": nomination_id,
                            "action_id": "decline_nomination"
                        }
                    ]
                }
            ]
        )
    except Exception as e:
        logger.error(f"Error sending nomination: {e}")

def handle_sync(data, channel_id, say, client):
    """Manually trigger member synchronization."""
    logger.info("Manual sync command")
    
    added, removed = sync_channel_members(client, channel_id, data, verbose=True)
    
    if added == 0 and removed == 0:
        say("✅ **Already in sync!**\n\nNo changes detected.")
    else:
        msg = "✅ **Sync complete!**\n\n"
        if added > 0:
            msg += f"• **Added:** {added} new member(s)\n"
        if removed > 0:
            msg += f"• **Removed:** {removed} member(s)\n"
        say(msg)

def handle_list(data, channel_id, say, client):
    """List all members with their eligibility status."""
    logger.info("List command")
    
    sync_channel_members(client, channel_id, data, verbose=False)
    
    if not data["members"]:
        say("📋 No members found in this channel.")
        return
    
    lines = ["*📋 Current Rotation Members*\n"]
    
    eligible = []
    observers = []
    
    for user_id, member in data["members"].items():
        total_led = member.get("total_led", 0)
        is_observer = member.get("is_observer", False)
        email = member.get("email", "N/A")
        name = member.get("name", "Unknown")
        
        if is_observer:
            observers.append((name, email, total_led))
        else:
            eligible.append((name, email, total_led))
    
    eligible.sort(key=lambda x: x[2])
    
    if eligible:
        lines.append("*✅ Eligible to Lead:*")
        for name, email, total_led in eligible:
            lines.append(f"  • *{name}* - `{email}` - Led: {total_led}")
        lines.append("")
    
    if observers:
        lines.append("*👀 Observers (Excluded):*")
        for name, email, total_led in observers:
            lines.append(f"  • *{name}* - `{email}` - Led: {total_led}")
    
    say("\n".join(lines))

def handle_history(data, say):
    """Show last 10 meeting leaders."""
    logger.info("History command")
    
    if not data["history"]:
        say("📊 **No meeting history yet.**")
        return
    
    lines = ["*📊 Recent Meeting Leaders*\n"]
    
    for entry in reversed(data["history"][-10:]):
        status_emoji = {
            "nominated": "🎲",
            "accepted": "✅",
            "declined": "❌"
        }.get(entry.get("status", "nominated"), "")
        
        date = entry['date']
        leader_name = entry['leader_name']
        status = entry.get('status', 'nominated')
        
        lines.append(f"{status_emoji} **{date}** - {leader_name} ({status})")
    
    say("\n".join(lines))

def handle_stats(data, say):
    """Show detailed leadership statistics."""
    logger.info("Stats command")
    
    if not data["members"]:
        say("📊 **No statistics available yet.**")
        return
    
    lines = ["*📊 Leadership Statistics*\n"]
    
    sorted_members = sorted(
        data["members"].items(),
        key=lambda x: x[1].get("total_led", 0),
        reverse=True
    )
    
    for user_id, member in sorted_members:
        name = member.get("name", "Unknown")
        email = member.get("email", "N/A")
        nominated = member.get("total_nominated", 0)
        accepted = member.get("total_accepted", 0)
        declined = member.get("total_declined", 0)
        led = member.get("total_led", 0)
        is_observer = member.get("is_observer", False)
        
        status = "👀 Observer" if is_observer else "✅ Eligible"
        acceptance_rate = f"{(accepted/nominated*100):.0f}%" if nominated > 0 else "N/A"
        
        lines.append(f"*{name}* (`{email}`) - {status}")
        lines.append(f"  Nominated: {nominated} | Accepted: {accepted} | Declined: {declined} | Led: {led} | Rate: {acceptance_rate}")
        lines.append("")
    
    say("\n".join(lines))
# ============================================================================
# SLASH COMMAND HANDLER
# ============================================================================

@app.command("/meeting-leader")
def handle_meeting_leader(ack, command, say, client):
    """Main slash command handler for /meeting-leader."""
    ack()
    
    text = command.get("text", "").strip()
    channel_id = command["channel_id"]
    
    logger.info(f"Command: /meeting-leader {text}")
    
    data = load_data()
    
    parts = text.split()
    action = parts[0].lower() if parts else "help"
    
    if action == "select":
        handle_select(data, channel_id, say, client)
    elif action == "sync":
        handle_sync(data, channel_id, say, client)
    elif action == "list":
        handle_list(data, channel_id, say, client)
    elif action == "history":
        handle_history(data, say)
    elif action == "stats" or action == "statistics":
        handle_stats(data, say)
    elif action == "add-observer":
        handle_add_observer(data, parts, channel_id, say, client)
    elif action == "remove-observer":
        handle_remove_observer(data, parts, channel_id, say, client)
    elif action == "list-observers":
        handle_list_observers(data, say)
    else:
        say("""*🤖 Meeting Leader Bot - Command Reference*

*📋 Main Commands:*
• `/meeting-leader select` - Manually pick random leader
• `/meeting-leader sync` - Sync channel members
• `/meeting-leader list` - Show all members
• `/meeting-leader history` - Show last 10 leaders
• `/meeting-leader stats` - Show statistics

*👀 Observer Management (Batch Support):*
• `/meeting-leader add-observer email1@domain.com email2@domain.com`
• `/meeting-leader add-observer John Doe, Jane Smith`
• `/meeting-leader remove-observer email@domain.com, John Doe`
• `/meeting-leader list-observers` - Show all observers

*⚙️ Automated Behavior:*
• Every **Tuesday & Thursday at 10:00 AM** - automatic nomination
• Checks for holidays and vacation status
• Silent operation (minimal logs)

*✅ Eligibility Rules:*
• Cannot lead more than **once per week**
• Observers are excluded
• Members who haven't led recently are prioritized""")

# ============================================================================
# EVENT LISTENERS
# ============================================================================

@app.event("member_joined_channel")
def handle_member_joined(event, client):
    """Auto-sync when someone joins the channel."""
    channel_id = event["channel"]
    
    if channel_id == MEETING_CHANNEL_ID:
        data = load_data()
        sync_channel_members(client, channel_id, data, verbose=False)
        logger.info(f"Auto-sync: member joined")

@app.event("member_left_channel")
def handle_member_left(event, client):
    """Auto-sync when someone leaves the channel."""
    channel_id = event["channel"]
    
    if channel_id == MEETING_CHANNEL_ID:
        data = load_data()
        sync_channel_members(client, channel_id, data, verbose=False)
        logger.info(f"Auto-sync: member left")

# ============================================================================
# SCHEDULER
# ============================================================================

def run_scheduler(client):
    """Run the scheduler in a separate background thread."""
    schedule.every().tuesday.at("10:00").do(lambda: automated_nomination(client))
    schedule.every().thursday.at("10:00").do(lambda: automated_nomination(client))
    
    logger.info("Scheduler started: Tue/Thu at 10:00 AM")
    
    while True:
        try:
            schedule.run_pending()
            time.sleep(60)
        except Exception as e:
            logger.error(f"Scheduler error: {e}")
            time.sleep(60)

# ============================================================================
# APPLICATION STARTUP
# ============================================================================

if __name__ == "__main__":
    logger.info("=== MEETING LEADER BOT STARTING ===")
    
    # Validate environment variables
    if not os.environ.get("SLACK_BOT_TOKEN"):
        logger.error("SLACK_BOT_TOKEN not set!")
        exit(1)
    
    if not os.environ.get("SLACK_APP_TOKEN"):
        logger.error("SLACK_APP_TOKEN not set!")
        exit(1)
    
    if not MEETING_CHANNEL_ID:
        logger.warning("MEETING_CHANNEL_ID not set - automated nominations disabled")
    
    logger.info("Environment variables validated")
    
    # Initialize Slack client
    slack_client = app.client
    
    # Start scheduler thread
    scheduler_thread = threading.Thread(
        target=run_scheduler,
        args=(slack_client,),
        daemon=True,
        name="SchedulerThread"
    )
    scheduler_thread.start()
    logger.info("Scheduler thread started")
    
    # Start Socket Mode handler
    logger.info("Bot is now running")
    
    try:
        handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
        handler.start()
    except KeyboardInterrupt:
        logger.info("Bot stopped (Ctrl+C)")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        exit(1)        