"""
Slack Meeting Leader Bot - Control Panel Edition
============================================================
Enhanced with persistent control panel and interactive buttons.

Features:
- Persistent control panel with visual buttons
- Ephemeral responses (private to user)
- Public audit logs for changes only
- Interactive user selection menus for observer management
- Visual sync buttons and status indicators
- Quick action buttons on member lists
- Automatic re-nomination on decline
- Silent operation with minimal public logs
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

logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ============================================================================
# INITIALIZATION
# ============================================================================

app = App(token=os.environ.get("SLACK_BOT_TOKEN"))
DATA_FILE = "meeting_data.json"
MEETING_CHANNEL_ID = os.environ.get("MEETING_CHANNEL_ID")

# Store selected users temporarily (in-memory)
selected_users_cache = {}

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
        "observers": [],
        "control_panel_ts": None
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

def post_audit_log(client, channel_id, actor_name, action, affected_names):
    """
    Post a public audit log for observer management actions.
    Only posts when there are actual changes.
    """
    if not affected_names:
        return
    
    if len(affected_names) == 1:
        affected_str = affected_names[0]
    elif len(affected_names) == 2:
        affected_str = f"{affected_names[0]} and {affected_names[1]}"
    else:
        affected_str = ", ".join(affected_names[:-1]) + f", and {affected_names[-1]}"
    
    message = f"📋 _{actor_name} {action} {affected_str}._"
    
    try:
        client.chat_postMessage(
            channel=channel_id,
            text=message
        )
    except Exception as e:
        logger.error(f"Error posting audit log: {e}")

def post_ephemeral_message(client, channel_id, user_id, text=None, blocks=None):
    """
    Post an ephemeral message visible only to the specific user.
    """
    try:
        client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text=text if text else "Message",
            blocks=blocks if blocks else None
        )
    except Exception as e:
        logger.error(f"Error posting ephemeral message: {e}")
# ============================================================================
# MEMBER SYNCHRONIZATION
# ============================================================================

def sync_channel_members(client, channel_id, data, verbose=False):
    """
    Synchronize member list from Slack channel.
    
    Returns:
        dict: {
            "new_count": int,
            "removed_count": int,
            "total_count": int,
            "new_members": [names],
            "removed_members": [names]
        }
    """
    if verbose:
        logger.info(f"SYNC STARTED - Channel: {channel_id}")
    
    try:
        result = client.conversations_members(channel=channel_id)
        channel_member_ids = set(result["members"])
        
        bot_info = client.auth_test()
        bot_user_id = bot_info["user_id"]
        channel_member_ids.discard(bot_user_id)
        
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
        
        data_email_to_id = {}
        for user_id, member in data["members"].items():
            email = member.get("email", "").lower().strip()
            if email:
                data_email_to_id[email] = user_id
        
        new_member_count = 0
        removed_member_count = 0
        new_member_names = []
        removed_member_names = []
        
        for user_id, user_info in channel_user_data.items():
            current_email = user_info["email"]
            current_name = user_info["name"]
            current_first_name = user_info["first_name"]
            current_last_name = user_info["last_name"]
            
            if user_id in data["members"]:
                stored_email = data["members"][user_id].get("email", "").lower().strip()
                
                data["members"][user_id]["email"] = current_email
                data["members"][user_id]["name"] = current_name
                data["members"][user_id]["first_name"] = current_first_name
                data["members"][user_id]["last_name"] = current_last_name
                
                if verbose and stored_email != current_email:
                    logger.info(f"Email updated: {current_name} - {stored_email} → {current_email}")
            
            elif current_email in data_email_to_id:
                old_user_id = data_email_to_id[current_email]
                
                if verbose:
                    logger.info(f"USER_ID CHANGED for {current_email}: {old_user_id} → {user_id}")
                
                data["members"][user_id] = data["members"][old_user_id].copy()
                data["members"][user_id].update({
                    "email": current_email,
                    "name": current_name,
                    "first_name": current_first_name,
                    "last_name": current_last_name
                })
                
                del data["members"][old_user_id]
                
                if old_user_id in data.get("observers", []):
                    data["observers"].remove(old_user_id)
                    data["observers"].append(user_id)
                    data["members"][user_id]["is_observer"] = True
                
                for entry in data["history"]:
                    if entry.get("leader_id") == old_user_id:
                        entry["leader_id"] = user_id
                
                for nomination in data.get("pending_nominations", {}).values():
                    if nomination.get("user_id") == old_user_id:
                        nomination["user_id"] = user_id
            
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
                new_member_names.append(current_name)
        
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
            removed_member_names.append(name)
            
            if user_id in data.get("observers", []):
                data["observers"].remove(user_id)
        
        save_data(data)
        
        if new_member_count > 0 or removed_member_count > 0 or verbose:
            logger.info(f"Sync complete: +{new_member_count} new, -{removed_member_count} removed, {len(data['members'])} total")
        
        return {
            "new_count": new_member_count,
            "removed_count": removed_member_count,
            "total_count": len(data["members"]),
            "new_members": new_member_names,
            "removed_members": removed_member_names
        }
    
    except Exception as e:
        logger.error(f"SYNC ERROR: {str(e)}")
        return {
            "new_count": 0,
            "removed_count": 0,
            "total_count": len(data.get("members", {})),
            "new_members": [],
            "removed_members": []
        }
# ============================================================================
# CONTROL PANEL
# ============================================================================

def create_control_panel_blocks():
    """Create the blocks for the control panel."""
    return [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "🤖 Meeting Leader Bot - Control Panel"
            }
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*Quick Actions - Click any button below:*"
            }
        },
        {
            "type": "divider"
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "🔄 Sync Members"
                    },
                    "style": "primary",
                    "action_id": "panel_sync_members"
                }
            ]
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "📋 List Members"
                    },
                    "action_id": "panel_list_members"
                }
            ]
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "➕ Add Observers"
                    },
                    "action_id": "panel_add_observers"
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "➖ Remove Observers"
                    },
                    "action_id": "panel_remove_observers"
                },
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "👀 List Observers"
                    },
                    "action_id": "panel_list_observers"
                }
            ]
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "📈 Statistics"
                    },
                    "action_id": "panel_statistics"
                }
            ]
        },
        {
            "type": "divider"
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "💡 *Button Guide:*\n• *🔄 Sync Members* - Update member list from channel\n• *📋 List Members* - View all members with quick actions (private)\n• *➕ Add Observers* - Select members to exclude from rotation (private)\n• *➖ Remove Observers* - Re-enable observers for rotation (private)\n• *👀 List Observers* - View all excluded members (private)\n• *📈 Statistics* - View meeting leadership stats (private)"
                }
            ]
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "_Most responses are private (only you see them). Public logs show only when changes are made._"
                }
            ]
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "_Slash commands still available: `/meeting-leader help` for full command list_"
                }
            ]
        }
    ]

def post_control_panel(client, channel_id):
    """
    Post or update the control panel in the channel.
    
    Returns:
        str: Message timestamp of the control panel
    """
    try:
        response = client.chat_postMessage(
            channel=channel_id,
            text="🤖 Meeting Leader Bot - Control Panel",
            blocks=create_control_panel_blocks()
        )
        
        logger.info(f"Control panel posted: {response['ts']}")
        
        try:
            client.pins_add(
                channel=channel_id,
                timestamp=response['ts']
            )
            logger.info("Control panel pinned successfully")
        except Exception as e:
            logger.warning(f"Could not pin control panel: {e}")
        
        return response['ts']
    
    except Exception as e:
        logger.error(f"Error posting control panel: {e}")
        return None

def refresh_control_panel(client, channel_id, data):
    """Refresh the control panel (update existing or create new)."""
    
    control_panel_ts = data.get("control_panel_ts")
    
    if control_panel_ts:
        try:
            client.chat_update(
                channel=channel_id,
                ts=control_panel_ts,
                text="🤖 Meeting Leader Bot - Control Panel",
                blocks=create_control_panel_blocks()
            )
            logger.info("Control panel refreshed")
            return control_panel_ts
        except Exception as e:
            logger.warning(f"Could not update control panel, posting new one: {e}")
    
    new_ts = post_control_panel(client, channel_id)
    if new_ts:
        data["control_panel_ts"] = new_ts
        save_data(data)
    
    return new_ts
# ============================================================================
# LEADER SELECTION LOGIC
# ============================================================================

def get_eligible_members(data, current_date, client=None, check_status=False):
    """Get list of members eligible to lead a meeting on the given date."""
    current_week = get_week_number(current_date)
    eligible = []
    
    for user_id, member in data["members"].items():
        if member.get("is_observer", False):
            continue
        
        led_this_week = False
        for entry in data["history"]:
            if (entry["week"] == current_week and 
                entry["leader_id"] == user_id and 
                entry.get("status") == "accepted"):
                led_this_week = True
                break
        
        if led_this_week:
            continue
        
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
            weight = 1000
        
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

def create_nomination(client, channel_id, selected_id, data, is_auto=True):
    """
    Create and send a nomination message.
    
    Returns:
        str: nomination_id or None on error
    """
    today = datetime.now()
    day_name = today.strftime("%A")
    
    member = data["members"][selected_id]
    member_name = member['name']
    member_email = member.get('email', 'N/A')
    
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
    
    selection_type = "randomly" if is_auto else "manually"
    
    try:
        client.chat_postMessage(
            channel=channel_id,
            text=f"🎲 <@{selected_id}> has been {selection_type} selected to lead today's meeting!",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"🎲 *<@{selected_id}>* has been {selection_type} selected to lead today's meeting!\n\n"
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
        logger.info(f"Nomination created: {member_name}")
        return nomination_id
    except Exception as e:
        logger.error(f"Error sending nomination: {e}")
        return None
# ============================================================================
# AUTOMATED NOMINATION SYSTEM
# ============================================================================

def automated_nomination(client):
    """Automated nomination process that runs at scheduled times."""
    if not MEETING_CHANNEL_ID:
        logger.error("MEETING_CHANNEL_ID not set")
        return
    
    data = load_data()
    today = datetime.now()
    day_name = today.strftime("%A")
    
    if day_name not in ["Tuesday", "Thursday"]:
        return
    
    logger.info(f"Automated nomination: {today.strftime('%Y-%m-%d')} ({day_name})")
    
    if is_holiday(today):
        logger.info("Public holiday detected")
        try:
            client.chat_postMessage(
                channel=MEETING_CHANNEL_ID,
                text="🏖️ No meeting today - it's a public holiday! Enjoy your day off!"
            )
        except Exception as e:
            logger.error(f"Error posting holiday message: {e}")
        return
    
    sync_channel_members(client, MEETING_CHANNEL_ID, data, verbose=False)
    
    selected_id = select_random_leader(data, today, client, check_status=True)
    
    if not selected_id:
        logger.warning("No eligible leaders available")
        try:
            client.chat_postMessage(
                channel=MEETING_CHANNEL_ID,
                text="🚫 *Meeting cancelled* - No available leaders today.\n\n"
                     "Possible reasons:\n"
                     "• All members already led this week\n"
                     "• All members are marked as observers\n\n"
                     "Use the control panel or `/meeting-leader list` to check member status."
            )
        except Exception as e:
            logger.error(f"Error posting cancellation: {e}")
        save_data(data)
        return
    
    is_available, status_message = check_user_status(client, selected_id)
    
    member = data["members"][selected_id]
    member_name = member['name']
    
    logger.info(f"Selected: {member_name} - Available: {is_available}")
    
    if not is_available:
        try:
            client.chat_postMessage(
                channel=MEETING_CHANNEL_ID,
                text=f"⚠️ *Nominated person is unavailable*\n\n"
                     f"*{member_name}* (<@{selected_id}>) was randomly selected but is currently unavailable:\n"
                     f"• Status: {status_message}\n\n"
                     f"Please nominate another team member manually using the control panel or:\n"
                     f"`/meeting-leader select`"
            )
        except Exception as e:
            logger.error(f"Error posting unavailable message: {e}")
        save_data(data)
        return
    
    create_nomination(client, MEETING_CHANNEL_ID, selected_id, data, is_auto=True)
# ============================================================================
# NOMINATION RESPONSE HANDLERS
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
    
    member = data["members"][user_id]
    member["last_led"] = nomination["date"]
    member["total_led"] = member.get("total_led", 0) + 1
    member["total_accepted"] = member.get("total_accepted", 0) + 1
    
    member_name = member["name"]
    member_email = member.get("email", "N/A")
    total_led = member["total_led"]
    
    for entry in data["history"]:
        if entry.get("nomination_id") == nomination_id:
            entry["status"] = "accepted"
            break
    
    del data["pending_nominations"][nomination_id]
    
    save_data(data)
    
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
    """Handle when nominated person clicks Decline button - auto re-nominate."""
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
    
    member = data["members"][user_id]
    member["total_declined"] = member.get("total_declined", 0) + 1
    
    member_name = member["name"]
    member_email = member.get("email", "N/A")
    
    for entry in data["history"]:
        if entry.get("nomination_id") == nomination_id:
            entry["status"] = "declined"
            break
    
    del data["pending_nominations"][nomination_id]
    
    save_data(data)
    
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
                                f"_Selecting another person..._"
                    }
                }
            ]
        )
    except Exception as e:
        logger.error(f"Error updating message: {e}")
    
    logger.info("Auto re-nomination triggered")
    
    today = datetime.now()
    channel_id = body["channel"]["id"]
    
    new_selected_id = select_random_leader(data, today, client, check_status=True)
    
    if not new_selected_id:
        logger.warning("No more eligible leaders for re-nomination")
        try:
            client.chat_postMessage(
                channel=channel_id,
                text="🚫 *No more available leaders today*\n\n"
                     "All eligible members have either led this week, declined, or are unavailable.\n\n"
                     "Please coordinate manually in the channel."
            )
        except Exception as e:
            logger.error(f"Error posting no-leaders message: {e}")
        return
    
    is_available, status_message = check_user_status(client, new_selected_id)
    
    if not is_available:
        logger.warning(f"Re-nominated person unavailable: {status_message}")
        try:
            client.chat_postMessage(
                channel=channel_id,
                text=f"⚠️ *Next person also unavailable*\n\n"
                     f"Please coordinate manually or use the control panel to try again."
            )
        except Exception as e:
            logger.error(f"Error posting unavailable message: {e}")
        return
    
    create_nomination(client, channel_id, new_selected_id, data, is_auto=False)
# ============================================================================
# CONTROL PANEL BUTTON HANDLERS (EPHEMERAL RESPONSES)
# ============================================================================

@app.action("panel_sync_members")
def handle_panel_sync(ack, body, client):
    """Handle Sync Members button from control panel."""
    ack()
    
    channel_id = body["channel"]["id"]
    user_id = body["user"]["id"]
    data = load_data()
    
    post_ephemeral_message(
        client, 
        channel_id, 
        user_id,
        text="🔄 Syncing members...",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "🔄 *Syncing members...*\n\nPlease wait..."
                }
            }
        ]
    )
    
    sync_result = sync_channel_members(client, channel_id, data, verbose=False)
    
    result_lines = []
    
    if sync_result["new_count"] > 0 or sync_result["removed_count"] > 0:
        result_lines.append("✅ *Sync Complete!*\n")
        
        if sync_result["new_count"] > 0:
            result_lines.append(f"**Added:** {sync_result['new_count']} member(s)")
            for name in sync_result["new_members"][:5]:
                result_lines.append(f"  • {name}")
            if len(sync_result["new_members"]) > 5:
                result_lines.append(f"  • ... and {len(sync_result['new_members']) - 5} more")
            result_lines.append("")
        
        if sync_result["removed_count"] > 0:
            result_lines.append(f"**Removed:** {sync_result['removed_count']} member(s)")
            for name in sync_result["removed_members"][:5]:
                result_lines.append(f"  • {name}")
            if len(sync_result["removed_members"]) > 5:
                result_lines.append(f"  • ... and {len(sync_result['removed_members']) - 5} more")
            result_lines.append("")
        
        result_lines.append(f"**Total members:** {sync_result['total_count']}")
        
        try:
            client.chat_postMessage(
                channel=channel_id,
                text="\n".join(result_lines)
            )
        except Exception as e:
            logger.error(f"Error posting sync result: {e}")
    else:
        post_ephemeral_message(
            client,
            channel_id,
            user_id,
            text="✅ *Sync Complete!*\n\nNo changes detected. All members are up to date.\n\n**Total members:** " + str(sync_result['total_count'])
        )

@app.action("panel_list_members")
def handle_panel_list(ack, body, client):
    """Handle List Members button from control panel - EPHEMERAL."""
    ack()
    
    channel_id = body["channel"]["id"]
    user_id = body["user"]["id"]
    data = load_data()
    
    if not data["members"]:
        post_ephemeral_message(
            client,
            channel_id,
            user_id,
            text="📋 No members found. Use 🔄 Sync Members button first."
        )
        return
    
    eligible = []
    observers = []
    
    for uid, member in data["members"].items():
        total_led = member.get("total_led", 0)
        is_observer = member.get("is_observer", False)
        email = member.get("email", "N/A")
        name = member.get("name", "Unknown")
        
        if is_observer:
            observers.append((uid, name, email, total_led))
        else:
            eligible.append((uid, name, email, total_led))
    
    eligible.sort(key=lambda x: x[3])
    
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "📋 Current Rotation Members"
            }
        }
    ]
    
    if eligible:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*✅ Eligible to Lead:*"
            }
        })
        
        for uid, name, email, total_led in eligible[:15]:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{name}*\n`{email}` • Led: {total_led} meeting(s)"
                },
                "accessory": {
                    "type": "overflow",
                    "options": [
                        {
                            "text": {
                                "type": "plain_text",
                                "text": "👀 Make Observer"
                            },
                            "value": f"make_observer_{uid}"
                        }
                    ],
                    "action_id": "member_quick_action"
                }
            })
        
        if len(eligible) > 15:
            blocks.append({
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"_Showing 15 of {len(eligible)} eligible members_"
                    }
                ]
            })
        
        blocks.append({"type": "divider"})
    
    if observers:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*👀 Observers (Excluded from Selection):*"
            }
        })
        
        for uid, name, email, total_led in observers[:10]:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{name}*\n`{email}` • Led: {total_led} meeting(s)"
                },
                "accessory": {
                    "type": "overflow",
                    "options": [
                        {
                            "text": {
                                "type": "plain_text",
                                "text": "✅ Remove Observer Status"
                            },
                            "value": f"remove_observer_{uid}"
                        }
                    ],
                    "action_id": "member_quick_action"
                }
            })
        
        if len(observers) > 10:
            blocks.append({
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"_Showing 10 of {len(observers)} observers_"
                    }
                ]
            })
    
    post_ephemeral_message(
        client,
        channel_id,
        user_id,
        text="Current rotation members",
        blocks=blocks
    )

@app.action("panel_add_observers")
def handle_panel_add_observers(ack, body, client):
    """Handle Add Observers button from control panel - EPHEMERAL."""
    ack()
    
    channel_id = body["channel"]["id"]
    user_id = body["user"]["id"]
    
    try:
        post_ephemeral_message(
            client,
            channel_id,
            user_id,
            text="Select members to add as observers:",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "*➕ Add Observers*\n\nSelect one or more members to exclude from meeting leader selection:"
                    }
                },
                {
                    "type": "input",
                    "block_id": "add_observers_input",
                    "element": {
                        "type": "multi_users_select",
                        "placeholder": {
                            "type": "plain_text",
                            "text": "Select members..."
                        },
                        "action_id": "add_observers_select"
                    },
                    "label": {
                        "type": "plain_text",
                        "text": "Members to add as observers"
                    }
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "✅ Confirm Add"
                            },
                            "style": "primary",
                            "action_id": "confirm_add_observers"
                        },
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "❌ Cancel"
                            },
                            "action_id": "cancel_observer_action"
                        }
                    ]
                }
            ]
        )
    except Exception as e:
        logger.error(f"Error showing add observer menu: {e}")

@app.action("panel_remove_observers")
def handle_panel_remove_observers(ack, body, client):
    """Handle Remove Observers button from control panel - EPHEMERAL."""
    ack()
    
    channel_id = body["channel"]["id"]
    user_id = body["user"]["id"]
    data = load_data()
    
    current_observer_ids = data.get("observers", [])
    
    if not current_observer_ids:
        post_ephemeral_message(
            client,
            channel_id,
            user_id,
            text="⚠️ **No observers to remove**\n\nThere are currently no observers set."
        )
        return
    
    try:
        post_ephemeral_message(
            client,
            channel_id,
            user_id,
            text="Select observers to remove:",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "*➖ Remove Observers*\n\nSelect one or more observers to make eligible for meeting leader selection:"
                    }
                },
                {
                    "type": "input",
                    "block_id": "remove_observers_input",
                    "element": {
                        "type": "multi_users_select",
                        "placeholder": {
                            "type": "plain_text",
                            "text": "Select observers to remove..."
                        },
                        "action_id": "remove_observers_select"
                    },
                    "label": {
                        "type": "plain_text",
                        "text": "Observers to remove"
                    }
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "✅ Confirm Remove"
                            },
                            "style": "primary",
                            "action_id": "confirm_remove_observers"
                        },
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "❌ Cancel"
                            },
                            "action_id": "cancel_observer_action"
                        }
                    ]
                }
            ]
        )
    except Exception as e:
        logger.error(f"Error showing remove observer menu: {e}")

@app.action("panel_list_observers")
def handle_panel_list_observers(ack, body, client):
    """Handle List Observers button from control panel - EPHEMERAL."""
    ack()
    
    channel_id = body["channel"]["id"]
    user_id = body["user"]["id"]
    data = load_data()
    
    if "observers" not in data or not data["observers"]:
        post_ephemeral_message(
            client,
            channel_id,
            user_id,
            text="📋 **No observers currently set**\n\nAll members are eligible for selection."
        )
        return
    
    lines = ["*👀 Current Observers*\n"]
    lines.append("_These members are excluded from random selection:_\n")
    
    for user_id_observer in data["observers"]:
        if user_id_observer in data["members"]:
            member = data["members"][user_id_observer]
            name = member.get("name", "Unknown")
            email = member.get("email", "N/A")
            led = member.get("total_led", 0)
            
            lines.append(f"*{name}* - `{email}` - Led: {led}")
    
    lines.append(f"\n**Total observers:** {len(data['observers'])}")
    
    post_ephemeral_message(
        client,
        channel_id,
        user_id,
        text="\n".join(lines)
    )

@app.action("panel_statistics")
def handle_panel_statistics(ack, body, client):
    """Handle Statistics button from control panel - EPHEMERAL."""
    ack()
    
    channel_id = body["channel"]["id"]
    user_id = body["user"]["id"]
    data = load_data()
    
    if not data["members"]:
        post_ephemeral_message(
            client,
            channel_id,
            user_id,
            text="📊 **No statistics available yet.**"
        )
        return
    
    lines = ["*📊 Leadership Statistics*\n"]
    
    sorted_members = sorted(
        data["members"].items(),
        key=lambda x: x[1].get("total_led", 0),
        reverse=True
    )
    
    for uid, member in sorted_members[:20]:
        name = member.get("name", "Unknown")
        email = member.get("email", "N/A")
        nominated = member.get("total_nominated", 0)
        accepted = member.get("total_accepted", 0)
        declined = member.get("total_declined", 0)
        led = member.get("total_led", 0)
        is_observer = member.get("is_observer", False)
        
        status = "👀 Observer" if is_observer else "✅ Eligible"
        acceptance_rate = f"{(accepted/nominated*100):.0f}%" if nominated > 0 else "N/A"
        
        lines.append(f"*{name}* - {status}")
        lines.append(f"`{email}`")
        lines.append(f"Nominated: {nominated} | Accepted: {accepted} | Declined: {declined} | Led: {led} | Rate: {acceptance_rate}")
        lines.append("")
    
    if len(sorted_members) > 20:
        lines.append(f"_Showing top 20 of {len(sorted_members)} members_")
    
    post_ephemeral_message(
        client,
        channel_id,
        user_id,
        text="\n".join(lines)
    )
# ============================================================================
# OBSERVER MANAGEMENT ACTION HANDLERS
# ============================================================================

@app.action("add_observers_select")
def handle_add_observers_select(ack, body, action):
    """Handle user selection for adding observers."""
    ack()
    user_id = body["user"]["id"]
    selected_users = action.get("selected_users", [])
    selected_users_cache[f"add_{user_id}"] = selected_users

@app.action("remove_observers_select")
def handle_remove_observers_select(ack, body, action):
    """Handle user selection for removing observers."""
    ack()
    user_id = body["user"]["id"]
    selected_users = action.get("selected_users", [])
    selected_users_cache[f"remove_{user_id}"] = selected_users

@app.action("confirm_add_observers")
def handle_confirm_add_observers(ack, body, client):
    """Confirm and process adding observers - EPHEMERAL response + PUBLIC audit log."""
    ack()
    
    user_id = body["user"]["id"]
    channel_id = body["channel"]["id"]
    
    selected_users = selected_users_cache.get(f"add_{user_id}", [])
    
    if not selected_users:
        post_ephemeral_message(
            client,
            channel_id,
            user_id,
            text="⚠️ No members selected. Please try again."
        )
        return
    
    data = load_data()
    
    if "observers" not in data:
        data["observers"] = []
    
    added_names = []
    already_observers = []
    
    try:
        actor_info = client.users_info(user=user_id)
        actor_name = actor_info["user"].get("real_name", "Unknown")
    except:
        actor_name = "Unknown"
    
    for selected_user_id in selected_users:
        if selected_user_id in data["members"]:
            member = data["members"][selected_user_id]
            member_name = member.get("name", "Unknown")
            
            if selected_user_id in data["observers"]:
                already_observers.append(member_name)
            else:
                data["observers"].append(selected_user_id)
                data["members"][selected_user_id]["is_observer"] = True
                added_names.append(member_name)
    
    save_data(data)
    
    if f"add_{user_id}" in selected_users_cache:
        del selected_users_cache[f"add_{user_id}"]
    
    response_lines = []
    
    if added_names:
        response_lines.append(f"✅ **{len(added_names)} observer(s) added:**")
        for name in added_names:
            response_lines.append(f"• {name}")
        response_lines.append("")
    
    if already_observers:
        response_lines.append(f"⚠️ **{len(already_observers)} already observer(s):**")
        for name in already_observers:
            response_lines.append(f"• {name}")
    
    if not response_lines:
        response_lines.append("⚠️ No valid members selected.")
    
    post_ephemeral_message(
        client,
        channel_id,
        user_id,
        text="\n".join(response_lines)
    )
    
    if added_names:
        post_audit_log(client, channel_id, actor_name, "added observer", added_names)

@app.action("confirm_remove_observers")
def handle_confirm_remove_observers(ack, body, client):
    """Confirm and process removing observers - EPHEMERAL response + PUBLIC audit log."""
    ack()
    
    user_id = body["user"]["id"]
    channel_id = body["channel"]["id"]
    
    selected_users = selected_users_cache.get(f"remove_{user_id}", [])
    
    if not selected_users:
        post_ephemeral_message(
            client,
            channel_id,
            user_id,
            text="⚠️ No members selected. Please try again."
        )
        return
    
    data = load_data()
    
    if "observers" not in data:
        data["observers"] = []
    
    removed_names = []
    not_observers = []
    
    try:
        actor_info = client.users_info(user=user_id)
        actor_name = actor_info["user"].get("real_name", "Unknown")
    except:
        actor_name = "Unknown"
    
    for selected_user_id in selected_users:
        if selected_user_id in data["members"]:
            member = data["members"][selected_user_id]
            member_name = member.get("name", "Unknown")
            
            if selected_user_id in data["observers"]:
                data["observers"].remove(selected_user_id)
                data["members"][selected_user_id]["is_observer"] = False
                removed_names.append(member_name)
            else:
                not_observers.append(member_name)
    
    save_data(data)
    
    if f"remove_{user_id}" in selected_users_cache:
        del selected_users_cache[f"remove_{user_id}"]
    
    response_lines = []
    
    if removed_names:
        response_lines.append(f"✅ **{len(removed_names)} observer(s) removed:**")
        for name in removed_names:
            response_lines.append(f"• {name}")
        response_lines.append("")
    
    if not_observers:
        response_lines.append(f"⚠️ **{len(not_observers)} not observer(s):**")
        for name in not_observers:
            response_lines.append(f"• {name}")
    
    if not response_lines:
        response_lines.append("⚠️ No valid members selected.")
    
    post_ephemeral_message(
        client,
        channel_id,
        user_id,
        text="\n".join(response_lines)
    )
    
    if removed_names:
        post_audit_log(client, channel_id, actor_name, "removed observer", removed_names)

@app.action("cancel_observer_action")
def handle_cancel_observer_action(ack, body, client):
    """Handle cancel button for observer actions - EPHEMERAL."""
    ack()
    
    user_id = body["user"]["id"]
    channel_id = body["channel"]["id"]
    
    if f"add_{user_id}" in selected_users_cache:
        del selected_users_cache[f"add_{user_id}"]
    if f"remove_{user_id}" in selected_users_cache:
        del selected_users_cache[f"remove_{user_id}"]
    
    post_ephemeral_message(
        client,
        channel_id,
        user_id,
        text="❌ _Action cancelled._"
    )

@app.action("member_quick_action")
def handle_member_quick_action(ack, body, client, action):
    """Handle quick action from overflow menu - EPHEMERAL response + PUBLIC audit log."""
    ack()
    
    value = action["selected_option"]["value"]
    user_id = body["user"]["id"]
    channel_id = body["channel"]["id"]
    
    data = load_data()
    
    try:
        actor_info = client.users_info(user=user_id)
        actor_name = actor_info["user"].get("real_name", "Unknown")
    except:
        actor_name = "Unknown"
    
    if value.startswith("make_observer_"):
        target_user_id = value.replace("make_observer_", "")
        
        if target_user_id in data["members"]:
            if "observers" not in data:
                data["observers"] = []
            
            if target_user_id not in data["observers"]:
                data["observers"].append(target_user_id)
                data["members"][target_user_id]["is_observer"] = True
                save_data(data)
                
                target_name = data["members"][target_user_id].get("name", "Unknown")
                
                post_ephemeral_message(
                    client,
                    channel_id,
                    user_id,
                    text=f"✅ {target_name} is now an observer."
                )
                
                post_audit_log(client, channel_id, actor_name, "added observer", [target_name])
            else:
                target_name = data["members"][target_user_id].get("name", "Unknown")
                post_ephemeral_message(
                    client,
                    channel_id,
                    user_id,
                    text=f"⚠️ {target_name} is already an observer."
                )
    
    elif value.startswith("remove_observer_"):
        target_user_id = value.replace("remove_observer_", "")
        
        if target_user_id in data["members"]:
            if "observers" not in data:
                data["observers"] = []
            
            if target_user_id in data["observers"]:
                data["observers"].remove(target_user_id)
                data["members"][target_user_id]["is_observer"] = False
                save_data(data)
                
                target_name = data["members"][target_user_id].get("name", "Unknown")
                
                post_ephemeral_message(
                    client,
                    channel_id,
                    user_id,
                    text=f"✅ {target_name} is now eligible for selection."
                )
                
                post_audit_log(client, channel_id, actor_name, "removed observer", [target_name])
            else:
                target_name = data["members"][target_user_id].get("name", "Unknown")
                post_ephemeral_message(
                    client,
                    channel_id,
                    user_id,
                    text=f"⚠️ {target_name} is not an observer."
                )
# ============================================================================
# SLASH COMMAND HANDLER
# ============================================================================

@app.command("/meeting-leader")
def handle_meeting_leader(ack, command, say, client):
    """Main slash command handler for /meeting-leader."""
    ack()
    
    text = command.get("text", "").strip()
    channel_id = command["channel_id"]
    user_id = command["user"]["id"]
    
    logger.info(f"Command: /meeting-leader {text}")
    
    data = load_data()
    
    parts = text.split()
    action = parts[0].lower() if parts else "help"
    
    if action == "select":
        sync_channel_members(client, channel_id, data, verbose=False)
        
        today = datetime.now()
        day_name = today.strftime("%A")
        
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
        
        if day_name not in ["Tuesday", "Thursday"]:
            say(f"🎲 **Random selection result:** *<@{selected_id}>*\n\n"
                f"**Name:** {member_name}\n"
                f"**Email:** {member_email}\n\n"
                f"⚠️ Today is {day_name} - no sync meeting scheduled.")
            return
        
        create_nomination(client, channel_id, selected_id, data, is_auto=False)
    
    elif action == "sync":
        post_ephemeral_message(
            client,
            channel_id,
            user_id,
            text="🔄 Syncing members...",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "🔄 *Syncing members...*\n\nPlease wait..."
                    }
                }
            ]
        )
        
        sync_result = sync_channel_members(client, channel_id, data, verbose=False)
        
        result_lines = []
        
        if sync_result["new_count"] > 0 or sync_result["removed_count"] > 0:
            result_lines.append("✅ *Sync Complete!*\n")
            
            if sync_result["new_count"] > 0:
                result_lines.append(f"**Added:** {sync_result['new_count']} member(s)")
                for name in sync_result["new_members"][:5]:
                    result_lines.append(f"  • {name}")
                if len(sync_result["new_members"]) > 5:
                    result_lines.append(f"  • ... and {len(sync_result['new_members']) - 5} more")
                result_lines.append("")
            
            if sync_result["removed_count"] > 0:
                result_lines.append(f"**Removed:** {sync_result['removed_count']} member(s)")
                for name in sync_result["removed_members"][:5]:
                    result_lines.append(f"  • {name}")
                if len(sync_result["removed_members"]) > 5:
                    result_lines.append(f"  • ... and {len(sync_result['removed_members']) - 5} more")
                result_lines.append("")
            
            result_lines.append(f"**Total members:** {sync_result['total_count']}")
            
            try:
                client.chat_postMessage(
                    channel=channel_id,
                    text="\n".join(result_lines)
                )
            except Exception as e:
                logger.error(f"Error posting sync result: {e}")
        else:
            post_ephemeral_message(
                client,
                channel_id,
                user_id,
                text="✅ *Sync Complete!*\n\nNo changes detected. All members are up to date.\n\n**Total members:** " + str(sync_result['total_count'])
            )
    
    elif action == "list":
        # Trigger same as panel button
        handle_panel_list(ack, {"channel": {"id": channel_id}, "user": {"id": user_id}}, client)
    
    elif action == "history":
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
    
    elif action == "stats" or action == "statistics":
        # Trigger same as panel button
        handle_panel_statistics(ack, {"channel": {"id": channel_id}, "user": {"id": user_id}}, client)
    
    elif action == "add-observer":
        handle_panel_add_observers(ack, {"channel": {"id": channel_id}, "user": {"id": user_id}}, client)
    
    elif action == "remove-observer":
        handle_panel_remove_observers(ack, {"channel": {"id": channel_id}, "user": {"id": user_id}}, client)
    
    elif action == "list-observers":
        handle_panel_list_observers(ack, {"channel": {"id": channel_id}, "user": {"id": user_id}}, client)
    
    elif action == "refresh-panel":
        refresh_control_panel(client, channel_id, data)
        say("✅ Control panel refreshed!")
    
    else:
        say("""*🤖 Meeting Leader Bot - Control Panel Edition*

*🎮 Interactive Control Panel:*
A persistent control panel with visual buttons is available in the channel for easy access to all features.

*🔘 Control Panel Buttons:*
• *🔄 Sync Members* - Update member list from channel (public results if changes)
• *📋 List Members* - View all members with quick actions (private)
• *➕ Add Observers* - Select members to exclude from rotation (private)
• *➖ Remove Observers* - Re-enable observers for rotation (private)
• *👀 List Observers* - View all excluded members (private)
• *📈 Statistics* - View meeting leadership stats (private)

*⌨️ Slash Commands (Alternative):*
• `/meeting-leader select` - Manually pick random leader
• `/meeting-leader sync` - Sync channel members
• `/meeting-leader list` - Show members (private)
• `/meeting-leader history` - Show last 10 leaders
• `/meeting-leader stats` - Show statistics (private)
• `/meeting-leader add-observer` - Add observers (private)
• `/meeting-leader remove-observer` - Remove observers (private)
• `/meeting-leader list-observers` - List observers (private)
• `/meeting-leader refresh-panel` - Refresh control panel

*✨ Interactive Features:*
• **Visual Button Controls** - Click buttons instead of typing commands
• **Ephemeral Responses** - Most interactions are private (only you see them)
• **User Multi-Select Menus** - Visual selection with profile pictures
• **Quick Action Buttons** - One-click observer management from member list
• **Auto Re-nomination** - Automatic selection when person declines
• **Public Audit Logs** - Only final changes logged publicly (format: "📋 _Name action Person_")
• **Silent Operation** - Minimal channel noise
• **Persistent Control Panel** - Always visible in channel

*⚙️ Automated Behavior:*
• **Tuesday & Thursday at 10:00 AM** - Automatic leader nomination
• **Auto re-nomination** - If person declines, bot automatically selects another
• **Holiday detection** - No nominations on public holidays
• **Vacation status check** - Skips members with OOO/vacation status
• **Silent sync** - Automatic member sync with minimal logging

*✅ Eligibility Rules:*
• Members can lead maximum **once per week** (Monday-Sunday)
• **Observers are excluded** from automatic selection
• **Priority weighting** - Members who never led or haven't led recently are prioritized
• **Availability check** - Bot checks Slack status before nomination

*📋 Audit Trail:*
• All observer changes logged publicly (only when changes happen)
• Format: "📋 _PersonName action PersonAffected_"
• Example: "📋 _Jon Doe added observer Lilu Ojovan_"
• No user tags in audit logs (plain names only)
• Sync results only public if members added/removed

*🔇 Privacy & Noise Reduction:*
• **Private responses** - Statistics, lists, and observer menus visible only to you
• **Public logs only for changes** - Channel sees only: nominations, accepts/declines, audit logs
• **Clean channel** - No spam from individual queries
• **Minimal logging** - Silent operation by default

_Use the control panel buttons above or type commands for quick access!_""")
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
# SCHEDULER - PANEL POST & NOMINALIZATION (2 SEPARATE JOBS)
# ============================================================================

def post_control_panel_scheduled(client):
    """
    Post control panel at 09:23 AM RO time (06:23 UTC).
    Runs independently from nominalization.
    """
    if not MEETING_CHANNEL_ID:
        logger.error("MEETING_CHANNEL_ID not set")
        return

    data = load_data()
    today = datetime.now()
    day_name = today.strftime("%A")

    if day_name not in ["Tuesday", "Thursday"]:
        return

    if is_holiday(today):
        logger.info("Public holiday detected - panel post skipped")
        try:
            client.chat_postMessage(
                channel=MEETING_CHANNEL_ID,
                text="🏖️ No meeting today - it's a public holiday! Enjoy your day off!"
            )
        except Exception as e:
            logger.error(f"Error posting holiday message: {e}")
        return

    logger.info(f"[PANEL POST] Posting control panel: {today.strftime('%Y-%m-%d %H:%M:%S')} {day_name}")

    try:
        refresh_control_panel(client, MEETING_CHANNEL_ID, data)
        logger.info("✅ Control panel posted successfully")
    except Exception as e:
        logger.error(f"❌ Failed to post control panel: {e}")


def run_nominalization_scheduled(client):
    """
    Run nomination process at 09:24 AM RO time (06:24 UTC).
    Runs 1 minute AFTER panel post.
    """
    if not MEETING_CHANNEL_ID:
        logger.error("MEETING_CHANNEL_ID not set")
        return

    data = load_data()
    today = datetime.now()
    day_name = today.strftime("%A")

    if day_name not in ["Tuesday", "Thursday"]:
        return

    if is_holiday(today):
        logger.info("Public holiday detected - nomination skipped")
        return

    logger.info(f"[NOMINATION] Starting nomination process: {today.strftime('%Y-%m-%d %H:%M:%S')} {day_name}")

    try:
        sync_channel_members(client, MEETING_CHANNEL_ID, data, verbose=False)
        selected_id = select_random_leader(data, today, client, check_status=True)

        if not selected_id:
            logger.warning("❌ No eligible leaders available")
            try:
                client.chat_postMessage(
                    channel=MEETING_CHANNEL_ID,
                    text="🚫 *Meeting cancelled* - No available leaders today.\n\n"
                         "Possible reasons:\n"
                         "• All members already led this week\n"
                         "• All members are marked as observers\n\n"
                         "Use the control panel to manually select someone."
                )
            except Exception as e:
                logger.error(f"Error posting cancellation: {e}")
            save_data(data)
            return

        member = data["members"][selected_id]
        is_available, status_message = check_user_status(client, selected_id)

        logger.info(f"Selected: {member['name']} - Available: {is_available}")

        if not is_available:
            logger.warning(f"⚠️ Selected person unavailable: {status_message}")
            try:
                client.chat_postMessage(
                    channel=MEETING_CHANNEL_ID,
                    text=f"⚠️ *Nominated person is unavailable*\n\n"
                         f"*{member['name']}* was randomly selected but is currently unavailable:\n"
                         f"• Status: {status_message}\n\n"
                         f"Please nominate another team member manually using the control panel or:\n"
                         f"`/meeting-leader select`"
                )
            except Exception as e:
                logger.error(f"Error posting unavailable message: {e}")
            save_data(data)
            return

        create_nomination(client, MEETING_CHANNEL_ID, selected_id, data, is_auto=True)
        logger.info(f"✅ Nomination created for {member['name']}")

    except Exception as e:
        logger.error(f"❌ Nomination process error: {e}")
        save_data(data)


def run_scheduler(client):
    """
    Scheduler with two independent jobs for control panel and nominalization.

    Timeline:
    - 09:23 AM RO (06:23 UTC): Post control panel
    - 09:24 AM RO (06:24 UTC): Run nominalization (1 minute later)
    """
    schedule.every().tuesday.at("06:23").do(lambda: post_control_panel_scheduled(client))
    schedule.every().thursday.at("06:23").do(lambda: post_control_panel_scheduled(client))

    schedule.every().tuesday.at("06:24").do(lambda: run_nominalization_scheduled(client))
    schedule.every().thursday.at("06:24").do(lambda: run_nominalization_scheduled(client))

    logger.info("Scheduler started:")
    logger.info("  • Control Panel: Tuesday & Thursday @ 09:23 AM RO (06:23 UTC)")
    logger.info("  • Nominalization: Tuesday & Thursday @ 09:24 AM RO (06:24 UTC)")

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
    logger.info("=== MEETING LEADER BOT - CONTROL PANEL EDITION STARTING ===")
    
    if not os.environ.get("SLACK_BOT_TOKEN"):
        logger.error("SLACK_BOT_TOKEN not set!")
        exit(1)
    
    if not os.environ.get("SLACK_APP_TOKEN"):
        logger.error("SLACK_APP_TOKEN not set!")
        exit(1)
    
    if not MEETING_CHANNEL_ID:
        logger.warning("MEETING_CHANNEL_ID not set - automated nominations and control panel disabled")
    
    logger.info("Environment variables validated")
    
    slack_client = app.client
    
    # Post control panel on startup
    if MEETING_CHANNEL_ID:
        logger.info("Posting control panel to channel...")
        data = load_data()
        control_panel_ts = post_control_panel(slack_client, MEETING_CHANNEL_ID)
        if control_panel_ts:
            data["control_panel_ts"] = control_panel_ts
            save_data(data)
            logger.info("Control panel posted successfully")
        else:
            logger.warning("Failed to post control panel")
    
    # Start scheduler thread
    scheduler_thread = threading.Thread(
        target=run_scheduler,
        args=(slack_client,),
        daemon=True,
        name="SchedulerThread"
    )
    scheduler_thread.start()
    logger.info("Scheduler thread started")
    
    logger.info("Bot is now running with control panel and ephemeral features")
    
    try:
        handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
        handler.start()
    except KeyboardInterrupt:
        logger.info("Bot stopped (Ctrl+C)")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        exit(1)