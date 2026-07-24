# ============================================================================
# RECOMMENDED FIX: Option 2 - Two Separate Scheduler Jobs
# ============================================================================
# Replace the run_scheduler() function and add two new functions
# This decouples panel posting from nominalization with 1-minute delay

def post_control_panel_scheduled(client):
    """
    Post control panel at 09:23 AM RO time (06:23 UTC).
    Runs independently from nominalization.

    Called by scheduler - no blocking sleep.
    """
    if not MEETING_CHANNEL_ID:
        logger.error("MEETING_CHANNEL_ID not set")
        return

    data = load_data()
    today = datetime.now()
    day_name = today.strftime("%A")

    # Only on meeting days
    if day_name not in ["Tuesday", "Thursday"]:
        logger.info(f"Panel post skipped - today is {day_name}")
        return

    # Check for public holidays
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

    Called by scheduler - completely independent from panel posting.
    """
    if not MEETING_CHANNEL_ID:
        logger.error("MEETING_CHANNEL_ID not set")
        return

    data = load_data()
    today = datetime.now()
    day_name = today.strftime("%A")

    # Only on meeting days
    if day_name not in ["Tuesday", "Thursday"]:
        logger.info(f"Nomination skipped - today is {day_name}")
        return

    # Check for public holidays
    if is_holiday(today):
        logger.info("Public holiday detected - nomination skipped")
        return

    logger.info(f"[NOMINATION] Starting nomination process: {today.strftime('%Y-%m-%d %H:%M:%S')} {day_name}")

    try:
        # Sync members
        sync_result = sync_channel_members(client, MEETING_CHANNEL_ID, data, verbose=False)
        logger.info(f"Sync complete: {sync_result['total_count']} members")

        # Select leader
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

        # Check if selected person is available
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

        # Create and send nomination
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

    Both run on Tuesday and Thursday.
    Non-blocking - jobs run concurrently without sleep delays.
    """

    # Schedule panel posting (adjust times to match your timezone)
    # If your system shows 09:23 AM RO = UTC-time, change "06:23" accordingly
    schedule.every().tuesday.at("06:23").do(lambda: post_control_panel_scheduled(client))
    schedule.every().thursday.at("06:23").do(lambda: post_control_panel_scheduled(client))

    # Schedule nominalization 1 minute after panel
    schedule.every().tuesday.at("06:24").do(lambda: run_nominalization_scheduled(client))
    schedule.every().thursday.at("06:24").do(lambda: run_nominalization_scheduled(client))

    logger.info("Scheduler started:")
    logger.info("  • Control Panel: Tuesday & Thursday @ 09:23 AM RO (06:23 UTC)")
    logger.info("  • Nominalization: Tuesday & Thursday @ 09:24 AM RO (06:24 UTC)")
    logger.info("  • Both jobs run independently (non-blocking)")

    # Main scheduler loop
    while True:
        try:
            schedule.run_pending()
            time.sleep(60)  # Check every minute if jobs need to run
        except Exception as e:
            logger.error(f"Scheduler error: {e}")
            time.sleep(60)

