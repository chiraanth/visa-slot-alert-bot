"""
Visa Slot Alert Bot
A Telegram bot that monitors visa appointment slots and sends alerts.
NO AUTHENTICATION OR OTP REQUIRED - Scrapes public data only.
"""

import asyncio
import aiohttp
from bs4 import BeautifulSoup
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes
)
from telegram.error import TelegramError, RetryAfter, TimedOut
import os
from dotenv import load_dotenv
from datetime import datetime
import logging
from typing import Dict, List, Optional
from dataclasses import dataclass, field
import sys
import json
from pathlib import Path

# =============================================================================
# CONFIGURATION
# =============================================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
VISA_SLOTS_URL = os.getenv("VISA_SLOTS_URL", "https://visaslots.info/")

# Logging Configuration
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('visa_bot.log')
    ]
)
logger = logging.getLogger(__name__)

# Menu Options
VISA_TYPES = ["B1", "B2", "B1/B2", "F-1", "H-1B", "J-1", "L-1", "O-1"]
CITIES = ["ALL", "MUMBAI", "HYDERABAD", "CHENNAI", "NEW DELHI", "KOLKATA"]
CONSULATE_TYPES = ["CONSULAR", "VAC"]
YEAR_OPTIONS = ["No Filter", "2025", "2026", "2027"]
INTERVALS = {
    "1 min": 60,
    "5 min": 300,
    "10 min": 600,
    "30 min": 1800,
    "60 min": 3600
}

# Constants
MAX_RETRIES = 3
RETRY_DELAY = 5
REQUEST_TIMEOUT = 30
RATE_LIMIT_DELAY = 2
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

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

    def is_available(self) -> bool:
        """Check if slot is available"""
        return (
            self.slots_available != "0" and
            self.earliest_date not in ["N/A", "", "No Appointments Available"]
        )

    def to_dict(self) -> dict:
        """Convert to dictionary"""
        return {
            "location": self.location,
            "visa_type": self.visa_type,
            "last_updated": self.last_updated,
            "earliest_date": self.earliest_date,
            "slots_available": self.slots_available
        }


@dataclass
class UserPreferences:
    """User preferences for slot monitoring"""
    visa_type: Optional[str] = None
    consulate_city: Optional[str] = None
    consulate_type: Optional[str] = None
    interval: Optional[int] = None
    year_filter: Optional[List[str]] = None
    no_slot_alert_sent: bool = False
    last_notified_slots: List[str] = field(default_factory=list)

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

    def to_dict(self) -> dict:
        """Convert to dictionary for persistence"""
        return {
            "visa_type": self.visa_type,
            "consulate_city": self.consulate_city,
            "consulate_type": self.consulate_type,
            "interval": self.interval,
            "year_filter": self.year_filter,
            "no_slot_alert_sent": self.no_slot_alert_sent,
            "last_notified_slots": self.last_notified_slots
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'UserPreferences':
        """Create from dictionary"""
        return cls(
            visa_type=data.get("visa_type"),
            consulate_city=data.get("consulate_city"),
            consulate_type=data.get("consulate_type"),
            interval=data.get("interval"),
            year_filter=data.get("year_filter"),
            no_slot_alert_sent=data.get("no_slot_alert_sent", False),
            last_notified_slots=data.get("last_notified_slots", [])
        )


# =============================================================================
# PERSISTENCE
# =============================================================================

class UserDataPersistence:
    """Save and load user preferences"""
    
    def __init__(self, data_dir: Path = DATA_DIR):
        self.data_dir = data_dir
        self.data_file = data_dir / "user_data.json"

    def save_user_data(self, user_data: Dict[int, UserPreferences]):
        """Save user data to file"""
        try:
            data = {
                str(chat_id): prefs.to_dict()
                for chat_id, prefs in user_data.items()
            }
            with open(self.data_file, 'w') as f:
                json.dump(data, f, indent=2)
            logger.info(f"Saved data for {len(data)} users")
        except Exception as e:
            logger.error(f"Error saving user data: {e}")

    def load_user_data(self) -> Dict[int, UserPreferences]:
        """Load user data from file"""
        try:
            if not self.data_file.exists():
                return {}
            
            with open(self.data_file, 'r') as f:
                data = json.load(f)
            
            user_data = {
                int(chat_id): UserPreferences.from_dict(prefs)
                for chat_id, prefs in data.items()
            }
            logger.info(f"Loaded data for {len(user_data)} users")
            return user_data
        except Exception as e:
            logger.error(f"Error loading user data: {e}")
            return {}


# =============================================================================
# STATE MANAGEMENT
# =============================================================================

class UserDataManager:
    """Thread-safe user data manager with persistence"""
    
    def __init__(self):
        self.persistence = UserDataPersistence()
        self._user_data: Dict[int, UserPreferences] = self.persistence.load_user_data()
        self._alert_tasks: Dict[int, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def get_preferences(self, chat_id: int) -> UserPreferences:
        """Get or create user preferences"""
        async with self._lock:
            if chat_id not in self._user_data:
                self._user_data[chat_id] = UserPreferences()
                self._save_data()
            return self._user_data[chat_id]

    def _save_data(self):
        """Save data to disk"""
        self.persistence.save_user_data(self._user_data)

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


user_manager = UserDataManager()


# =============================================================================
# UTILITIES
# =============================================================================

def visa_matches_site(user_pref: str, site_visa: str) -> bool:
    """Check if user's visa preference matches site visa type"""
    user_pref = user_pref.upper().strip()
    site_visa = site_visa.upper().strip()
    
    visa_mappings = {
        "B1": ["B1", "B1/B2"],
        "B2": ["B2", "B1/B2"],
        "B1/B2": ["B1/B2"],
        "F-1": ["F1", "F1/F2", "F-1"],
        "H-1B": ["H1B", "H-1B", "H1", "H-1"],
        "J-1": ["J1", "J-1"],
        "L-1": ["L1", "L-1"],
        "O-1": ["O1", "O-1"]
    }
    
    if user_pref in visa_mappings:
        return site_visa in visa_mappings[user_pref]
    
    return user_pref == site_visa


def year_matches(date_str: str, year_filter: Optional[List[str]]) -> bool:
    """Check if date matches year filter"""
    if not year_filter or date_str in ["N/A", "", None]:
        return True
    return any(year in date_str for year in year_filter)


def validate_environment() -> bool:
    """Validate required environment variables"""
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not found in environment variables!")
        return False
    return True


# =============================================================================
# ENHANCED WEB SCRAPING WITH ANTI-DETECTION
# =============================================================================

class VisaSlotsScraper:
    """Enhanced web scraper with anti-detection measures"""
    
    def __init__(self, url: str = VISA_SLOTS_URL):
        self.url = url
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        """Async context manager entry with rotating user agents"""
        # Rotate between different realistic user agents
        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2.1 Safari/605.1.15",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
        ]
        
        import random
        selected_ua = random.choice(user_agents)
        
        connector = aiohttp.TCPConnector(
            limit=10,
            ssl=False,  # Disable SSL verification if needed
            force_close=True  # Close connections after each request
        )
        
        self.session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            headers={
                "User-Agent": selected_ua,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "DNT": "1",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Cache-Control": "max-age=0",
                "sec-ch-ua": '"Not A(Brand";v="99", "Google Chrome";v="121", "Chromium";v="121"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"'
            }
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        if self.session:
            await self.session.close()
            # Give time for connections to close
            await asyncio.sleep(0.5)

    async def fetch_slots(self) -> List[VisaSlot]:
        """Fetch visa slots with enhanced anti-detection"""
        for attempt in range(MAX_RETRIES):
            try:
                if not self.session:
                    raise RuntimeError("Session not initialized")
                
                logger.info(f"🌐 Fetching visa slots (attempt {attempt + 1}/{MAX_RETRIES})")
                
                # Add random delay between retries to appear more human-like
                if attempt > 0:
                    delay = RETRY_DELAY * (attempt + 1) + random.uniform(1, 3)
                    logger.info(f"⏳ Waiting {delay:.1f}s before retry...")
                    await asyncio.sleep(delay)
                
                async with self.session.get(
                    self.url,
                    allow_redirects=True,
                    ssl=False  # Disable SSL verification
                ) as response:
                    
                    if response.status == 403:
                        logger.warning(f"⚠️ Access forbidden (403) - Attempt {attempt + 1}/{MAX_RETRIES}")
                        if attempt < MAX_RETRIES - 1:
                            # Wait longer on 403
                            await asyncio.sleep(RETRY_DELAY * (attempt + 2) * 3)
                            continue
                        logger.error("❌ Website is blocking requests after all retries")
                        return []
                    
                    if response.status == 429:
                        logger.warning("⚠️ Rate limited (429)")
                        retry_after = int(response.headers.get('Retry-After', 60))
                        logger.info(f"⏳ Waiting {retry_after}s as requested...")
                        await asyncio.sleep(retry_after)
                        continue
                    
                    if response.status != 200:
                        logger.warning(f"⚠️ HTTP {response.status} received")
                        if attempt < MAX_RETRIES - 1:
                            await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                            continue
                        return []
                    
                    html = await response.text()
                    
                    if len(html) < 100:
                        logger.warning("⚠️ Received suspiciously short response")
                        if attempt < MAX_RETRIES - 1:
                            await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                            continue
                    
                    slots = self._parse_html(html)
                    
                    if not slots:
                        logger.warning("⚠️ No slots parsed from HTML")
                        if attempt < MAX_RETRIES - 1:
                            continue
                    
                    logger.info(f"✅ Successfully fetched {len(slots)} slots")
                    return slots
                    
            except asyncio.TimeoutError:
                logger.error(f"⏱️ Timeout on attempt {attempt + 1}")
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                    
            except aiohttp.ClientError as e:
                logger.error(f"🔌 Network error on attempt {attempt + 1}: {e}")
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                    
            except Exception as e:
                logger.error(f"❌ Unexpected error: {e}", exc_info=True)
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1))
        
        logger.error("❌ All fetch attempts failed")
        return []

    def _parse_html(self, html: str) -> List[VisaSlot]:
        """Parse HTML and extract visa slots"""
        try:
            soup = BeautifulSoup(html, "html.parser")
            tables = soup.find_all("table")
            
            if not tables:
                logger.warning("⚠️ No tables found in HTML")
                # Log a sample of the HTML for debugging
                logger.debug(f"HTML preview: {html[:500]}")
                return []
            
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
            
            logger.info(f"📊 Parsed {len(all_slots)} slots from {len(tables)} tables")
            return all_slots
            
        except Exception as e:
            logger.error(f"❌ Error parsing HTML: {e}", exc_info=True)
            return []

# =============================================================================
# TELEGRAM MESSAGING
# =============================================================================

class TelegramMessenger:
    """Handle Telegram message sending"""
    
    def __init__(self, bot_token: str):
        self.bot = Bot(token=bot_token)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str = "Markdown",
        reply_markup=None
    ) -> bool:
        """Send message with retry logic"""
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
                logger.warning(f"⏳ Rate limited. Waiting {e.retry_after}s")
                await asyncio.sleep(e.retry_after)
                
            except TimedOut:
                logger.warning(f"⏱️ Timeout on attempt {attempt + 1}")
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY)
                    
            except TelegramError as e:
                logger.error(f"📱 Telegram error: {e}")
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY)
                else:
                    return False
                    
            except Exception as e:
                logger.error(f"❌ Error sending message: {e}", exc_info=True)
                return False
        
        return False

    async def send_slot_alert(self, chat_id: int, slot: VisaSlot, is_new: bool = True) -> bool:
        """Send visa slot alert"""
        emoji = "🆕" if is_new else "🔄"
        message = (
            f"{emoji} *Visa Slot Alert!* 🚨\n\n"
            f"📍 *Location:* {slot.location}\n"
            f"📌 *Visa Type:* {slot.visa_type}\n"
            f"📅 *Earliest Date:* {slot.earliest_date}\n"
            f"🟢 *Slots:* {slot.slots_available}\n"
            f"🕐 *Updated:* {slot.last_updated}\n\n"
            f"🔗 [Check Website]({VISA_SLOTS_URL})\n\n"
            f"_No login required!_"
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
        """Filter slots into matching and other locations"""
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
        """Main alert loop for monitoring slots"""
        preferences = await user_manager.get_preferences(chat_id)
        logger.info(f"🚀 Starting alert loop for chat_id: {chat_id}")
        
        await self.messenger.send_message(
            chat_id,
            "✅ *Monitoring Started!*\n\n"
            "🔍 Checking for slots...\n"
            "💡 _No login or OTP needed!_",
            parse_mode="Markdown"
        )
        
        async with VisaSlotsScraper() as scraper:
            self.scraper = scraper
            
            try:
                while True:
                    try:
                        await self._check_slots(chat_id, preferences)
                    except Exception as e:
                        logger.error(f"❌ Error in slot check: {e}", exc_info=True)
                        await self.messenger.send_message(
                            chat_id,
                            f"⚠️ *Error checking slots*\n\n"
                            f"Will retry in {preferences.interval // 60} min.\n\n"
                            f"_Error: {str(e)[:100]}_",
                            parse_mode="Markdown"
                        )
                    
                    logger.info(f"⏰ Waiting {preferences.interval}s until next check")
                    await asyncio.sleep(preferences.interval)
                    
            except asyncio.CancelledError:
                logger.info(f"🛑 Alert loop cancelled for chat_id: {chat_id}")
                await self.messenger.send_message(
                    chat_id,
                    "🛑 *Monitoring Stopped*",
                    parse_mode="Markdown"
                )
                raise
            finally:
                await user_manager.remove_alert_task(chat_id)

    async def _check_slots(self, chat_id: int, preferences: UserPreferences):
        """Check for available slots and send alerts"""
        all_slots = await self.scraper.fetch_slots()
        
        if not all_slots:
            logger.warning("⚠️ No slots data retrieved")
            if not preferences.no_slot_alert_sent:
                await self.messenger.send_message(
                    chat_id,
                    "⚠️ *Could not fetch slot data*\n\n"
                    "The website might be temporarily unavailable.\n"
                    "Will keep trying...",
                    parse_mode="Markdown"
                )
                preferences.no_slot_alert_sent = True
            return

        matching_open, other_open = SlotFilter.filter_slots(all_slots, preferences)

        if matching_open:
            preferences.no_slot_alert_sent = False
            logger.info(f"✅ Found {len(matching_open)} matching slots")
            
            for slot in matching_open:
                slot_key = f"{slot.location}_{slot.earliest_date}"
                is_new = slot_key not in preferences.last_notified_slots
                
                await self.messenger.send_slot_alert(chat_id, slot, is_new)
                
                if is_new:
                    preferences.last_notified_slots.append(slot_key)
                    preferences.last_notified_slots = preferences.last_notified_slots[-50:]
                
                await asyncio.sleep(RATE_LIMIT_DELAY)
                
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
            "⚠️ *No slots at preferred location*\n",
            f"Found {len(other_slots)} alternatives:\n"
        ]
        
        for slot in other_slots[:5]:
            summary_lines.append(
                f"• {slot.location}\n"
                f"  📅 {slot.earliest_date} | 🟢 {slot.slots_available} slots"
            )
        
        if len(other_slots) > 5:
            summary_lines.append(f"\n_...and {len(other_slots) - 5} more_")
        
        await self.messenger.send_message(chat_id, "\n".join(summary_lines), parse_mode="Markdown")
        preferences.no_slot_alert_sent = True

    async def _send_no_slots_message(self, chat_id: int, preferences: UserPreferences):
        """Send message when no slots found"""
        if not preferences.no_slot_alert_sent:
            await self.messenger.send_message(
                chat_id,
                f"ℹ️ *No slots available*\n\n"
                f"Next check in {preferences.interval // 60} min...",
                parse_mode="Markdown"
            )
            preferences.no_slot_alert_sent = True


# =============================================================================
# COMMAND HANDLERS
# =============================================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    chat_id = update.effective_chat.id
    await user_manager.get_preferences(chat_id)
    
    welcome_message = (
        "🤖 *Visa Slot Alert Bot*\n\n"
        "✅ NO LOGIN REQUIRED\n"
        "✅ NO OTP NEEDED\n"
        "✅ PUBLIC DATA ONLY\n\n"
        "*Setup (3 steps):*\n"
        "1️⃣ /set\\_visa - Choose visa type\n"
        "2️⃣ /set\\_consulate - Choose location\n"
        "3️⃣ /start\\_alerts - Start monitoring\n\n"
        "*Other commands:*\n"
        "/status - View settings\n"
        "/stop - Stop monitoring\n"
        "/help - Show help"
    )
    
    await update.message.reply_text(welcome_message, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
    help_text = (
        "📖 *Help & FAQ*\n\n"
        "*Q: Do I need to login?*\n"
        "A: No! This bot scrapes public data.\n\n"
        "*Q: Why no OTP?*\n"
        "A: We use publicly available information.\n\n"
        "*Q: How often does it check?*\n"
        "A: You choose (1-60 min intervals)\n\n"
        "*Setup:*\n"
        "1. /set\\_visa\n"
        "2. /set\\_consulate\n"
        "3. /set\\_interval\n"
        "4. /start\\_alerts"
    )
    
    await update.message.reply_text(help_text, parse_mode="Markdown")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command"""
    chat_id = update.effective_chat.id
    preferences = await user_manager.get_preferences(chat_id)
    is_running = await user_manager.is_running(chat_id)
    
    status_emoji = "🟢 Active" if is_running else "🔴 Stopped"
    
    message = (
        f"📊 *Status Report*\n\n"
        f"Status: {status_emoji}\n\n"
        f"*Settings:*\n"
        f"{preferences.get_summary()}\n\n"
        f"*Stats:*\n"
        f"• Slots notified: {len(preferences.last_notified_slots)}\n"
        f"• Auth required: ❌ None!\n\n"
        f"_Last updated: {datetime.now().strftime('%H:%M:%S')}_"
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
        "📋 *Select Visa Type:*",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )


async def set_consulate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /set_consulate command"""
    keyboard = [
        [InlineKeyboardButton(city, callback_data=f"city_{city}")]
        for city in CITIES
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "🏛️ *Select Consulate City:*",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )


async def set_interval_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /set_interval command"""
    keyboard = [
        [InlineKeyboardButton(interval, callback_data=f"interval_{interval}")]
        for interval in INTERVALS.keys()
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "⏰ *Select Check Interval:*",
        reply_markup=reply_markup,
        parse_mode="Markdown"
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
            "⚠️ *Setup Incomplete*\n\n"
            "Please complete setup:\n"
            "1. /set\\_visa\n"
            "2. /set\\_consulate\n"
            "3. /set\\_interval",
            parse_mode="Markdown"
        )
        return

    if await user_manager.is_running(chat_id):
        await message.reply_text("⚠️ Already monitoring!")
        return

    summary = (
        f"🔔 *Monitoring Started!*\n\n"
        f"{preferences.get_summary()}\n\n"
        f"✅ NO authentication needed\n"
        f"⏳ First check starting now..."
    )
    await message.reply_text(summary, parse_mode="Markdown")

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
        await update.message.reply_text(
            "🛑 *Monitoring Stopped*\n\n"
            "Use /start\\_alerts to resume",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("⚠️ Not currently monitoring")


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
        preferences.visa_type = data.replace("visa_", "")
        await query.message.reply_text(f"✅ Visa: {preferences.visa_type}")
        
        keyboard = [[InlineKeyboardButton(city, callback_data=f"city_{city}")] for city in CITIES]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text("🏛️ *Select City:*", reply_markup=reply_markup, parse_mode="Markdown")
        
    elif data.startswith("city_"):
        preferences.consulate_city = data.replace("city_", "")
        await query.message.reply_text(f"✅ City: {preferences.consulate_city}")
        
        keyboard = [[InlineKeyboardButton(t, callback_data=f"type_{t}")] for t in CONSULATE_TYPES]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text("🏢 *Select Type:*", reply_markup=reply_markup, parse_mode="Markdown")
        
    elif data.startswith("type_"):
        preferences.consulate_type = data.replace("type_", "")
        await query.message.reply_text(f"✅ Type: {preferences.consulate_type}")
        
        keyboard = [[InlineKeyboardButton(y, callback_data=f"year_{y}")] for y in YEAR_OPTIONS]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text("📅 *Year Filter:*", reply_markup=reply_markup, parse_mode="Markdown")
        
    elif data.startswith("year_"):
        selection = data.replace("year_", "")
        preferences.year_filter = None if selection == "No Filter" else [selection]
        await query.message.reply_text(f"✅ Year: {selection}")
        
        keyboard = [[InlineKeyboardButton(i, callback_data=f"interval_{i}")] for i in INTERVALS.keys()]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text("⏰ *Check Interval:*", reply_markup=reply_markup, parse_mode="Markdown")
        
    elif data.startswith("interval_"):
        interval_key = data.replace("interval_", "")
        preferences.interval = INTERVALS[interval_key]
        await query.message.reply_text(f"✅ Interval: {interval_key}")
        
        summary = (
            "🎯 *Setup Complete!*\n\n"
            f"{preferences.get_summary()}\n\n"
            "✅ No authentication required!"
        )
        
        keyboard = [[InlineKeyboardButton("🚀 Start Monitoring", callback_data="start_alerts")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.message.reply_text(summary, parse_mode="Markdown")
        await query.message.reply_text("Ready to start:", reply_markup=reply_markup)
        
    elif data == "start_alerts":
        await start_alerts_command(update, context)


# =============================================================================
# ERROR HANDLER
# =============================================================================

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors"""
    logger.error(f"Update {update} caused error {context.error}", exc_info=context.error)
    
    if update and update.effective_message:
        await update.effective_message.reply_text(
            "❌ *Error occurred*\n\n"
            "Please try again or use /help",
            parse_mode="Markdown"
        )


# =============================================================================
# MAIN
# =============================================================================

def main():
    """Main entry point"""
    logger.info("=" * 60)
    logger.info("🤖 Visa Slot Alert Bot - Starting")
    logger.info("=" * 60)
    
    if not validate_environment():
        sys.exit(1)
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("set_visa", set_visa_command))
    app.add_handler(CommandHandler("set_consulate", set_consulate_command))
    app.add_handler(CommandHandler("set_interval", set_interval_command))
    app.add_handler(CommandHandler("start_alerts", start_alerts_command))
    app.add_handler(CommandHandler("stop", stop_command))
    
    # Callbacks
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    # Errors
    app.add_error_handler(error_handler)
    
    logger.info("✅ Bot ready - NO OTP REQUIRED!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("🛑 Stopped by user")
    except Exception as e:
        logger.critical(f"💥 Critical error: {e}", exc_info=True)
        sys.exit(1)