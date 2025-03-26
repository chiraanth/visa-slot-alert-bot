import requests
from bs4 import BeautifulSoup
import asyncio
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, CallbackContext
import os
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# User Preferences (year_filter will be a list of strings, or None)
user_preferences = {
    "visa_type": None,
    "consulate_city": None,
    "consulate_type": None,
    "interval": None,       # In seconds
    "year_filter": None,    # e.g. ["2025"] or ["2025", "2026"]
}
no_slot_alert_sent = False  # For spam prevention

# Menu options
VISA_TYPES = ["B1", "B2", "B1/B2", "F-1", "H-1B", "J-1", "L-1"]
CITIES = ["ALL","MUMBAI", "HYDERABAD", "CHENNAI", "NEW DELHI", "KOLKATA"]
CONSULATE_TYPES = ["CONSULAR", "VAC"]
# Year Filter options
YEAR_OPTIONS = ["No Filter", "2025", "2026"]
INTERVALS = {"5 min": 300, "10 min": 600, "30 min": 1800, "60 min": 3600}

alert_task = None

def visa_matches_site(user_pref: str, site_visa: str) -> bool:
    if user_pref == "B1":
        return site_visa in ["B1", "B1/B2"]
    elif user_pref == "B2":
        return site_visa in ["B2", "B1/B2"]
    elif user_pref == "B1/B2":
        return site_visa == "B1/B2"
    if user_pref == "F-1":
        return site_visa in ["F1", "F1/F2", "F-1"]
    return user_pref == site_visa

def year_matches(date_str: str, year_filter):
    if not year_filter or date_str == "N/A":
        return True
    return any(year in date_str for year in year_filter)

async def start(update: Update, context: CallbackContext):
    await update.message.reply_text("Welcome to Visa Slot Bot! Use the menu to configure alerts.")

async def set_visa(update: Update, context: CallbackContext):
    await send_visa_menu(update)

async def set_consulate(update: Update, context: CallbackContext):
    await send_consulate_city_menu(update)

async def set_interval(update: Update, context: CallbackContext):
    await send_interval_menu(update)

# --------------------------
# Menu Functions
# --------------------------

async def send_visa_menu(update):
    keyboard = [[InlineKeyboardButton(v, callback_data=f"visa_{v}")] for v in VISA_TYPES]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Select a Visa Type:", reply_markup=reply_markup)

async def send_consulate_city_menu(update):
    keyboard = [[InlineKeyboardButton(city, callback_data=f"consulatecity_{city}")] for city in CITIES]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Select a Consulate City:", reply_markup=reply_markup)

async def send_consulate_type_menu(update):
    keyboard = [[InlineKeyboardButton(ct, callback_data=f"consulattype_{ct}")] for ct in CONSULATE_TYPES]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Select a Consulate Type:", reply_markup=reply_markup)

async def send_year_filter_menu(update):
    keyboard = [[InlineKeyboardButton(option, callback_data=f"yearfilter_{option}")] for option in YEAR_OPTIONS]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Select Year Filter:", reply_markup=reply_markup)

async def send_interval_menu(update):
    keyboard = [[InlineKeyboardButton(i, callback_data=f"interval_{i}")] for i in INTERVALS.keys()]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Select Update Interval:", reply_markup=reply_markup)

# --------------------------
# Handling Selections
# --------------------------

async def handle_selection(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("visa_"):
        user_preferences["visa_type"] = data.replace("visa_", "")
        await query.message.reply_text(f"✅ Visa Type set to: {user_preferences['visa_type']}")
        await send_consulate_city_menu(query)

    elif data.startswith("consulatecity_"):
        user_preferences["consulate_city"] = data.replace("consulatecity_", "")
        await query.message.reply_text(f"✅ Consulate City set to: {user_preferences['consulate_city']}")
        await send_consulate_type_menu(query)

    elif data.startswith("consulattype_"):
        user_preferences["consulate_type"] = data.replace("consulattype_", "")
        await query.message.reply_text(f"✅ Consulate Type set to: {user_preferences['consulate_type']}")
        # Next, show the Year Filter menu
        await send_year_filter_menu(query)

    elif data.startswith("yearfilter_"):
        selection = data.replace("yearfilter_", "")
        if selection == "No Filter":
            user_preferences["year_filter"] = None
        else:
            user_preferences["year_filter"] = [selection]
        await query.message.reply_text(f"✅ Year Filter set to: {selection}")
        # Next, show the Interval menu
        await send_interval_menu(query)

    elif data.startswith("interval_"):
        user_preferences["interval"] = INTERVALS[data.replace("interval_", "")]
        await query.message.reply_text(f"✅ Update Interval set to: {data.replace('interval_', '')}")
        full_consulate = f"{user_preferences['consulate_city']} {user_preferences['consulate_type']}"
        summary = (
            "🎯 All Set! Here are your filter settings:\n"
            f"• Visa Type: {user_preferences['visa_type']}\n"
            f"• Consulate: {full_consulate}\n"
            f"• Year Filter: {user_preferences.get('year_filter', 'None')}\n"
            f"• Interval: {user_preferences['interval'] // 60} min"
        )
        await query.message.reply_text(summary)
        keyboard = [[InlineKeyboardButton("🚀 Start Alerts", callback_data="start_alerts")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text("Click below to start receiving alerts:", reply_markup=reply_markup)

# --------------------------
# Alert Handlers
# --------------------------

async def start_alerts_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    if query is not None:
        await query.answer()
        message = query.message
    else:
        message = update.effective_message

    if (not user_preferences["visa_type"] or
        not user_preferences["consulate_city"] or
        not user_preferences["consulate_type"] or
        not user_preferences["interval"]):
        await message.reply_text("⚠️ Please set Visa Type, Consulate (city & type), and Interval first!")
        return

    global alert_task
    if alert_task and not alert_task.done():
        await message.reply_text("⚠️ Alerts are already running!")
        return

    full_consulate = f"{user_preferences['consulate_city']} {user_preferences['consulate_type']}"
    summary = (
        f"🔔 Alerts Started!\n\n"
        f"✅ Visa Type: {user_preferences['visa_type']}\n"
        f"✅ Consulate: {full_consulate}\n"
        f"✅ Year Filter: {user_preferences.get('year_filter', 'None')}\n"
        f"✅ Interval: {user_preferences['interval'] // 60} min\n\n"
        f"⏳ Checking for slots now..."
    )
    await message.reply_text(summary, parse_mode="Markdown")
    alert_task = asyncio.create_task(run_alert_loop())

async def stop_alerts(update: Update, context: CallbackContext):
    global alert_task
    if alert_task and not alert_task.done():
        alert_task.cancel()
        alert_task = None
        await update.message.reply_text("🛑 Alerts stopped successfully.")
    else:
        await update.message.reply_text("⚠️ No alerts are running.")

# --------------------------
# Data Fetching & Alert Loop
# --------------------------

def get_all_visa_slots():
    response = requests.get("https://visaslots.info/", headers={"User-Agent": "Mozilla/5.0"})
    if response.status_code != 200:
        print(f"Failed to fetch data, status code: {response.status_code}")
        return []
    soup = BeautifulSoup(response.text, "html.parser")
    tables = soup.find_all("table")
    all_rows = []
    for table in tables:
        rows = table.find_all("tr")[1:]
        for row in rows:
            cols = row.find_all("td")
            location = cols[0].text.strip()
            site_visa = cols[1].text.strip()
            last_updated = cols[2].text.strip()
            earliest_date = cols[3].text.strip()
            slots_available = cols[4].text.strip()
            all_rows.append({
                "Location": location,
                "Visa Type": site_visa,
                "Last Updated": last_updated,
                "Earliest Date": earliest_date,
                "Slots Available": slots_available
            })
    return all_rows

async def send_telegram_alert(slot):
    bot = Bot(token=BOT_TOKEN)
    message = (
        f"🚨 *Visa Slot Alert!* 🚨\n\n"
        f"📍 *Location:* {slot['Location']}\n"
        f"📌 *Visa Type:* {slot['Visa Type']}\n"
        f"⏳ *Earliest Available Date:* {slot['Earliest Date']}\n"
        f"🟢 *Slots Available:* {slot['Slots Available']}\n"
        f"⏰ *Last Updated:* {slot['Last Updated']}\n\n"
        f"Check Now: [VisaSlots](https://visaslots.info/)"
    )
    await bot.send_message(chat_id=CHAT_ID, text=message, parse_mode="Markdown")

async def run_alert_loop():
    global no_slot_alert_sent
    bot = Bot(token=BOT_TOKEN)
    while True:
        all_slots = get_all_visa_slots()
        matching_preference = []  # rows for the chosen consulate type (across all locations if "ALL" is selected)
        other_locations = []      # rows that match the visa type but are not considered in the preferred set

        # If user selected "ALL", our preferred check is based solely on consulate type.
        for row in all_slots:
            if visa_matches_site(user_preferences["visa_type"], row["Visa Type"]):
                if user_preferences["consulate_city"] == "ALL":
                    # Check if the row's location ends with the chosen consulate type (e.g., "VAC" or "CONSULAR")
                    if row["Location"].strip().endswith(user_preferences["consulate_type"]):
                        matching_preference.append(row)
                    else:
                        other_locations.append(row)
                else:
                    # If a specific city is selected, use the full string match.
                    preferred_location = f"{user_preferences['consulate_city']} {user_preferences['consulate_type']}"
                    if row["Location"] == preferred_location:
                        matching_preference.append(row)
                    else:
                        other_locations.append(row)

        # Filter open slots with any additional filters (e.g. year)
        matching_open_slots = [
            r for r in matching_preference
            if r["Slots Available"] != "0"
               and r["Earliest Date"] != "N/A"
               and year_matches(r["Earliest Date"], user_preferences.get("year_filter"))
        ]

        if matching_open_slots:
            no_slot_alert_sent = False
            for slot in matching_open_slots:
                await send_telegram_alert(slot)
        else:
            other_open_slots = [
                r for r in other_locations
                if r["Slots Available"] != "0"
                   and r["Earliest Date"] != "N/A"
                   and year_matches(r["Earliest Date"], user_preferences.get("year_filter"))
            ]
            if other_open_slots:
                summary_lines = [
                    "No open slots found at your preferred consulate type!\n\n"
                    "Other open locations for your visa type:"
                ]
                for s in other_open_slots:
                    line = (
                        f"• {s['Location']} | "
                        f"Earliest: {s['Earliest Date']} | "
                        f"Slots: {s['Slots Available']} | "
                        f"Last Updated: {s['Last Updated']}"
                    )
                    summary_lines.append(line)
                summary_text = "\n".join(summary_lines)
                if not no_slot_alert_sent:
                    await bot.send_message(chat_id=CHAT_ID, text=summary_text)
                    no_slot_alert_sent = True
            else:
                if not no_slot_alert_sent:
                    await bot.send_message(
                        chat_id=CHAT_ID,
                        text="No open slots found at this time. Checking again soon..."
                    )
                    no_slot_alert_sent = True

        await asyncio.sleep(user_preferences["interval"])


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("set_visa", set_visa))
    app.add_handler(CommandHandler("set_consulate", set_consulate))
    app.add_handler(CommandHandler("set_interval", set_interval))
    app.add_handler(CommandHandler("start_alerts", start_alerts_handler))
    app.add_handler(CommandHandler("stop", stop_alerts))

    app.add_handler(CallbackQueryHandler(handle_selection, pattern="^(visa_|consulatecity_|consulattype_|yearfilter_|interval_)"))
    app.add_handler(CallbackQueryHandler(start_alerts_handler, pattern="^start_alerts$"))

    print("🤖 Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()

