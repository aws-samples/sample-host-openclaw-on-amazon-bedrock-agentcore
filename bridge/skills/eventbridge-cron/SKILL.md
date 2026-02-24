---
name: eventbridge-cron
description: Schedule recurring tasks using Amazon EventBridge Scheduler. Create, list, update, and delete cron schedules that fire at specified times and deliver results to the user's chat channel. Use when the user asks to schedule reminders, recurring tasks, or periodic actions.
allowed-tools: Bash(node:*)
---

# EventBridge Cron Scheduler

Schedule recurring tasks using Amazon EventBridge. Each user's schedules are isolated — no cross-user access.

## Important

**Always use the user_id from the system prompt** when calling these tools.
Never hardcode or guess a user_id. The system provides it automatically.

## Usage

### create_schedule

Create a new cron schedule. The schedule fires at the specified time and sends the message to the agent for processing, then delivers the response to the user's chat.

```bash
node {baseDir}/create.js <user_id> <cron_expression> <timezone> <message> [channel] [channel_target] [schedule_name]
```

- `user_id` (required): The user's unique identifier (e.g., `telegram_12345`)
- `cron_expression` (required): EventBridge cron or rate expression (e.g., `cron(0 9 * * ? *)` for daily at 9am, `rate(1 hour)` for hourly)
- `timezone` (required): IANA timezone (e.g., `Asia/Tokyo`, `America/New_York`, `Europe/London`, `UTC`)
- `message` (required): The task message the agent should execute on each trigger
- `channel` (optional): Channel type — defaults to extracting from user_id (e.g., `telegram`)
- `channel_target` (optional): Channel destination — defaults to extracting from user_id (e.g., `12345`)
- `schedule_name` (optional): Human-readable name for the schedule

**Cron expression format**: `cron(minutes hours day-of-month month day-of-week year)`
- Minutes: 0-59
- Hours: 0-23
- Day-of-month: 1-31
- Month: 1-12 or JAN-DEC
- Day-of-week: 1-7 or SUN-SAT (1=Sunday)
- Year: * or specific year

**Examples**:
- `cron(0 9 * * ? *)` — Every day at 9:00 AM
- `cron(30 14 ? * MON-FRI *)` — Weekdays at 2:30 PM
- `cron(0 */2 * * ? *)` — Every 2 hours
- `rate(1 hour)` — Every hour
- `rate(5 minutes)` — Every 5 minutes

### list_schedules

List all cron schedules for the user.

```bash
node {baseDir}/list.js <user_id>
```

- `user_id` (required): The user's unique identifier

### update_schedule

Update an existing schedule (expression, timezone, message, enable/disable).

```bash
node {baseDir}/update.js <user_id> <schedule_id> [--expression "cron(...)"] [--timezone "Asia/Tokyo"] [--message "new message"] [--enable] [--disable] [--name "new name"]
```

- `user_id` (required): The user's unique identifier
- `schedule_id` (required): The schedule ID to update (from list_schedules output)
- `--expression`: New cron/rate expression
- `--timezone`: New IANA timezone
- `--message`: New task message
- `--enable`: Enable a disabled schedule
- `--disable`: Disable the schedule (keeps it but stops firing)
- `--name`: New human-readable name

### delete_schedule

Delete a cron schedule permanently.

```bash
node {baseDir}/delete.js <user_id> <schedule_id>
```

- `user_id` (required): The user's unique identifier
- `schedule_id` (required): The schedule ID to delete

## From Agent Chat

- "Remind me every day at 9am Tokyo time to check email" -> create_schedule with appropriate cron expression
- "What schedules do I have?" -> list_schedules
- "Change my morning reminder to 8am" -> update_schedule with new expression
- "Stop my daily reminder" -> update_schedule with --disable, or delete_schedule
- "Resume my daily reminder" -> update_schedule with --enable
- "Delete all my schedules" -> list_schedules then delete_schedule for each
- "Every weekday at 5pm remind me to log my hours" -> create_schedule with MON-FRI expression

## Security Notes

- Schedules are isolated per user via naming: `openclaw-{userId}-{scheduleId}`
- user_id uses underscores (e.g., `telegram_12345`) — colons are replaced
- Never use `default_user` as user_id — scripts reject it with an error
- Schedule expressions are validated before creation
- Timezones are validated against IANA timezone database
