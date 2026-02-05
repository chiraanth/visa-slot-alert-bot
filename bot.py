"""
Visa Slot Alert Bot
A Telegram bot that monitors visa appointment slots and sends alerts.
"""

import asyncio
import aiohttp
from bs4 import BeautifulSoup
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    CallbackContext,
    ContextTypes
)
from telegram.error import TelegramError, RetryAfter, TimedOut
import os
from dotenv import load_dotenv
from datetime import datetime
import logging
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from enum import Enum
import sys

# =============================================================================
# CONFIGURATION & CONSTANTS
# =============================================================================

load_dotenv()

# Bot Configuration
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
VISA_SLOTS_URL = os.getenv("VISA_SLOTS_URL", "https://visaslots.info/")

# Logging Configuration
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=getattr(logging, LOG_LEVEL),
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('visa_bot.log')
    ]
)
logger = logging.getLogger(__name__)

# Menu Options
VISA_TYPES = ["B1", "B2", "B1/B2", "F-1", "H-1B", "J-1", "L-1"]
CITIES = ["ALL", "MUMBAI", "HYDERABAD", "CHENNAI", "NEW DELHI", "KOLKATA"]
CONSULATE_TYPES = ["CONSULAR", "VAC"]
YEAR_OPTIONS = ["No Filter", "2025", "2026", "2027"]
INTERVALS = {
    "5 min": 300,
    "10 min": 600,
    "30 min": 1800,
    "60 min": 3600
}

# Rate Limiting
MAX_RETRIES = 3
RETRY_DELAY = 5
REQUEST_TIMEOUT = 30
RATE_LIMIT_DELAY = 2

# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class VisaSlot:
    """Represents a visa slot entry"""
    location: str
    visa_type: str
    last_updated: str
    earliest_date: str
    slots_available: str

    def __str__(self) -> str:
        return (
            f"Location: {self.location}, "
            f"Type: {self.visa_type}, "
            f"Date: {self.earliest_date}, "
            f"Slots: {self.slots_available}"
        )

    def is_available(self) -> bool:
        """Check if slot is available"""
        return (
            self.slots_available != "0" and
            self.earliest_date != "N/A" and
            self.earliest_date.strip() != ""
        )


@dataclass
class UserPreferences:
    """User preferences for slot monitoring"""
    visa_type: Optional[str] = None
    consulate_city: Optional[str] = None
    consulate_type: Optional[str] = None
    interval: Optional[int] = None
    year_filter: Optional[List[str]] = None
    no_slot_alert_sent: bool = False

    def is_complete(self) -> bool:
        """Check if all required preferences are set"""
        return all([
            self.visa_type,
            self.consulate_city,
            self.consulate_type,
            self.interval is not None
        ])

    def get_full_consulate(self) -> str:
        """Get formatted consulate string"""
        if self.consulate_city and self.consulate_type:
            return f"{self.consulate_city} {self.consulate_type}"
        return "Not set"

    def get_summary(self) -> str:
        """Get formatted summary of preferences"""
        return (
            f"• Visa Type: {self.visa_type or 'Not set'}\n"
            f"• Consulate: {self.get_full_consulate()}\n"
            f"• Year Filter: {self.year_filter or 'None'}\n"
            f"• Interval: {self.interval // 60 if self.interval else 'Not set'} min"
        )


class BotState(Enum):
    """Bot states"""
    IDLE = "idle"
    RUNNING = "running"
    ERROR = "error"


# =============================================================================
# GLOBAL STATE MANAGEMENT
# =============================================================================

class UserDataManager:
    """Thread-safe user data manager"""
    
    def __init__(self):
        self._user_data: Dict[int, UserPreferences] = {}
        self._alert_tasks: Dict[int, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def get_preferences(self, chat_id: int) -> UserPreferences:
        """Get or create user preferences"""
        async with self._lock:
            if chat_id not in self._user_data:
                self._user_data[chat_id] = UserPreferences()
            return self._user_data[chat_id]

    async def set_alert_task(self, chat_id: int, task: asyncio.Task):
        """Set alert task for user"""
        async with self._lock:
            self._alert_tasks[chat_id] = task

    async def get_alert_task(self, chat_id: int) -> Optional[asyncio.Task]:
        """Get alert task for user"""
        async with self._lock:
            return self._alert_tasks.get(chat_id)

    async def remove_alert_task(self, chat_id: int):
        """Remove alert task for user"""
        async with self._lock:
            if chat_id in self._alert_tasks:
                del self._alert_tasks[chat_id]

    async def is_running(self, chat_id: int) -> bool:
        """Check if alerts are running for user"""
        task = await self.get_alert_task(chat_id)
        return task is not None and not task.done()


# Initialize global manager
user_manager = UserDataManager()


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def visa_matches_site(user_pref: str, site_visa: str) -> bool:
    """
    Check if user's visa preference matches site visa type
    
    Args:
        user_pref: User's visa type preference
        site_visa: Visa type from website
        
    Returns:
        True if they match, False otherwise
    """
    # Normalize strings
    user_pref = user_pref.upper().strip()
    site_visa = site_visa.upper().strip()
    
    visa_mappings = {
        "B1": ["B1", "B1/B2"],
        "B2": ["B2", "B1/B2"],
        "B1/B2": ["B1/B2"],
        "F-1": ["F1", "F1/F2", "F-1"],
        "H-1B": ["H1B", "H-1B", "H1", "H-1"],
        "J-1": ["J1", "J-1"],
        "L-1": ["L1", "L-1"]
    }
    
    if user_pref in visa_mappings:
        return site_visa in visa_mappings[user_pref]
    
    return user_pref == site_visa


def year_matches(date_str: str, year_filter: Optional[List[str]]) -> bool:
    """
    Check if date matches year filter
    
    Args:
        date_str: Date string to check
        year_filter: List of years to filter by
        
    Returns:
        True if matches or no filter, False otherwise
    """
    if not year_filter or date_str in ["N/A", "", None]:
        return True
    return any(year in date_str for year in year_filter)


def validate_environment() -> bool:
    """Validate required environment variables"""
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not found in environment variables!")
        return False
    
    if not CHAT_ID:
        logger.warning("CHAT_ID not set. Bot will work but alerts won't be sent.")
    
    return True


# =============================================================================
# WEB SCRAPING
# =============================================================================

class VisaSlotsScraper:
    """Web scraper for visa slots"""
    
    def __init__(self, url: str = VISA_SLOTS_URL):
        self.url = url
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        """Async context manager entry"""
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        if self.session:
            await self.session.close()

    async def fetch_slots(self) -> List[VisaSlot]:
        """
        Fetch visa slots from website with retry logic
        
        Returns:
            List of VisaSlot objects
        """
        for attempt in range(MAX_RETRIES):
            try:
                if not self.session:
                    raise RuntimeError("Session not initialized. Use async context manager.")
                
                logger.info(f"Fetching visa slots (attempt {attempt + 1}/{MAX_RETRIES})")
                
                async with self.session.get(self.url) as response:
                    if response.status != 200:
                        logger.warning(f"HTTP {response.status} received")
                        if attempt < MAX_RETRIES - 1:
                            await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                            continue
                        return []
                    
                    html = await response.text()
                    slots = self._parse_html(html)
                    logger.info(f"Successfully fetched {len(slots)} slots")
                    return slots
                    
            except asyncio.TimeoutError:
                logger.error(f"Timeout on attempt {attempt + 1}")
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                    continue
                    
            except aiohttp.ClientError as e:
                logger.error(f"Client error on attempt {attempt + 1}: {e}")
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                    continue
                    
            except Exception as e:
                logger.error(f"Unexpected error fetching slots: {e}", exc_info=True)
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                    continue
        
        logger.error("All fetch attempts failed")
        return []

    def _parse_html(self, html: str) -> List[VisaSlot]:
        """
        Parse HTML and extract visa slots
        
        Args:
            html: HTML content
            
        Returns:
            List of VisaSlot objects
        """
        try:
            soup = BeautifulSoup(html, "html.parser")
            tables = soup.find_all("table")
            all_slots = []
            
            for table in tables:
                rows = table.find_all("tr")[1:]  # Skip header
                for row in rows:
                    cols = row.find_all("td")
                    if len(cols) >= 5:
                        slot = VisaSlot(
                            location=cols[0].text.strip(),
                            visa_type=cols[1].text.strip(),
                            last_updated=cols[2].text.strip(),
                            earliest_date=cols[3].text.strip(),
                            slots_available=cols[4].text.strip()
                        )
                        all_slots.append(slot)
            
            return all_slots
            
        except Exception as e:
            logger.error(f"Error parsing HTML: {e}", exc_info=True)
            return []


# =============================================================================
# TELEGRAM MESSAGING
# =============================================================================

class TelegramMessenger:
    """Handle Telegram message sending with error handling"""
    
    def __init__(self, bot_token: str):
        self.bot = Bot(token=bot_token)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str = "Markdown",
        reply_markup=None
    ) -> bool:
        """
        Send message with retry logic
        
        Returns:
            True if successful, False otherwise
        """
        for attempt in range(MAX_RETRIES):
            try:
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                    disable_web_page_preview=True
                )
                return True
                
            except RetryAfter as e:
                logger.warning(f"Rate limited. Waiting {e.retry_after} seconds")
                await asyncio.sleep(e.retry_after)
                
            except TimedOut:
                logger.warning(f"Timeout on attempt {attempt + 1}")
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY)
                    
            except TelegramError as e:
                logger.error(f"Telegram error: {e}")
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY)
                else:
                    return False
                    
            except Exception as e:
                logger.error(f"Unexpected error sending message: {e}", exc_info=True)
                return False
        
        return False

    async def send_slot_alert(self, chat_id: int, slot: VisaSlot) -> bool:
        """Send visa slot alert"""
        message = (
            f"🚨 *Visa Slot Alert!* 🚨\n\n"
            f"📍 *Location:* {slot.location}\n"
            f"📌 *Visa Type:* {slot.visa_type}\n"
            f"⏳ *Earliest Date:* {slot.earliest_date}\n"
            f"🟢 *Slots Available:* {slot.slots_available}\n"
            f"⏰ *Last Updated:* {slot.last_updated}\n\n"
            f"🔗 [Check Now]({VISA_SLOTS_URL})"
        )
        return await self.send_message(chat_id, message)


# =============================================================================
# SLOT FILTERING
# =============================================================================

class SlotFilter:
    """Filter visa slots based on user preferences"""
    
    @staticmethod
    def filter_slots(
        all_slots: List[VisaSlot],
        preferences: UserPreferences
    ) -> tuple[List[VisaSlot], List[VisaSlot]]:
        """
        Filter slots into matching and other locations
        
        Returns:
            Tuple of (matching_slots, other_slots)
        """
        matching_preference = []
        other_locations = []

        for slot in all_slots:
            if not visa_matches_site(preferences.visa_type, slot.visa_type):
                continue

            if preferences.consulate_city == "ALL":
                if slot.location.strip().endswith(preferences.consulate_type):
                    matching_preference.append(slot)
                else:
                    other_locations.append(slot)
            else:
                preferred_location = preferences.get_full_consulate()
                if slot.location == preferred_location:
                    matching_preference.append(slot)
                else:
                    other_locations.append(slot)

        # Filter by availability and year
        matching_open = [
            s for s in matching_preference
            if s.is_available() and year_matches(s.earliest_date, preferences.year_filter)
        ]

        other_open = [
            s for s in other_locations
            if s.is_available() and year_matches(s.earliest_date, preferences.year_filter)
        ]

        return matching_open, other_open


# =============================================================================
# ALERT SYSTEM
# =============================================================================

class AlertSystem:
    """Main alert monitoring system"""
    
    def __init__(self, messenger: TelegramMessenger):
        self.messenger = messenger
        self.scraper: Optional[VisaSlotsScraper] = None

    async def run_alert_loop(self, chat_id: int):
        """
        Main alert loop for monitoring slots
        
        Args:
            chat_id: Telegram chat ID to send alerts to
        """
        preferences = await user_manager.get_preferences(chat_id)
        logger.info(f"Starting alert loop for chat_id: {chat_id}")
        
        async with VisaSlotsScraper() as scraper:
            self.scraper = scraper
            
            try:
                while True:
                    try:
                        await self._check_slots(chat_id, preferences)
                    except Exception as e:
                        logger.error(f"Error in slot check: {e}", exc_info=True)
                        await self.messenger.send_message(
                            chat_id,
                            f"⚠️ Error checking slots: {str(e)[:100]}\n"
                            f"Will retry in {preferences.interval // 60} minutes."
                        )
                    
                    logger.info(f"Waiting {preferences.interval} seconds until next check")
                    await asyncio.sleep(preferences.interval)
                    
            except asyncio.CancelledError:
                logger.info(f"Alert loop cancelled for chat_id: {chat_id}")
                raise
            finally:
                await user_manager.remove_alert_task(chat_id)

    async def _check_slots(self, chat_id: int, preferences: UserPreferences):
        """Check for available slots and send alerts"""
        all_slots = await self.scraper.fetch_slots()
        
        if not all_slots:
            logger.warning("No slots data retrieved")
            return

        matching_open, other_open = SlotFilter.filter_slots(all_slots, preferences)

        if matching_open:
            preferences.no_slot_alert_sent = False
            logger.info(f"Found {len(matching_open)} matching slots")
            
            for slot in matching_open:
                await self.messenger.send_slot_alert(chat_id, slot)
                await asyncio.sleep(RATE_LIMIT_DELAY)  # Rate limiting
                
        elif other_open:
            await self._send_alternative_locations(chat_id, other_open, preferences)
        else:
            await self._send_no_slots_message(chat_id, preferences)

    async def _send_alternative_locations(
        self,
        chat_id: int,
        other_slots: List[VisaSlot],
        preferences: UserPreferences
    ):
        """Send message about alternative locations"""
        if preferences.no_slot_alert_sent:
            return

        summary_lines = [
            "⚠️ *No slots at your preferred consulate*\n",
            "Other open locations for your visa type:\n"
        ]
        
        for slot in other_slots[:5]:  # Limit to 5
            summary_lines.append(
                f"• {slot.location} | "
                f"Date: {slot.earliest_date} | "
                f"Slots: {slot.slots_available}"
            )
        
        if len(other_slots) > 5:
            summary_lines.append(f"\n_...and {len(other_slots) - 5} more locations_")
        
        await self.messenger.send_message(chat_id, "\n".join(summary_lines))
        preferences.no_slot_alert_sent = True

    async def _send_no_slots_message(self, chat_id: int, preferences: UserPreferences):
        """Send message when no slots are found"""
        if not preferences.no_slot_alert_sent:
            await self.messenger.send_message(
                chat_id,
                "ℹ️ No open slots found at this time.\n"
                f"Next check in {preferences.interval // 60} minutes..."
            )
            preferences.no_slot_alert_sent = True


# =============================================================================
# COMMAND HANDLERS
# =============================================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    chat_id = update.effective_chat.id
    await user_manager.get_preferences(chat_id)  # Initialize
    
    welcome_message = (
        "🤖 *Welcome to Visa Slot Alert Bot!*\n\n"
        "I'll help you monitor visa appointment slots.\n\n"
        "*Commands:*\n"
        "/set\\_visa - Set visa type\n"
        "/set\\_consulate - Set consulate location\n"
        "/set\\_interval - Set check interval\n"
        "/start\\_alerts - Start monitoring\n"
        "/stop - Stop monitoring\n"
        "/status - Check current settings\n"
        "/help - Show help message"
    )
    
    await update.message.reply_text(welcome_message, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
    help_text = (
        "📖 *How to use this bot:*\n\n"
        "1️⃣ Set your visa type using /set\\_visa\n"
        "2️⃣ Set your consulate using /set\\_consulate\n"
        "3️⃣ Set check interval using /set\\_interval\n"
        "4️⃣ Start monitoring with /start\\_alerts\n\n"
        "*Tips:*\n"
        "• Use /status to check your current settings\n"
        "• Use /stop to stop monitoring\n"
        "• You can change settings anytime\n\n"
        "*Need support?* Contact the bot administrator."
    )
    
    await update.message.reply_text(help_text, parse_mode="Markdown")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command"""
    chat_id = update.effective_chat.id
    preferences = await user_manager.get_preferences(chat_id)
    is_running = await user_manager.is_running(chat_id)
    
    status_emoji = "🟢 Running" if is_running else "🔴 Stopped"
    
    message = (
        f"📊 *Current Status*\n\n"
        f"Status: {status_emoji}\n\n"
        f"{preferences.get_summary()}"
    )
    
    await update.message.reply_text(message, parse_mode="Markdown")


async def set_visa_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /set_visa command"""
    keyboard = [
        [InlineKeyboardButton(visa, callback_data=f"visa_{visa}")]
        for visa in VISA_TYPES
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "📋 Select your Visa Type:",
        reply_markup=reply_markup
    )


async def set_consulate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /set_consulate command"""
    keyboard = [
        [InlineKeyboardButton(city, callback_data=f"city_{city}")]
        for city in CITIES
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "🏛️ Select Consulate City:",
        reply_markup=reply_markup
    )


async def set_interval_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /set_interval command"""
    keyboard = [
        [InlineKeyboardButton(interval, callback_data=f"interval_{interval}")]
        for interval in INTERVALS.keys()
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "⏰ Select Check Interval:",
        reply_markup=reply_markup
    )


async def start_alerts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start_alerts command"""
    if update.callback_query:
        await update.callback_query.answer()
        message = update.callback_query.message
        chat_id = update.callback_query.message.chat_id
    else:
        message = update.message
        chat_id = update.effective_chat.id

    preferences = await user_manager.get_preferences(chat_id)

    if not preferences.is_complete():
        await message.reply_text(
            "⚠️ *Incomplete Configuration*\n\n"
            "Please set all required fields:\n"
            "• Visa Type (/set\\_visa)\n"
            "• Consulate (/set\\_consulate)\n"
            "• Interval (/set\\_interval)",
            parse_mode="Markdown"
        )
        return

    if await user_manager.is_running(chat_id):
        await message.reply_text("⚠️ Alerts are already running!")
        return

    summary = (
        f"🔔 *Alerts Started!*\n\n"
        f"{preferences.get_summary()}\n\n"
        f"⏳ Checking for slots now..."
    )
    await message.reply_text(summary, parse_mode="Markdown")

    # Create and start alert task
    messenger = TelegramMessenger(BOT_TOKEN)
    alert_system = AlertSystem(messenger)
    task = asyncio.create_task(alert_system.run_alert_loop(chat_id))
    await user_manager.set_alert_task(chat_id, task)


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stop command"""
    chat_id = update.effective_chat.id
    task = await user_manager.get_alert_task(chat_id)
    
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await user_manager.remove_alert_task(chat_id)
        await update.message.reply_text("🛑 Alerts stopped successfully.")
    else:
        await update.message.reply_text("⚠️ No alerts are currently running.")


# =============================================================================
# CALLBACK HANDLERS
# =============================================================================

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all callback queries"""
    query = update.callback_query
    await query.answer()
    
    chat_id = update.effective_chat.id
    preferences = await user_manager.get_preferences(chat_id)
    data = query.data

    if data.startswith("visa_"):
        await handle_visa_selection(query, preferences, data)
        
    elif data.startswith("city_"):
        await handle_city_selection(query, preferences, data)
        
    elif data.startswith("type_"):
        await handle_type_selection(query, preferences, data)
        
    elif data.startswith("year_"):
        await handle_year_selection(query, preferences, data)
        
    elif data.startswith("interval_"):
        await handle_interval_selection(query, preferences, data)
        
    elif data == "start_alerts":
        # Create Update object for start_alerts_command
        await start_alerts_command(update, context)


async def handle_visa_selection(query, preferences: UserPreferences, data: str):
    """Handle visa type selection"""
    preferences.visa_type = data.replace("visa_", "")
    await query.message.reply_text(f"✅ Visa Type: {preferences.visa_type}")
    
    keyboard = [
        [InlineKeyboardButton(city, callback_data=f"city_{city}")]
        for city in CITIES
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text(
        "🏛️ Select Consulate City:",
        reply_markup=reply_markup
    )


async def handle_city_selection(query, preferences: UserPreferences, data: str):
    """Handle city selection"""
    preferences.consulate_city = data.replace("city_", "")
    await query.message.reply_text(f"✅ City: {preferences.consulate_city}")
    
    keyboard = [
        [InlineKeyboardButton(ctype, callback_data=f"type_{ctype}")]
        for ctype in CONSULATE_TYPES
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text(
        "🏢 Select Consulate Type:",
        reply_markup=reply_markup
    )


async def handle_type_selection(query, preferences: UserPreferences, data: str):
    """Handle consulate type selection"""
    preferences.consulate_type = data.replace("type_", "")
    await query.message.reply_text(f"✅ Type: {preferences.consulate_type}")
    
    keyboard = [
        [InlineKeyboardButton(year, callback_data=f"year_{year}")]
        for year in YEAR_OPTIONS
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text(
        "📅 Select Year Filter:",
        reply_markup=reply_markup
    )


async def handle_year_selection(query, preferences: UserPreferences, data: str):
    """Handle year filter selection"""
    selection = data.replace("year_", "")
    if selection == "No Filter":
        preferences.year_filter = None
    else:
        preferences.year_filter = [selection]
    
    await query.message.reply_text(f"✅ Year Filter: {selection}")
    
    keyboard = [
        [InlineKeyboardButton(interval, callback_data=f"interval_{interval}")]
        for interval in INTERVALS.keys()
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.message.reply_text(
        "⏰ Select Check Interval:",
        reply_markup=reply_markup
    )


async def handle_interval_selection(query, preferences: UserPreferences, data: str):
    """Handle interval selection"""
    interval_key = data.replace("interval_", "")
    preferences.interval = INTERVALS[interval_key]
    
    await query.message.reply_text(f"✅ Interval: {interval_key}")
    
    summary = (
        "🎯 *Configuration Complete!*\n\n"
        f"{preferences.get_summary()}"
    )
    
    keyboard = [[InlineKeyboardButton("🚀 Start Alerts", callback_data="start_alerts")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.message.reply_text(summary, parse_mode="Markdown")
    await query.message.reply_text(
        "Click below to start monitoring:",
        reply_markup=reply_markup
    )


# =============================================================================
# ERROR HANDLER
# =============================================================================

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors"""
    logger.error(f"Update {update} caused error {context.error}", exc_info=context.error)
    
    if update and update.effective_message:
        await update.effective_message.reply_text(
            "❌ An error occurred. Please try again or contact support."
        )


# =============================================================================
# MAIN APPLICATION
# =============================================================================

def main():
    """Main application entry point"""
    logger.info("=" * 50)
    logger.info("Starting Visa Slot Alert Bot")
    logger.info("=" * 50)
    
    # Validate environment
    if not validate_environment():
        logger.error("Environment validation failed. Exiting.")
        sys.exit(1)
    
    # Build application
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Add command handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("set_visa", set_visa_command))
    app.add_handler(CommandHandler("set_consulate", set_consulate_command))
    app.add_handler(CommandHandler("set_interval", set_interval_command))
    app.add_handler(CommandHandler("start_alerts", start_alerts_command))
    app.add_handler(CommandHandler("stop", stop_command))
    
    # Add callback handler
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    # Add error handler
    app.add_error_handler(error_handler)
    
    logger.info("🤖 Bot is running and ready to receive commands...")
    
    # Run bot
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.critical(f"Critical error: {e}", exc_info=True)
        sys.exit(1)